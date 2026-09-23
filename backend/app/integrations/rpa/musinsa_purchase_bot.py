"""무신사 무인 발주 RPA 클라이언트 스켈레톤 (실사이트 미검증).

2026-09-23: abc_mart.py용으로 이미 검증된 안전장치(USE_MOCK_RPA, confirm_final_payment)
구조를 그대로 따르되, 버튼 문구/URL/셀렉터는 전부 아직 실제 무신사 화면으로 확인 전인
"최선의 추정"이다. purchase_bot.py의 PlaywrightRPAClient가 처음 만들어졌을 때와 같은
상태 — 실제로 쓰기 전에는 반드시 아래 두 가지를 해야 한다:

  1. 실제 상품에 SourceMapping(source_platform=MUSINSA)을 등록해 이 클라이언트가
     실제로 호출되게 만들기 전까지는, 이 코드가 존재해도 실제 주문 처리에 전혀
     영향이 없다 (find_cheapest_source가 무신사를 후보로 고려하지 않기 때문).
  2. scripts/test_rpa_checkout.py 같은 진단 스크립트로 headless=False로 실행해
     실제 화면을 보면서, 멈추는 지점마다 SELECTOR_*/버튼 문구를 실제 화면 기준으로
     고쳐야 한다 (abc_mart RPA 검증 때와 동일한 절차).

이중 안전장치는 abc_mart RPA와 동일하게 적용된다:
  - USE_MOCK_RPA=true(기본값)면 이 클래스 자체가 호출되지 않는다.
  - confirm_final_payment=False(기본값)면 최종 결제 버튼은 누르지 않는다.
"""

import re

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

# 실사이트 미검증(최선의 추정) — 무신사 마이페이지 주문내역 URL.
ORDER_HISTORY_URL = "https://my.musinsa.com/order/list"
TRACKING_NUMBER_LABEL_PATTERN = re.compile(r"송장\s*번호\s*[:：]?\s*([A-Za-z0-9-]+)")
COURIER_NAME_LABEL_PATTERN = re.compile(r"(CJ\s*대한통운|한진택배|롯데택배|우체국택배|로젠택배)")
COURIER_NAME_TO_CODE = {
    "CJ대한통운": "CJGLS",
    "한진택배": "HANJIN",
    "롯데택배": "LOTTE",
    "우체국택배": "EPOST",
    "로젠택배": "LOGEN",
}

TYPING_DELAY_MS_RANGE = (100, 150)

# 실사이트 미검증(최선의 추정) — 검색 URL 자체는 아직 확인 전.
SEARCH_URL_TEMPLATE = "https://www.musinsa.com/search/goods?keyword={query}"
# 2026-09-23 실사이트(musinsa.com 메인 추천 페이지) 실제 HTML로 확인됨 — 상품 상세
# URL은 항상 이 href 패턴 하나뿐이다 (scrapers/musinsa.py의 SELECTOR_PRODUCT_LINK와 동일 근거).
SELECTOR_PRODUCT_LINK = "a[href*='musinsa.com/products/']"
# 실사이트로 확인됨(2026-09-23, scrapers/musinsa.py와 동일 근거): 사이즈는 정적 버튼
# 목록이 아니라 "사이즈" placeholder를 가진 닫힌 드롭다운(input)이다 — 클릭해서 열어야
# 옵션이 DOM에 나타난다. "열린 상태"의 실제 화면은 아직 캡처 전이라, 연 다음 각 사이즈
# 항목을 읽는 SELECTOR_SIZE_OPTIONS는 여전히 최선의 추정이다.
SELECTOR_SIZE_DROPDOWN_TRIGGER = "input[data-mds='DropdownTriggerInput'][placeholder='사이즈']"
SELECTOR_SIZE_OPTIONS = ".size-option li, .option-size button, select[name='option'] option"
BUY_NOW_BUTTON_TEXTS = ["바로 구매", "바로구매"]
ADD_TO_CART_BUTTON_TEXTS = ["장바구니 담기", "장바구니"]
CHECKOUT_BUTTON_TEXTS = ["주문하기", "구매하기", "선택상품 주문"]
PAYMENT_METHOD_SECTION_TEXTS = ["결제수단", "결제 수단"]
FINAL_PAYMENT_BUTTON_TEXTS = ["결제하기", "최종결제", "결제 하기"]
ORDER_NUMBER_LABEL_PATTERN = re.compile(r"주문\s*번호\s*[:：]?\s*([A-Za-z0-9-]+)")


class MusinsaRPAClient(BaseRPAClient):
    """Headless Chromium(Playwright)으로 무신사에서 무인 결제를 수행한다 — 실사이트 미검증 스켈레톤.

    abc_mart.py의 PlaywrightRPAClient와 같은 구조(세션 쿠키 주입, 팝업 자동수락,
    바로구매 우선 시도 → 장바구니 폴백)를 그대로 따랐다. 실제 화면 구조가 확인되면
    이 파일의 셀렉터/URL만 고치면 되고, purchase_order/fetch_tracking_info의
    시그니처는 BaseRPAClient 인터페이스를 그대로 지킨다.
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
        self._pause_on_error = pause_on_error

    async def purchase_order(
        self, style_code: str, size: str, shipping_info: ShippingInfo, confirm_final_payment: bool = False
    ) -> str:
        validate_shipping_info(shipping_info)
        if not self._session_cookies:
            raise RPAPurchaseError(
                "무신사 로그인 세션(쿠키)이 없습니다 — 로그인 세션을 먼저 저장해주세요 "
                "(scripts/save_abc_mart_session.py를 참고해 무신사용으로 만들어야 함, 아직 미작성)."
            )

        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self._headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                await context.add_cookies(self._session_cookies)
                page = await context.new_page()

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
                        import asyncio

                        await asyncio.to_thread(input, "확인했으면 Enter를 눌러 창을 닫으세요 >>> ")
                    raise
            finally:
                await browser.close()

    async def _goto_product_and_select_size(self, page, style_code: str, size: str) -> None:
        from urllib.parse import quote

        search_url = SEARCH_URL_TEMPLATE.format(query=quote(style_code))
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

        await page.wait_for_selector(SELECTOR_PRODUCT_LINK, state="attached", timeout=10000)
        await page.locator(SELECTOR_PRODUCT_LINK).first.click()
        await page.wait_for_load_state("domcontentloaded")

        dropdown_trigger = page.locator(SELECTOR_SIZE_DROPDOWN_TRIGGER)
        if await dropdown_trigger.count() > 0:
            await dropdown_trigger.first.click()

        await page.wait_for_selector(SELECTOR_SIZE_OPTIONS, state="attached", timeout=10000)
        size_options = page.locator(SELECTOR_SIZE_OPTIONS)
        size_button = size_options.filter(has_text=re.compile(rf"^\s*{re.escape(size)}\s*$"))
        count = await size_button.count()
        if count == 0:
            raise RPAPurchaseError(f"사이즈 '{size}' 버튼을 찾지 못했습니다 (품절이거나 선택자가 바뀌었을 수 있음).")
        await size_button.first.click()

        for label in BUY_NOW_BUTTON_TEXTS:
            button = page.get_by_role("button", name=re.compile(re.escape(label))).or_(
                page.get_by_text(label, exact=False)
            )
            if await button.count() > 0:
                await button.first.click()
                await page.wait_for_url(re.compile(r".*/order.*"), timeout=15000)
                return

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
        """실사이트 미검증 — abc_mart.py의 "tr 안에서 input 찾기" 방식을 그대로 가정하고 짰다.

        실제 무신사 배송지 입력 폼 구조(라벨-input 연결 방식, 주소 검색 팝업 구조)는
        전혀 확인 전이라, 이 메서드는 실사이트 검증 시 가장 많이 고쳐야 할 가능성이 높다.
        """
        new_address_radio = page.get_by_text("신규입력", exact=False)
        if await new_address_radio.count() > 0:
            await new_address_radio.first.click()

        import random

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
                target = None
                for i in range(row_count):
                    candidate_input = rows.nth(i).locator("input").first
                    input_count = await candidate_input.count()
                    is_visible = await candidate_input.is_visible() if input_count > 0 else False
                    if input_count > 0 and is_visible:
                        target = candidate_input
                if target is None:
                    continue
                await target.click()
                await target.fill("")
                await target.type(value, delay=delay)
                if (await target.input_value()) != value:
                    continue
                filled = True
                break
            if not filled:
                raise RPAPurchaseError(
                    f"배송지 입력칸을 찾지 못했습니다 ({field_key}, 시도한 라벨: {candidates}) — "
                    "화면을 캡처해서 실제 라벨 문구를 확인해야 합니다."
                )

        # 주소 검색 팝업 구조는 완전히 미확인 — 잘못 클릭해 엉뚱한 주소가 들어가는 것을
        # 막기 위해, abc_mart.py와 같은 정책으로 여기서는 시도하지 않고 명확한 에러로 멈춘다.
        raise RPAPurchaseError(
            "무신사 주소 입력(우편번호 검색 팝업) 구조가 아직 확인되지 않아 여기서 멈춥니다 — "
            "실제 화면을 캡처해서 주소 검색 UI 구조를 확인한 뒤 이 메서드를 완성해야 합니다."
        )

    async def _complete_payment(self, page, confirm_final_payment: bool = False) -> str:
        from datetime import UTC, datetime

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

    async def fetch_tracking_info(self, source_order_id: str) -> TrackingInfo | None:
        if source_order_id.startswith(REVIEW_ONLY_PREFIX):
            raise RPAPurchaseError(
                f"'{source_order_id}'는 실제 결제가 완료된 주문번호가 아닙니다(최종 결제 직전에 "
                "멈춘 상태) — 운송장을 조회할 수 없습니다."
            )
        if not self._session_cookies:
            raise RPAPurchaseError("무신사 로그인 세션(쿠키)이 없습니다.")

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
                        return None

                    courier_match = COURIER_NAME_LABEL_PATTERN.search(row_text)
                    if not courier_match:
                        raise RPAPurchaseError(
                            f"운송장번호({tracking_match.group(1)})는 찾았지만 택배사명을 찾지 못했습니다."
                        )

                    courier_name = courier_match.group(1).replace(" ", "")
                    courier_code = COURIER_NAME_TO_CODE.get(courier_name)
                    if courier_code is None:
                        raise RPAPurchaseError(f"택배사 '{courier_name}'에 대응하는 쿠팡 deliveryCompanyCode를 모릅니다.")

                    return TrackingInfo(courier_name=courier_name, courier_code=courier_code, tracking_no=tracking_match.group(1))
                except Exception as exc:
                    if self._pause_on_error:
                        print(f"\n실패 원인: {exc}", flush=True)
                        import asyncio

                        await asyncio.to_thread(input, "확인했으면 Enter를 눌러 창을 닫으세요 >>> ")
                    raise
            finally:
                await browser.close()
