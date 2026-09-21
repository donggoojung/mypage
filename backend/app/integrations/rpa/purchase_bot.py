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
ADD_TO_CART_BUTTON_TEXTS = ["장바구니 담기", "장바구니", "바로구매", "바로 구매"]
CHECKOUT_BUTTON_TEXTS = ["주문하기", "구매하기", "선택상품 주문"]
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
    ):
        self._source_base_url = source_base_url.rstrip("/")
        self._session_cookies = session_cookies
        self._settings = settings or get_settings()
        self._headless = headless

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

                await self._goto_product_and_select_size(page, style_code, size)
                await self._proceed_to_checkout(page)
                await self._fill_shipping_fields(page, shipping_info)
                return await self._complete_payment(page, confirm_final_payment)
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

        for label in ADD_TO_CART_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                await button.first.click()
                return
        raise RPAPurchaseError(f"장바구니 담기 버튼을 찾지 못했습니다 (시도한 문구: {ADD_TO_CART_BUTTON_TEXTS}).")

    async def _proceed_to_checkout(self, page) -> None:
        """장바구니에서 체크아웃 페이지로 이동한다. 실사이트 미검증."""
        for label in CHECKOUT_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                await button.first.click()
                await page.wait_for_load_state("domcontentloaded")
                return
        raise RPAPurchaseError(f"체크아웃(주문하기) 버튼을 찾지 못했습니다 (시도한 문구: {CHECKOUT_BUTTON_TEXTS}).")

    async def _fill_shipping_fields(self, page, shipping_info: ShippingInfo) -> None:
        """PRD 5.2-3: 고객 배송지 필드를 사람처럼 자연스러운 속도로 입력한다. 실사이트 미검증 —
        라벨 텍스트(수령인/연락처/주소)로 입력칸을 찾는 방식이라, 실제 라벨 문구가 다르면
        입력에 실패한다(이 경우 아무것도 안 눌렸으니 안전하게 멈춘다).
        """
        delay = random.randint(*TYPING_DELAY_MS_RANGE)
        field_values = {
            "수령인": shipping_info.recipient_name,
            "연락처": shipping_info.recipient_phone,
            "주소": shipping_info.shipping_addr,
        }
        for label, value in field_values.items():
            field = page.get_by_label(re.compile(re.escape(label)))
            if await field.count() == 0:
                raise RPAPurchaseError(f"배송지 입력칸 '{label}'을(를) 찾지 못했습니다 (라벨 문구가 다를 수 있음).")
            await field.first.click()
            await field.first.type(value, delay=delay)

        if shipping_info.shipping_message:
            message_field = page.get_by_label(re.compile("배송\\s*메모|요청사항"))
            if await message_field.count() > 0:
                await message_field.first.click()
                await message_field.first.type(shipping_info.shipping_message, delay=delay)

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
