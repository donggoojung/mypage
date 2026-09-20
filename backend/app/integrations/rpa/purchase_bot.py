"""소싱처 무인 발주 RPA 클라이언트 (PRD 5.2).

주의 — 실사이트 미검증: 이 파일의 `PlaywrightRPAClient`는 abc_mart.py를 실제 devtools로
검증했던 것과 달리, 장바구니/체크아웃/결제 화면의 정확한 CSS 선택자를 아직 확인하지 못했다.
`_goto_product_and_select_size` / `_proceed_to_checkout` / `_fill_shipping_fields` /
`_complete_payment` 4개 메서드는 실제 소싱처 사이트를 F12로 열어 선택자를 채운 뒤에만
동작한다 (abc_mart.py의 스타일코드/사이즈 셀렉터를 찾았던 것과 같은 방식).

그 전까지는 반드시 `USE_MOCK_RPA=true`(기본값)로 두고 개발/테스트한다 — 실제 결제가
걸린 기능이라 미검증 상태로 실행하면 잘못된 사이즈 구매, 결제 실패 등 금전적 사고로
이어질 수 있다.
"""

import random
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.integrations.rpa.base import BaseRPAClient, ShippingInfo
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs

# PRD 5.2-3: 사람처럼 보이도록 타이핑 사이 지연을 준다.
TYPING_DELAY_MS_RANGE = (100, 150)


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

    async def purchase_order(self, style_code: str, size: str, shipping_info: ShippingInfo) -> str:
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

    async def purchase_order(self, style_code: str, size: str, shipping_info: ShippingInfo) -> str:
        _validate_shipping_info(shipping_info)
        if not self._session_cookies:
            raise RPAPurchaseError("소싱처 로그인 세션(쿠키)이 없습니다 — 세션 복원 없이는 발주를 진행할 수 없습니다.")

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
                return await self._complete_payment(page)
            finally:
                await browser.close()

    async def _goto_product_and_select_size(self, page, style_code: str, size: str) -> None:
        """PRD 5.2-2: 품번 상세 URL로 이동해 사이즈를 선택하고 장바구니에 담는다."""
        raise NotImplementedError(
            "실제 소싱처 상품상세/장바구니 선택자를 devtools로 확인한 뒤 구현해야 합니다 "
            "(abc_mart.py의 사이즈 버튼 선택자 확인 방식 참고)."
        )

    async def _proceed_to_checkout(self, page) -> None:
        """장바구니에서 체크아웃 페이지로 이동한다."""
        raise NotImplementedError("실제 체크아웃 진입 선택자 확인 후 구현.")

    async def _fill_shipping_fields(self, page, shipping_info: ShippingInfo) -> None:
        """PRD 5.2-3: 고객 배송지 필드를 사람처럼 자연스러운 속도로 입력한다."""
        raise NotImplementedError("실제 배송지 입력 폼 선택자 확인 후, page.type(selector, text, delay=...) 로 구현.")

    async def _complete_payment(self, page) -> str:
        """PRD 5.2-4: 원클릭 간편결제/예치금/가상계좌 중 하나로 무인 결제를 완료하고
        소싱처 주문번호를 반환한다. 카드 원본 번호는 절대 다루지 않는다 — 사전에
        소싱처 사이트에 등록해둔 결제수단을 선택/클릭하는 것까지만 자동화한다.
        """
        raise NotImplementedError("실제 결제 수단 선택 및 주문번호 파싱 선택자 확인 후 구현.")
