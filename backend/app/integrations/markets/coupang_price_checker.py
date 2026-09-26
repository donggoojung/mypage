"""쿠팡 검색 결과 기반 경쟁가 조회 (PRD 2.2).

쿠팡은 경쟁 상품 가격을 조회할 수 있는 공개 API가 없어(쿠팡 Wing/파트너스 Open API는
자기 판매 상품 관리용), 검색 결과 페이지를 Playwright로 직접 파싱한다.

주의 — 셀렉터 미검증: 이 코드는 coupang.com 아웃바운드 접속이 차단된 환경에서 작성되어
실제 검색결과 페이지 구조로 셀렉터를 검증하지 못했다. abc_mart.py와 동일하게, 로컬 PC에서
scripts/test_scraper_margin.py --check-competitor 로 실행해보고 결과가 비면
SELECTOR_* 값을 실제 사이트에 맞게 조정해야 한다.
"""

import asyncio
import random
import re
import urllib.parse

from playwright.async_api import Page, async_playwright

from app.integrations.markets.price_checker_base import BaseCompetitorPriceChecker, CompetitorListing
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs
from app.models.enums import MarketType

COUPANG_SEARCH_URL = "https://www.coupang.com/np/search?component=&q={query}&channel=user"

# --- TODO: 실사이트 검증이 필요한 플레이스홀더 셀렉터 ---
SELECTOR_SEARCH_RESULT_ITEM = "#product-list li.search-product, ul#productList li"
SELECTOR_ITEM_NAME = ".name, .product-name"
SELECTOR_ITEM_PRICE = ".price-value, strong.price-value"
SELECTOR_ITEM_LINK = "a"
# ---------------------------------------------------------


class CoupangPriceChecker(BaseCompetitorPriceChecker):
    """쿠팡 검색 결과에서 최저가 상품을 찾는 실크롤러."""

    def __init__(self, headless: bool = True, min_delay: float = 1.5, max_delay: float = 4.2):
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay

    async def find_lowest_price(self, keyword: str) -> CompetitorListing | None:
        url = COUPANG_SEARCH_URL.format(query=urllib.parse.quote(keyword))

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self.headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                page = await context.new_page()

                await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_selector(SELECTOR_SEARCH_RESULT_ITEM, timeout=10000)
                except Exception:
                    pass

                return await self._parse_lowest_price(page)
            finally:
                await browser.close()

    async def _parse_lowest_price(self, page: Page) -> CompetitorListing | None:
        items = page.locator(SELECTOR_SEARCH_RESULT_ITEM)
        try:
            count = await items.count()
        except Exception:
            return None

        listings: list[CompetitorListing] = []
        for i in range(min(count, 10)):  # 검색 결과 상위 10개만 비교한다.
            item = items.nth(i)
            try:
                name = (await item.locator(SELECTOR_ITEM_NAME).first.text_content(timeout=2000) or "").strip()
                price_text = (
                    await item.locator(SELECTOR_ITEM_PRICE).first.text_content(timeout=2000) or ""
                ).strip()
                href = await item.locator(SELECTOR_ITEM_LINK).first.get_attribute("href", timeout=2000)
            except Exception:
                continue

            price = self._parse_price(price_text)
            if price <= 0 or not name:
                continue

            product_url = href if href and href.startswith("http") else f"https://www.coupang.com{href or ''}"
            listings.append(
                CompetitorListing(
                    market_type=MarketType.COUPANG,
                    product_title=name,
                    price=price,
                    product_url=product_url,
                )
            )

        if not listings:
            return None
        cheapest = min(listings, key=lambda listing: listing.price)
        cheapest.competitor_count = len(listings)
        return cheapest

    @staticmethod
    def _parse_price(raw: str) -> float:
        digits = re.sub(r"[^\d]", "", raw)
        return float(digits) if digits else 0.0


class MockCoupangPriceChecker(BaseCompetitorPriceChecker):
    """실제 크롤링 없이 로컬 개발/테스트가 가능한 고정 응답 Mock."""

    def __init__(self, fixed_price: float = 95000.0):
        self._fixed_price = fixed_price

    async def find_lowest_price(self, keyword: str) -> CompetitorListing | None:
        return CompetitorListing(
            market_type=MarketType.COUPANG,
            product_title=f"[Mock 검색결과] {keyword}",
            price=self._fixed_price,
            product_url="https://www.coupang.com/vp/products/mock-1",
            competitor_count=3,
        )
