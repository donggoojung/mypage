"""ABC마트 상품 상세페이지 실크롤러 (PRD 2.1, 2.2).

주의 — 셀렉터 미검증 (2026-09-18 기준):
이 코드는 abcmart.com으로의 아웃바운드 네트워크 접속이 차단된 환경에서 작성되어,
실제 페이지 구조로 CSS 셀렉터를 검증하지 못했다. 아래 두 단계로 데이터를 추출한다.

  1순위: `<script type="application/ld+json">`에 담긴 schema.org Product 구조화 데이터.
         (국내 쇼핑몰 다수가 검색엔진 노출을 위해 이 형식을 사용하며, 사이트 리뉴얼에도
         비교적 안정적이라 우선 시도한다.)
  2순위: 1순위가 없을 때 DOM 셀렉터(SELECTOR_*)로 폴백. 이 상수들은 실사이트에서
         브라우저 개발자도구(F12) → Elements 탭으로 정확한 값을 확인해 채워야 한다.

사용자 PC(로컬)에서 `python scripts/test_scraper_margin.py <실제 상품 URL>` 로 실행해보고,
파싱이 비어 나오면 SELECTOR_* 값을 실제 사이트에 맞게 수정한다.
"""

import asyncio
import json
import os
import random
import re

from playwright.async_api import Page, async_playwright

from app.integrations.scrapers.base import BaseScraper, ScrapedProduct

# 이 환경에는 Playwright 브라우저가 /opt/pw-browsers 에 사전 설치되어 있다.
# `playwright install`로 재다운로드하지 않고, 있으면 그 경로를 그대로 사용한다.
_PREINSTALLED_CHROMIUM = "/opt/pw-browsers/chromium"

# 실제 데스크톱 크롬 UA — 최신 버전 번호는 주기적으로 갱신 필요.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# PRD 2.2: navigator.webdriver 등 헤드리스 탐지 핵심 파라미터 제거.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""

# 나이키 CW2288-111 형태(영문 1~3자 + 숫자 3~4자리 - 숫자 2~5자리)의 품번 정규식 (PRD 2.1).
STYLE_CODE_PATTERN = re.compile(r"\b[A-Z]{1,3}\d{3,4}-\d{2,5}\b")

# --- TODO: 아래 셀렉터는 실사이트 검증이 필요한 플레이스홀더다 --------------------
SELECTOR_JSON_LD = 'script[type="application/ld+json"]'
SELECTOR_BRAND = ".prod-brand, .brand-name"
SELECTOR_PRODUCT_NAME = ".prod-name, .goods-name, h1"
SELECTOR_PRICE = ".prod-price .price, .sale-price"
SELECTOR_SIZE_OPTIONS = ".size-option li, .option-size li, [data-option-type='size'] li"
# ---------------------------------------------------------------------------------


class ABCMartScraper(BaseScraper):
    """ABC마트 상품 상세 URL을 파싱하는 실크롤러."""

    def __init__(self, headless: bool = True, min_delay: float = 1.5, max_delay: float = 4.2):
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay

    async def fetch_product(self, style_code: str) -> ScrapedProduct:
        # ABC마트는 품번으로 직접 상세페이지를 여는 공개 URL 규칙이 확인되지 않아,
        # 검색 결과 페이지를 거치는 흐름이 필요하다 (2차 지시 후속 작업).
        raise NotImplementedError(
            "품번 기반 직접 조회는 아직 미구현입니다. fetch_product_by_url(product_url)을 사용하세요."
        )

    async def fetch_product_by_url(self, product_url: str) -> ScrapedProduct:
        """상품 상세 페이지 URL을 직접 열어 데이터를 파싱한다."""
        launch_kwargs = {
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if os.path.exists(_PREINSTALLED_CHROMIUM):
            launch_kwargs["executable_path"] = _PREINSTALLED_CHROMIUM

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1366, "height": 768},
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                )
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                page = await context.new_page()

                # PRD 2.2: 정규분포형 가변 딜레이(1.5~4.2초)로 일정 주기 요청 패턴을 피한다.
                await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

                # "networkidle"은 광고/채팅위젯/재고 폴링 때문에 실사이트에서는 끝까지
                # 도달하지 못하고 무한 대기로 이어질 수 있다 — 기본 HTML만 빠르게 받는다.
                await page.goto(product_url, wait_until="domcontentloaded", timeout=20000)

                # 이 사이트는 React/Vue CSR 구조(PRD 2.1)라 상품 데이터가 XHR로 뒤늦게
                # 채워진다. JSON-LD나 가격 요소가 나타날 때까지 최대 10초만 별도로 기다리고,
                # 그래도 안 나타나면(위젯/폴링으로 계속 바쁜 상태) 있는 그대로 파싱을 시도한다.
                try:
                    await page.wait_for_selector(f"{SELECTOR_JSON_LD}, {SELECTOR_PRICE}", timeout=10000)
                except Exception:
                    pass

                return await self._parse_product_page(page, product_url)
            finally:
                await browser.close()

    async def _parse_product_page(self, page: Page, product_url: str) -> ScrapedProduct:
        product_data = await self._extract_json_ld(page)

        if product_data:
            brand = product_data.get("brand")
            brand_name = brand.get("name", "") if isinstance(brand, dict) else (brand or "")
            product_name = product_data.get("name", "")
            offers = product_data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = self._parse_price(offers.get("price"))
        else:
            brand_name = await self._safe_text(page, SELECTOR_BRAND)
            product_name = await self._safe_text(page, SELECTOR_PRODUCT_NAME)
            price = self._parse_price(await self._safe_text(page, SELECTOR_PRICE))

        style_code = self._extract_style_code(product_name) or self._extract_style_code(product_url) or ""
        size_stock = await self._extract_size_stock(page)

        return ScrapedProduct(
            style_code=style_code,
            brand_name=brand_name,
            product_name=product_name,
            source_url=product_url,
            price=price,
            size_stock=size_stock,
            raw_specs={},
        )

    async def _extract_json_ld(self, page: Page) -> dict | None:
        try:
            scripts = await page.locator(SELECTOR_JSON_LD).all_text_contents()
        except Exception:
            return None

        for raw in scripts:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            candidates = data if isinstance(data, list) else [data]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                    return candidate
        return None

    async def _safe_text(self, page: Page, selector: str) -> str:
        try:
            text = await page.locator(selector).first.text_content(timeout=3000)
            return (text or "").strip()
        except Exception:
            return ""

    async def _extract_size_stock(self, page: Page) -> dict:
        size_stock: dict[str, dict] = {}
        try:
            options = page.locator(SELECTOR_SIZE_OPTIONS)
            count = await options.count()
            for i in range(count):
                option = options.nth(i)
                label = (await option.text_content() or "").strip()
                if not label:
                    continue
                class_attr = (await option.get_attribute("class")) or ""
                is_sold_out = "disabled" in class_attr or "soldout" in class_attr.lower()
                size_stock[label] = {"stock": 0 if is_sold_out else None, "is_sold_out": is_sold_out}
        except Exception:
            pass
        return size_stock

    @staticmethod
    def _extract_style_code(text: str) -> str | None:
        if not text:
            return None
        match = STYLE_CODE_PATTERN.search(text.upper())
        return match.group(0) if match else None

    @staticmethod
    def _parse_price(raw) -> float:
        if raw is None:
            return 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        digits = re.sub(r"[^\d]", "", str(raw))
        return float(digits) if digits else 0.0
