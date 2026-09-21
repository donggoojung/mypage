"""소싱처 무인 발주 RPA 클라이언트 (PRD 5.2).

주의 — 실사이트 미검증 + 실제 결제 기능: 이 파일의 `PlaywrightRPAClient`는 abc_mart.py의
상품상세 크롤러를 실제 devtools 스크린샷으로 하나하나 검증했던 것과 같은 과정을 아직
카트/체크아웃/결제 화면에는 거치지 못했다. 아래 4개 메서드는 한국 쇼핑몰에서 흔히 쓰는
버튼 문구(장바구니, 주문하기, 결제하기 등)를 기반으로 한 최선의 추정 구현이며, 실제로
동작하는지는 반드시 `scripts/test_rpa_checkout.py`를 `headless=False`(기본값)로 실행해
눈으로 확인해야 한다 — 버튼을 못 찾고 타임아웃 나는 것은 안전하지만(돈이 안 나감), 엉뚱한
버튼을 잘못 눌러 잘못된 사이즈/수량으로 결제되는 것은 막을 수 없기 때문이다.

이중 안전장치:
  1. `USE_MOCK_RPA=true`(기본값)면 이 클래스 자체가 아예 호출되지 않는다 (MockRPAClient 사용).
  2. `confirm_final_payment=False`(기본값)면, 실제 소싱처 사이트를 열어 장바구니/배송지 입력까지
     다 진행하고 최종 결제 직전(주문서 확인 화면)에서 멈춘다 — 진짜 "결제하기" 버튼은
     `confirm_final_payment=True`를 명시적으로 넘겼을 때만 클릭한다 (쿠팡의 request_approval과
     같은 설계).

두 안전장치를 모두 끄기 전에는 반드시 `scripts/test_rpa_checkout.py`로 여러 번 시각적으로
검증하고, 버튼 선택자가 틀려서 멈추는 지점이 있으면 실제 화면 스크린샷을 보고 SELECTOR_*를
고쳐야 한다.
"""

import asyncio
import random
import re
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.integrations.rpa.base import BaseRPAClient, ShippingInfo
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs

# PRD 5.2-3: 사람처럼 보이도록 타이핑 사이 지연을 준다.
TYPING_DELAY_MS_RANGE = (100, 150)

# --- 아래 셀렉터/문구는 전부 실사이트 미검증(최선의 추정)이다. abc_mart.py의 SELECTOR_SIZE_OPTIONS만
# 상품상세 페이지 구조로 실제 검증됐고(2026-09-19), 사이즈를 "클릭"했을 때 장바구니/구매 흐름이
# 어떻게 이어지는지는 아직 확인 전이다.
SELECTOR_SIZE_OPTIONS = ".size-list .btn-prod-size, .size-option li, .option-size li"
# 우리는 "장바구니에 담아뒀다가 나중에 결제"할 일이 없다 — 고객 주문 1건당 상품 1개를 그
# 자리에서 바로 사는 거라 "바로구매"를 최우선으로 시도한다. 2026-09-21 실사이트에서
# "장바구니 담기"는 브라우저 네이티브 confirm() 팝업 + 장바구니 페이지 경유가 필요해
# 더 깨지기 쉬운 경로였다("이동하시겠습니까" 팝업이 뜨자마자 사라지는 등) — "바로구매"는
# 그 팝업/장바구니 페이지 없이 바로 주문서(/order) 화면으로 넘어가서 더 안정적이다.
BUY_NOW_BUTTON_TEXTS = ["바로구매", "바로 구매"]
ADD_TO_CART_BUTTON_TEXTS = ["장바구니 담기", "장바구니"]
CHECKOUT_BUTTON_TEXTS = ["주문하기", "구매하기", "선택상품 주문", "바로구매"]
PAYMENT_METHOD_SECTION_TEXTS = ["결제수단", "결제 수단"]
FINAL_PAYMENT_BUTTON_TEXTS = ["결제하기", "최종결제", "결제 하기"]
ORDER_NUMBER_LABEL_PATTERN = re.compile(r"주문\s*번호\s*[:：]?\s*([A-Za-z0-9-]+)")

# 최종 결제 직전 단계까지만 진행하고 멈췄을 때 반환하는 값 — 실제 주문번호가 아니라는 걸
# 호출자가 바로 알아볼 수 있게 접두어를 확실히 다르게 둔다.
REVIEW_ONLY_PREFIX = "REVIEW_ONLY"


class RPAPurchaseError(RuntimeError):
    """무인 발주 진행 중(재고 소진, 세션 만료, 결제 실패 등) 발생한 오류."""


def _validate_shipping_info(shipping_info: ShippingInfo) -> None:
    missing = [
        field
        for field in ("recipient_name", "recipient_phone", "shipping_addr")
        if not getattr(shipping_info, field)
    ]
    if missing:
        raise RPAPurchaseError(f"배송지 정보 누락: {missing}")


class MockRPAClient(BaseRPAClient):
    """실제 사이트 호출 없이, 배송지 검증 로직과 발주 성공 흐름만 재현하는 Mock 구현."""

    async def purchase_order(
        self, style_code: str, size: str, shipping_info: ShippingInfo, confirm_final_payment: bool = False
    ) -> str:
        # Mock은 실제 결제가 없으니 confirm_final_payment 값과 무관하게 항상 성공 처리한다.
        _validate_shipping_info(shipping_info)
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        return f"MOCK-{style_code}-{size}-{timestamp}-{random.randint(1000, 9999)}"


class PlaywrightRPAClient(BaseRPAClient):
    """Headless Chromium(Playwright)으로 소싱처 사이트에서 실제 무인 결제를 수행한다.

    `session_cookies`는 Redis 등 인메모리 캐시에 미리 저장해둔, 소싱처에 로그인된
    상태의 쿠키 목록이다 (PRD 5.2-1 "세션 복원") — 이 클라이언트는 주입만 담당하고,
    로그인 자체(캡차 포함)는 별도 배치로 미리 완료해 세션을 채워둬야 한다.
    """

    def __init__(
        self,
        source_base_url: str,
        session_cookies: list[dict],
        settings: Settings | None = None,
        headless: bool = True,
        pause_on_error: bool = False,
    ):
        self._source_base_url = source_base_url.rstrip("/")
        self._session_cookies = session_cookies
        self._settings = settings or get_settings()
        self._headless = headless
        # 디버깅 전용 — 어느 단계에서 실패하면 브라우저를 바로 안 닫고, 사람이 실제(로그인된)
        # 화면을 직접 보고 캡처할 시간을 준다. scripts/test_rpa_checkout.py만 True로 켜고,
        # 실제 서비스(order_processor.py)에서는 항상 False로 둬야 한다 — 사람이 없는 서버
        # 환경에서 켜두면 실패한 세션이 영원히 안 닫힌 채로 남는다.
        self._pause_on_error = pause_on_error

    async def purchase_order(
        self, style_code: str, size: str, shipping_info: ShippingInfo, confirm_final_payment: bool = False
    ) -> str:
        _validate_shipping_info(shipping_info)
        if not self._session_cookies:
            raise RPAPurchaseError(
                "소싱처 로그인 세션(쿠키)이 없습니다 — scripts/save_abc_mart_session.py로 먼저 "
                "로그인 세션을 저장해주세요. 세션 복원 없이는 발주를 진행할 수 없습니다."
            )

        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self._headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                await context.add_cookies(self._session_cookies)
                page = await context.new_page()

                # 2026-09-21 실사이트 devtools로 확인됨: "장바구니 담기"를 누르면 HTML
                # 버튼이 아니라 브라우저 자체 confirm() 팝업("장바구니로 이동하시겠습니까?")이
                # 뜬다. Playwright는 이런 팝업을 처리하는 핸들러가 없으면 자동으로 "취소"
                # 처리해버려서, 장바구니 페이지로 못 넘어가고 계속 상품페이지에 남아있었다.
                # 흐름을 앞으로 진행시키는 게 목적이니 모든 팝업을 항상 "확인"으로 수락한다.
                # (핸들러가 sync 람다면 dialog.accept()의 코루틴이 await 안 돼서 조용히
                # 아무 일도 안 일어나므로, 반드시 async 함수로 등록해야 한다.)
                async def _accept_dialog(dialog):
                    await dialog.accept()

                page.on("dialog", _accept_dialog)

                try:
                    await self._goto_product_and_select_size(page, style_code, size)
                    await self._proceed_to_checkout(page)
                    await self._fill_shipping_fields(page, shipping_info)
                    return await self._complete_payment(page, confirm_final_payment)
                except Exception:
                    if self._pause_on_error:
                        print(f"\n실패한 화면에서 멈췄습니다 — 지금 뜬 브라우저 창을 직접 보고 캡처하세요 (URL: {page.url}).")
                        await asyncio.to_thread(input, "확인했으면 Enter를 눌러 창을 닫으세요 >>> ")
                    raise
            finally:
                await browser.close()

    async def _goto_product_and_select_size(self, page, style_code: str, size: str) -> None:
        """PRD 5.2-2: 품번 상세 URL로 이동해 사이즈를 선택하고 장바구니에 담는다.

        검색 URL(`/display/search-word/result?searchWord=`)은 2026-09-21 실사이트
        devtools로 확인됨 — 상품상세 URL은 `/product?prdtNo=N`(랭킹 목록에서 검증됨)과
        `/product/new?prdtNo=N`(검색 결과에서 클릭 시 확인됨) 두 가지가 섞여 있는데,
        둘 다 SELECTOR_PRODUCT_LINK 링크를 그대로 따라가기만 하면 되므로 코드에서
        URL 패턴을 직접 조립할 필요는 없다.
        """
        search_url = f"https://abcmart.a-rt.com/display/search-word/result?searchWord={style_code}&channel=10001"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

        from app.integrations.scrapers.abc_mart import SELECTOR_PRODUCT_LINK

        await page.wait_for_selector(SELECTOR_PRODUCT_LINK, state="attached", timeout=10000)
        await page.locator(SELECTOR_PRODUCT_LINK).first.click()
        await page.wait_for_load_state("domcontentloaded")

        # 2026-09-21 실사이트 devtools로 확인됨: <ul class="size-list"><li>
        # <button class="btn-prod-size ...">260</button></li></ul> 구조가 abc_mart.py에서
        # 검증된 것과 동일하다. 다만 이 상품정보는 페이지 로딩 후 XHR로 뒤늦게 채워지므로,
        # 버튼이 DOM에 나타날 때까지 먼저 기다려야 한다 — 안 그러면 아직 안 채워진 빈 화면을
        # 보고 "사이즈가 없다"고 잘못 판단하게 된다.
        await page.wait_for_selector(SELECTOR_SIZE_OPTIONS, state="attached", timeout=10000)
        size_options = page.locator(SELECTOR_SIZE_OPTIONS)
        size_button = size_options.filter(has_text=re.compile(rf"^\s*{re.escape(size)}\s*$"))
        count = await size_button.count()
        if count == 0:
            raise RPAPurchaseError(f"사이즈 '{size}' 버튼을 찾지 못했습니다 (품절이거나 선택자가 바뀌었을 수 있음).")
        await size_button.first.click()

        # "바로구매"를 최우선으로 시도한다 — 성공하면 장바구니를 거치지 않고 바로 주문서
        # (/order) 화면으로 넘어간다. wait_for_url은 실제 브라우저 네비게이션뿐 아니라
        # SPA 방식의 클라이언트 라우팅(주소만 바뀌는 경우)도 잡아내므로, 팝업 뒤에 이어지는
        # 화면 전환이 정확히 "완전한 페이지 로드"가 아니어도 안전하게 기다릴 수 있다.
        for label in BUY_NOW_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                await button.first.click()
                await page.wait_for_url(re.compile(r".*/order.*"), timeout=15000)
                return

        # 폴백: "바로구매"가 없으면 장바구니 담기 → confirm() 팝업 자동수락 → 장바구니
        # 페이지(/cart/cart-list) 순서로 진행한다 (2026-09-21 devtools로 확인된 흐름).
        for label in ADD_TO_CART_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                await button.first.click()
                await page.wait_for_url(re.compile(r".*/cart.*"), timeout=15000)
                return
        raise RPAPurchaseError(
            f"바로구매/장바구니 담기 버튼을 찾지 못했습니다 "
            f"(시도한 문구: {BUY_NOW_BUTTON_TEXTS + ADD_TO_CART_BUTTON_TEXTS})."
        )

    async def _proceed_to_checkout(self, page) -> None:
        """장바구니에서 체크아웃(주문서 작성) 페이지로 이동한다.

        "바로구매" 경로를 탔다면 이미 /order 페이지에 도착해 있으므로 할 일이 없다 —
        _goto_product_and_select_size가 장바구니 폴백 경로를 탔을 때만 실제로 버튼을 찾는다.
        """
        if "/order" in page.url:
            return

        for label in CHECKOUT_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                await button.first.click()
                await page.wait_for_url(re.compile(r".*/order.*"), timeout=15000)
                return
        raise RPAPurchaseError(f"체크아웃(주문하기) 버튼을 찾지 못했습니다 (시도한 문구: {CHECKOUT_BUTTON_TEXTS}).")

    async def _fill_shipping_fields(self, page, shipping_info: ShippingInfo) -> None:
        """PRD 5.2-3: 고객 배송지 필드를 사람처럼 자연스러운 속도로 입력한다.

        2026-09-21 실사이트(비로그인 상태) devtools로 확인됨: 주문서(/order) 페이지의
        "배송 정보" 섹션은 기본값이 "주문자와 동일"이라, 그대로 두면 실제 고객이 아니라
        ABC마트 계정 소유자 본인 정보로 배송지가 채워지는 심각한 오류가 난다 — 반드시
        "신규입력"으로 바꾼 뒤 실제 수령인 정보를 입력해야 한다.

        2026-09-21 실사이트(로그인 상태) devtools로 확인됨: 이 폼은 `<table class="tbl-form">`
        구조라, 칸 이름("이름", "휴대폰번호")이 `<label>` 태그로 입력칸과 연결돼 있지 않고
        그냥 `<th>` 텍스트다 — 그래서 label 기준 검색(get_by_label)은 일부만 우연히 맞고
        (이름) 일부는 못 찾는다(휴대폰번호). 대신 "그 텍스트가 있는 행(tr) 안에서 input을
        찾는" 방식으로 통일한다 — abc_mart.py에서 검증된 사이즈버튼도 같은 tbl-form 구조 안에
        있었다. 같은 라벨이 위쪽 "주문 고객정보" 섹션에도 있어 여러 행이 매칭될 수 있으므로,
        나중에 나오는(= "배송 정보" 섹션의) 행을 우선한다.

        주소(우편번호 찾기 팝업)는 아직 실사이트 구조 미확인 — 팝업/iframe 내부 선택자를
        모르는 채로 잘못 클릭하면 엉뚱한 주소가 들어갈 수 있어, 여기서는 시도하지 않고
        명확한 에러로 멈춘다(안전).
        """
        new_address_radio = page.get_by_text("신규입력", exact=False)
        if await new_address_radio.count() > 0:
            await new_address_radio.first.click()

        delay = random.randint(*TYPING_DELAY_MS_RANGE)
        field_labels = {
            "recipient_name": ["이름", "수령인", "받는\\s*사람"],
            "recipient_phone": ["휴대폰번호", "연락처", "휴대폰\\s*번호"],
        }
        field_values = {
            "recipient_name": shipping_info.recipient_name,
            "recipient_phone": shipping_info.recipient_phone,
        }
        for field_key, candidates in field_labels.items():
            value = field_values[field_key]
            filled = False
            for label_pattern in candidates:
                row = page.locator("tr").filter(has_text=re.compile(label_pattern))
                if await row.count() == 0:
                    continue
                # 같은 라벨이 위쪽 "주문 고객정보" 섹션에도 있을 수 있어, 나중에 나오는
                # (= "배송 정보" 섹션의) 행을 우선한다.
                target = row.last.locator("input").first
                if await target.count() == 0:
                    continue
                await target.click()
                await target.fill("")
                await target.type(value, delay=delay)
                filled = True
                break
            if not filled:
                raise RPAPurchaseError(
                    f"배송지 입력칸을 찾지 못했습니다 ({field_key}, 시도한 라벨: {candidates}) — "
                    "화면을 캡처해서 실제 라벨 문구를 확인해야 합니다."
                )

        # 주소는 "우편번호 찾기" 팝업 내부 구조가 미확인이라, 안전하게 여기서 멈춘다.
        zipcode_button = page.get_by_text("우편번호 찾기", exact=False)
        if await zipcode_button.count() == 0:
            raise RPAPurchaseError("'우편번호 찾기' 버튼을 찾지 못했습니다 — 주소 입력 UI 구조를 다시 확인해야 합니다.")
        raise RPAPurchaseError(
            "주소 입력은 '우편번호 찾기' 팝업을 통해서만 가능한 것으로 보이는데, 그 팝업 내부 "
            "구조가 아직 확인되지 않았습니다 — 이름/연락처까지는 입력했습니다. 지금 뜬 화면에서 "
            "'우편번호 찾기'를 직접 눌러 나오는 팝업을 캡처해서 보내주세요."
        )

    async def _complete_payment(self, page, confirm_final_payment: bool = False) -> str:
        """PRD 5.2-4: 원클릭 간편결제/예치금/가상계좌 중 사전에 등록해둔 결제수단을 선택하고,
        `confirm_final_payment=True`일 때만 실제 결제 버튼을 눌러 완료한다. 카드 원본 번호는
        절대 다루지 않는다 — 소싱처 사이트에 이미 등록된 결제수단을 클릭하는 것까지만 한다.
        실사이트 미검증.
        """
        for label in PAYMENT_METHOD_SECTION_TEXTS:
            section = page.get_by_text(label, exact=False)
            if await section.count() > 0:
                await section.first.scroll_into_view_if_needed()
                break

        final_button = None
        for label in FINAL_PAYMENT_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                final_button = button.first
                break

        if not confirm_final_payment:
            timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            return f"{REVIEW_ONLY_PREFIX}-{timestamp}"

        if final_button is None:
            raise RPAPurchaseError(f"최종 결제 버튼을 찾지 못했습니다 (시도한 문구: {FINAL_PAYMENT_BUTTON_TEXTS}).")

        await final_button.click()
        await page.wait_for_load_state("domcontentloaded")

        body_text = await page.locator("body").inner_text(timeout=5000)
        match = ORDER_NUMBER_LABEL_PATTERN.search(body_text)
        if match:
            return match.group(1)
        raise RPAPurchaseError("결제 버튼은 눌렀지만 주문완료 화면에서 주문번호를 찾지 못했습니다 — 직접 확인이 필요합니다.")
