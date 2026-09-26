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
from app.integrations.rpa.base import (
    REVIEW_ONLY_PREFIX,
    BaseRPAClient,
    RPAPurchaseError,
    ShippingInfo,
    TrackingInfo,
    validate_shipping_info,
)
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs

# PRD 6.1: 마이페이지 주문내역 화면에서 택배사명/운송장번호가 보통 이런 문구 근처에
# 표시된다 — 실사이트 미검증(최선의 추정), scripts/test_tracking_lookup.py로 검증 필요.
ORDER_HISTORY_URL = "https://abcmart.a-rt.com/mypage/order/list"
TRACKING_NUMBER_LABEL_PATTERN = re.compile(r"송장\s*번호\s*[:：]?\s*([A-Za-z0-9-]+)")
COURIER_NAME_LABEL_PATTERN = re.compile(r"(CJ\s*대한통운|한진택배|롯데택배|우체국택배|로젠택배)")
# ABC마트가 실제로 쓰는 택배사명 → 쿠팡 deliveryCompanyCode 매핑. CJ대한통운만 이
# 코드베이스 다른 곳(쿠팡 상품등록 deliveryCompanyCode 기본값)에서 이미 "CJGLS"로
# 쓰고 있어 그대로 재사용한다 — 나머지는 실API 미검증 추정값.
COURIER_NAME_TO_CODE = {
    "CJ대한통운": "CJGLS",
    "한진택배": "HANJIN",
    "롯데택배": "LOTTE",
    "우체국택배": "EPOST",
    "로젠택배": "LOGEN",
}

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


class MockRPAClient(BaseRPAClient):
    """실제 사이트 호출 없이, 배송지 검증 로직과 발주 성공 흐름만 재현하는 Mock 구현."""

    async def purchase_order(
        self, style_code: str, size: str, shipping_info: ShippingInfo, confirm_final_payment: bool = False
    ) -> str:
        # Mock은 실제 결제가 없으니 confirm_final_payment 값과 무관하게 항상 성공 처리한다.
        validate_shipping_info(shipping_info)
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        return f"MOCK-{style_code}-{size}-{timestamp}-{random.randint(1000, 9999)}"

    async def fetch_tracking_info(self, source_order_id: str) -> TrackingInfo | None:
        # Mock은 항상 발송 완료 상태로 가정하고 가상의 운송장 정보를 돌려준다.
        return TrackingInfo(
            courier_name="CJ대한통운",
            courier_code="CJGLS",
            tracking_no=f"MOCK-TRACK-{abs(hash(source_order_id)) % 10**12:012d}",
        )


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
        # PRD 9.1: 실제 결제가 완료된 순간의 주문완료 화면을 정식 구매 증빙으로 남겨둔다.
        self._last_receipt_screenshot: bytes | None = None

    def get_last_receipt_screenshot(self) -> bytes | None:
        return self._last_receipt_screenshot

    async def purchase_order(
        self, style_code: str, size: str, shipping_info: ShippingInfo, confirm_final_payment: bool = False
    ) -> str:
        validate_shipping_info(shipping_info)
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
                except Exception as exc:
                    if self._pause_on_error:
                        print(f"\n실패 원인: {exc}", flush=True)
                        print(f"실패한 화면에서 멈췄습니다 — 지금 뜬 브라우저 창을 직접 보고 캡처하세요 (URL: {page.url}).", flush=True)
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
                rows = page.locator("tr").filter(has_text=re.compile(label_pattern))
                row_count = await rows.count()
                print(f"  [진단] '{field_key}' 후보 라벨 '{label_pattern}' → 매칭된 행 {row_count}개", flush=True)
                # 2026-09-21 devtools+실행 로그로 확인됨: "이름"/"휴대폰번호" 같은 라벨이
                # 위쪽 "주문 고객정보"(계정 본인 정보) 섹션과 아래쪽 "배송 정보"(실제 수령인)
                # 섹션 둘 다에 있고, 두 섹션 다 동시에 화면에 "보이는(visible)" 상태다 —
                # "처음 보이는 행"을 고르면 엉뚱하게 주문 고객정보(본인 정보) 칸에 채워진다.
                # "배송 정보" 섹션이 페이지 아래쪽(= DOM 뒤쪽)에 있으므로, 보이는 행들 중
                # 가장 나중(마지막) 것을 골라야 한다. (그 외에 배송수단별로 미리 만들어두고
                # style="display:none"으로 숨겨둔 행들도 섞여 있어 visible 체크 자체는 계속 필요.)
                target = None
                for i in range(row_count):
                    candidate_input = rows.nth(i).locator("input").first
                    input_count = await candidate_input.count()
                    is_visible = await candidate_input.is_visible() if input_count > 0 else False
                    print(f"    - 행 {i}: input {input_count}개, visible={is_visible}", flush=True)
                    if input_count > 0 and is_visible:
                        target = candidate_input  # break하지 않고 계속 진행해 "마지막" 것을 남긴다.
                if target is None:
                    continue
                await target.click()
                await target.fill("")
                await target.type(value, delay=delay)
                actual_value = await target.input_value()
                print(f"  [진단] '{field_key}' 입력 시도 후 실제 값: {actual_value!r} (기대값: {value!r})", flush=True)
                if actual_value != value:
                    continue
                filled = True
                break
            if not filled:
                raise RPAPurchaseError(
                    f"배송지 입력칸을 찾지 못했습니다 ({field_key}, 시도한 라벨: {candidates}) — "
                    "화면을 캡처해서 실제 라벨 문구를 확인해야 합니다."
                )

        # 2026-09-21 실사이트 확인됨: "우편번호 찾기"는 새 팝업 창으로 카카오(다음) 우편번호
        # 서비스 표준 위젯을 띄운다 — 여러 사이트에서 공통으로 쓰는 잘 알려진 위젯이라
        # 어느 정도 예측 가능한 구조지만, 정확한 결과 목록 선택자는 실사이트 미검증이다.
        zipcode_button = page.get_by_text("우편번호 찾기", exact=False)
        if await zipcode_button.count() == 0:
            raise RPAPurchaseError("'우편번호 찾기' 버튼을 찾지 못했습니다 — 주소 입력 UI 구조를 다시 확인해야 합니다.")

        # 사람이 직접 누르면 바로 뜨는 팝업이, 자동화가 누르면 안 뜨는 경우가 있었다(클릭
        # 타이밍/이벤트 차이로 팝업이 막히거나 사이트가 못 받는 경우) — 클릭 전에 잠깐
        # 화면이 안정되길 기다리고, 안 뜨면 한 번 더 시도한다.
        popup = None
        for attempt in range(2):
            await page.wait_for_timeout(500)
            try:
                async with page.context.expect_page(timeout=15000) as popup_info:
                    await zipcode_button.first.click()
                popup = await popup_info.value
                break
            except Exception:
                print(f"  [진단] 우편번호 팝업 대기 {attempt + 1}번째 시도 실패, 재시도합니다.", flush=True)
        if popup is None:
            raise RPAPurchaseError(
                "'우편번호 찾기'를 눌러도 팝업 창이 뜨지 않았습니다 — 자동화 클릭과 실제 클릭의 "
                "차이(팝업 차단 등)일 수 있습니다. 지금 뜬 화면에서 직접 눌러보고 무슨 일이 "
                "일어나는지(새 창/화면 안 팝업 등) 확인해 주세요."
            )
        await popup.wait_for_load_state("domcontentloaded")

        # 카카오 우편번호 서비스는 팝업 창 안에 실제 검색 위젯을 iframe으로 한 번 더
        # 감싸서 넣는 경우가 있다 — popup 최상위에서 못 찾으면 iframe들도 뒤져본다.
        search_selector = "input[type='text'], input[type='search'], input:not([type])"
        search_frame = popup.main_frame
        if await search_frame.locator(search_selector).count() == 0:
            for frame in popup.frames:
                if await frame.locator(search_selector).count() > 0:
                    search_frame = frame
                    break
            else:
                raise RPAPurchaseError(
                    "우편번호 검색창을 팝업 안에서 찾지 못했습니다 (iframe 구조 포함 확인) — "
                    "팝업 화면을 캡처해서 확인해야 합니다."
                )

        search_input = search_frame.locator(search_selector).first
        # 2026-09-21 실행 로그로 확인됨: placeholder 안내문구가 스타일용 <span>으로
        # input 위에 겹쳐 있어(포커스 전까지 보여주는 장식) 일반 click()의 "클릭 지점이
        # 가려져 있지 않은지" 검사에서 계속 막힌다 — force=True로 그 검사를 건너뛴다.
        await search_input.click(force=True)
        await search_input.type(shipping_info.shipping_addr, delay=delay)
        await search_input.press("Enter")

        # 검색 결과 목록에서 첫 번째(가장 유사도 높은) 항목을 클릭한다 — 고르면 팝업이
        # 자동으로 닫히고 우편번호/도로명주소가 원래 화면에 채워지는 게 표준 동작이다.
        result_item = search_frame.locator("li, tr").filter(has_text=re.compile(r"\d"))
        try:
            await result_item.first.wait_for(state="visible", timeout=10000)
        except Exception as exc:
            raise RPAPurchaseError(
                f"우편번호 검색 결과를 찾지 못했습니다 (검색어: {shipping_info.shipping_addr!r}) — "
                "팝업 화면을 캡처해서 실제 결과 목록 구조를 확인해야 합니다."
            ) from exc
        await result_item.first.click()

        # 2026-09-21 실행 로그로 확인됨: "tr을 '주소' 텍스트로 찾기"는 이제 다른 행과
        # 헷갈려서(우편번호/주소1/주소2가 서로 다른 행에 나뉘어 있는 걸로 보임) 안정적이지
        # 않다. 대신 "우편번호 찾기" 버튼(유일하게 존재하는 확실한 기준점)이 속한 행과,
        # 그 바로 다음에 오는 형제 행들에서 입력칸을 찾는 방식으로 바꾼다.
        zip_button_row = zipcode_button.locator("xpath=ancestor::tr[1]")
        zip_input = zip_button_row.locator("input").first
        for _ in range(20):
            if (await zip_input.input_value()).strip():
                break
            await page.wait_for_timeout(300)
        zip_value = await zip_input.input_value()
        print(f"  [진단] 우편번호 검색 후 값: {zip_value!r}", flush=True)
        if not zip_value.strip():
            raise RPAPurchaseError("우편번호 검색은 진행했지만 결과가 원래 화면에 채워지지 않았습니다 — 화면을 캡처해서 확인해야 합니다.")

        # 상세주소(동/호수 등)를 별도로 구분해서 받지 않으므로, "우편번호 찾기" 행 바로 다음
        # 형제 행들 중 아직 비어있는 텍스트 입력칸에 원본 주소 전체를 한 번 더 넣어 정보
        # 누락을 막는다 (중복되더라도 배송기사 입장에서는 무해함).
        following_inputs = zip_button_row.locator("xpath=following-sibling::tr[position()<=2]//input[@type='text']")
        following_count = await following_inputs.count()
        for i in range(following_count):
            candidate = following_inputs.nth(i)
            if (await candidate.input_value()) == "":
                await candidate.click(force=True)
                await candidate.type(shipping_info.shipping_addr, delay=delay)
                break

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
            # PRD 9.1: 실제로 돈이 나간 순간의 주문완료 화면(가격/상품/주문번호가 보이는
            # 상태)을 정식 구매 증빙으로 캡처해둔다 — 지재권 분쟁/신고 대응용.
            try:
                self._last_receipt_screenshot = await page.screenshot(full_page=True)
            except Exception as exc:  # noqa: BLE001 - 증빙 캡처 실패가 결제 성공 자체를 실패로 만들면 안 된다.
                print(f"  결제완료 화면 캡처 실패({exc}) — 결제는 정상 완료된 상태로 계속 진행합니다.", flush=True)
            return match.group(1)
        raise RPAPurchaseError("결제 버튼은 눌렀지만 주문완료 화면에서 주문번호를 찾지 못했습니다 — 직접 확인이 필요합니다.")

    async def fetch_tracking_info(self, source_order_id: str) -> TrackingInfo | None:
        """PRD 6.1: 마이페이지 주문내역에서 이 주문의 택배사명/운송장번호를 찾는다.

        실사이트 미검증 — `_complete_payment`가 최종 결제까지 실행됐을 때만 돌려주는
        진짜 ABC마트 주문번호(`source_order_id`, "REVIEW_ONLY-..." 접두어가 아닌 값)를
        받아서, 그 주문번호가 적힌 행을 찾아 택배사명/운송장번호를 읽는다. 아직 소싱처가
        발송 준비 중이라 운송장이 안 뜬 상태면 None을 돌려준다 — 호출부가 나중에 다시
        폴링해야 한다는 뜻이다.
        """
        if source_order_id.startswith(REVIEW_ONLY_PREFIX):
            raise RPAPurchaseError(
                f"'{source_order_id}'는 실제 결제가 완료된 주문번호가 아닙니다(최종 결제 직전에 "
                "멈춘 상태) — 운송장을 조회할 수 없습니다."
            )
        if not self._session_cookies:
            raise RPAPurchaseError(
                "소싱처 로그인 세션(쿠키)이 없습니다 — scripts/save_abc_mart_session.py로 먼저 "
                "로그인 세션을 저장해주세요."
            )

        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self._headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                await context.add_cookies(self._session_cookies)
                page = await context.new_page()

                try:
                    await page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=20000)

                    order_row = page.locator("tr, li, div").filter(has_text=source_order_id)
                    row_count = await order_row.count()
                    if row_count == 0:
                        raise RPAPurchaseError(
                            f"마이페이지 주문내역에서 주문번호 '{source_order_id}'를 찾지 못했습니다 — "
                            "화면을 캡처해서 실제 주문내역 화면 구조를 확인해야 합니다."
                        )

                    row_text = await order_row.first.inner_text()

                    tracking_match = TRACKING_NUMBER_LABEL_PATTERN.search(row_text)
                    if not tracking_match:
                        # 아직 소싱처에서 발송 준비 중이라 운송장이 안 나온 상태일 수 있다 —
                        # 이건 에러가 아니라 "나중에 다시 확인" 신호다.
                        return None

                    courier_match = COURIER_NAME_LABEL_PATTERN.search(row_text)
                    if not courier_match:
                        raise RPAPurchaseError(
                            f"운송장번호({tracking_match.group(1)})는 찾았지만 택배사명을 찾지 "
                            f"못했습니다 — 화면 문구를 확인해야 합니다 (행 텍스트: {row_text!r})."
                        )

                    courier_name = courier_match.group(1).replace(" ", "")
                    courier_code = COURIER_NAME_TO_CODE.get(courier_name)
                    if courier_code is None:
                        raise RPAPurchaseError(
                            f"택배사 '{courier_name}'에 대응하는 쿠팡 deliveryCompanyCode를 모릅니다 — "
                            "COURIER_NAME_TO_CODE에 추가해야 합니다."
                        )

                    return TrackingInfo(
                        courier_name=courier_name,
                        courier_code=courier_code,
                        tracking_no=tracking_match.group(1),
                    )
                except Exception as exc:
                    if self._pause_on_error:
                        print(f"\n실패 원인: {exc}", flush=True)
                        print(f"실패한 화면에서 멈췄습니다 — 지금 뜬 브라우저 창을 직접 보고 캡처하세요 (URL: {page.url}).", flush=True)
                        await asyncio.to_thread(input, "확인했으면 Enter를 눌러 창을 닫으세요 >>> ")
                    raise
            finally:
                await browser.close()
