"""쿠팡 WING "자동가격조정" 자동 끄기 RPA.

쿠팡 오픈API는 "자동가격조정"(판매자 자동 가격 조정) 기능을 지원하지 않는다(쿠팡 공식
FAQ로 확인됨, 2026-09-22) — WING 화면에서 사람이 직접 꺼야 한다. 이 봇은 API로 상품을
등록한 직후, WING 화면에 로그인된 상태로 들어가 해당 상품의 자동가격조정을 대신 꺼준다.

주의 — 실사이트 미검증: `save_abc_mart_session.py`/`purchase_bot.py`와 같은 방식으로
"화면 문구 기반 최선의 추정 구현"이다. 반드시 `scripts/test_wing_price_lock.py`를
headless=False로 실행해 실제 화면에서 확인하고, 버튼을 못 찾으면 SELECTOR_*/문구를
실제 화면에 맞게 고쳐야 한다.

이중 안전장치:
  1. `USE_MOCK_RPA=true`(기본값)면 이 클래스 자체가 호출되지 않는다.
  2. 상품을 못 찾거나 토글을 못 찾으면 예외를 내며 멈춘다 — 엉뚱한 상품/버튼을 잘못
     누르는 것보다, 아무것도 안 하고 실패하는 쪽이 항상 더 안전하다.
"""

from app.core.config import Settings, get_settings
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs

WING_HOME_URL = "https://wing.coupang.com/"

# --- 아래 문구는 전부 실사이트 미검증(최선의 추정)이다. WING 메뉴 구조가 다르면
# scripts/test_wing_price_lock.py로 실제 화면 보고 고쳐야 한다.
PRODUCT_MANAGEMENT_MENU_TEXTS = ["상품관리"]
PRODUCT_SEARCH_MENU_TEXTS = ["상품 조회/수정", "상품조회/수정", "상품 조회 및 수정"]
AUTO_PRICE_TOGGLE_TEXT = "자동가격조정"
# 토글이 켜져있을 때(파란색/활성) aria-checked="true" 또는 클래스에 "on"/"active"가
# 붙는 게 일반적인 패턴이라 여러 후보를 시도한다 — 실제 구조는 화면 보고 확정해야 한다.
CONFIRM_DIALOG_BUTTON_TEXTS = ["확인", "끄기", "해제", "예"]


class WingPriceLockError(RuntimeError):
    """자동가격조정 끄기 RPA 진행 중(상품 못 찾음, 토글 못 찾음, 세션 만료 등) 발생한 오류."""


class WingPriceLockBot:
    """Mock/Real 겸용 — 등록 직후 자동가격조정을 끄는 봇."""

    def __init__(
        self,
        settings: Settings | None = None,
        use_mock: bool | None = None,
        session_cookies: list[dict] | None = None,
    ):
        self._settings = settings or get_settings()
        self._use_mock = self._settings.use_mock_rpa if use_mock is None else use_mock
        self._session_cookies = session_cookies or []

    async def disable_auto_price_adjustment(self, style_code: str, pause_on_error: bool = False) -> bool:
        """품번(style_code)으로 상품을 찾아 자동가격조정을 끈다.

        이미 꺼져있으면 그대로 True를 반환한다(멱등). Mock 모드면 항상 True.
        `pause_on_error=True`면 실패 직전 화면에서 멈춰서 눈으로 원인을 볼 수 있다
        (scripts/test_wing_price_lock.py 전용 디버그 옵션).
        """
        if self._use_mock:
            return True
        return await self._real_disable(style_code, pause_on_error=pause_on_error)

    async def _real_disable(self, style_code: str, pause_on_error: bool) -> bool:
        if not self._session_cookies:
            raise WingPriceLockError(
                "WING 로그인 세션이 없습니다. scripts/save_wing_session.py를 먼저 실행해 로그인 상태를 저장하세요."
            )

        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(headless=not pause_on_error))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                await context.add_cookies(self._session_cookies)
                page = await context.new_page()

                try:
                    return await self._toggle_off(page, style_code)
                except Exception:
                    if pause_on_error:
                        print(f"\n  [진단] 에러 발생 — 화면을 확인하세요. 현재 URL: {page.url}", flush=True)
                        import asyncio

                        await asyncio.to_thread(input, "  계속하려면(또는 창 닫으려면) Enter >>> ")
                    raise
            finally:
                await browser.close()

    async def _toggle_off(self, page, style_code: str) -> bool:
        print(f"  [진단] WING 홈으로 이동 중...", flush=True)
        await page.goto(WING_HOME_URL, wait_until="domcontentloaded", timeout=30000)

        print(f"  [진단] '상품관리' 메뉴 찾는 중...", flush=True)
        await self._click_first_matching_text(page, PRODUCT_MANAGEMENT_MENU_TEXTS)
        await page.wait_for_timeout(500)

        print(f"  [진단] '상품 조회/수정' 메뉴 찾는 중...", flush=True)
        await self._click_first_matching_text(page, PRODUCT_SEARCH_MENU_TEXTS)
        await page.wait_for_url("**/products**", timeout=15000)

        print(f"  [진단] 품번 '{style_code}'로 검색 중...", flush=True)
        search_box = page.get_by_placeholder("검색").or_(page.locator("input[type='search'], input[type='text']").first)
        await search_box.first.fill(style_code)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1500)

        print(f"  [진단] 검색결과에서 상품 행 펼치는 중...", flush=True)
        row = page.locator(f"tr:has-text('{style_code}'), div:has-text('{style_code}')").first
        expand_arrow = row.locator("svg, button, [class*='arrow'], [class*='toggle']").first
        await expand_arrow.click(timeout=10000)
        await page.wait_for_timeout(500)

        print(f"  [진단] '{AUTO_PRICE_TOGGLE_TEXT}' 토글 찾는 중...", flush=True)
        toggle = page.get_by_text(AUTO_PRICE_TOGGLE_TEXT).locator("xpath=following::*[contains(@class,'toggle') or contains(@class,'switch') or @role='switch'][1]")

        is_on = await self._is_toggle_on(toggle)
        if not is_on:
            print(f"  [진단] 이미 꺼져있습니다. 변경 없음.", flush=True)
            return True

        print(f"  [진단] 켜져있음 → 끄기 클릭", flush=True)
        await toggle.click(timeout=10000)
        await page.wait_for_timeout(500)

        for text in CONFIRM_DIALOG_BUTTON_TEXTS:
            confirm_btn = page.get_by_role("button", name=text)
            if await confirm_btn.count() > 0 and await confirm_btn.first.is_visible():
                print(f"  [진단] 확인 팝업 '{text}' 클릭", flush=True)
                await confirm_btn.first.click(timeout=5000)
                break

        await page.wait_for_timeout(500)
        still_on = await self._is_toggle_on(toggle)
        if still_on:
            raise WingPriceLockError(f"자동가격조정을 껐는데도 여전히 켜진 상태로 보입니다 (style_code={style_code}).")
        print(f"  [진단] 자동가격조정 끄기 완료", flush=True)
        return True

    @staticmethod
    async def _click_first_matching_text(page, texts: list[str]) -> None:
        for text in texts:
            locator = page.get_by_text(text, exact=False)
            if await locator.count() > 0:
                await locator.first.click(timeout=10000)
                return
        raise WingPriceLockError(f"다음 문구 중 아무것도 화면에서 못 찾았습니다: {texts}")

    @staticmethod
    async def _is_toggle_on(toggle) -> bool:
        try:
            aria_checked = await toggle.get_attribute("aria-checked")
            if aria_checked is not None:
                return aria_checked == "true"
            class_name = await toggle.get_attribute("class") or ""
            return "on" in class_name.lower() or "active" in class_name.lower() or "checked" in class_name.lower()
        except Exception:
            return False
