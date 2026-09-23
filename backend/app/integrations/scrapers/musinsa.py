"""무신사 상품 상세페이지 크롤러 스켈레톤 (실사이트 미검증).

abc_mart.py와 같은 순서로 짰다 — 품번(또는 상품명)으로 검색해 첫 결과의 상세
페이지로 들어가, JSON-LD 구조화 데이터를 우선 시도하고 없으면 DOM 셀렉터로
폴백해서 가격/사이즈별 재고를 읽는다. 다만 URL 패턴과 SELECTOR_*는 전부 아직
실제 무신사 화면(F12)으로 확인 전인 "최선의 추정"이다.

실제로 쓰기 전에 반드시:
  1. 실제 상품에 SourceMapping(source_platform=MUSINSA)을 등록하지 않는 한, 이
     코드가 존재해도 sourcing_optimizer.find_cheapest_source가 실제로 호출하지
     않는다(=안전, 실제 주문 처리에 영향 없음).
  2. scripts/test_scraper_margin.py 같은 진단 스크립트로 실제 무신사 상품 URL을
     넣어 결과를 확인하고, 비거나 틀리면 SELECTOR_*/URL 패턴을 실제 화면 기준으로
     고쳐야 한다 (abc_mart.py 검증 때와 동일한 절차).
"""

import asyncio
import json
import random
import re
from urllib.parse import quote

from playwright.async_api import Page, async_playwright

from app.integrations.scrapers.base import BaseScraper, ScrapedProduct
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs

STYLE_CODE_PATTERN = re.compile(r"\b[A-Z]{1,3}\d{3,6}-\d{2,5}\b")

# 2026-09-23 실사이트(musinsa.com 메인 추천 페이지) 실제 HTML로 확인됨: 상품 상세
# URL은 항상 이 패턴 하나뿐이다 — "/app/goods/..." 같은 옛날 패턴은 실제로 안 쓰인다.
# 예: https://www.musinsa.com/products/6324162
PRODUCT_URL_PATTERN = re.compile(r"https://www\.musinsa\.com/products/\d+")

# 실사이트 미검증(최선의 추정) — 검색 URL 자체는 아직 확인 전. 검색 결과 페이지도
# /products/{id} 링크를 쓸 가능성이 높지만(위 패턴과 동일한 컴포넌트일 것으로 추정),
# 실제로 열어봐야 확실하다.
SEARCH_URL_TEMPLATE = "https://www.musinsa.com/search/goods?keyword={query}"

SELECTOR_JSON_LD = 'script[type="application/ld+json"]'
# 2026-09-23 실사이트로 확인됨 — 상품 상세로 가는 링크는 항상 이 href 패턴을 쓴다.
SELECTOR_PRODUCT_LINK = "a[href*='musinsa.com/products/']"
# 아래 3개는 상품 "목록" 카드 기준으로 확인된 값(실사이트, 2026-09-23) — 상품
# "상세" 페이지 자체의 구조는 아직 미확인이라 최선의 추정으로 남겨둔다.
SELECTOR_BRAND = ".product-brand, .goods_brand"
SELECTOR_PRODUCT_NAME = ".product-title, .goods_name, h1"
SELECTOR_PRICE = ".price, .goods_price .txt-price"
SELECTOR_SIZE_OPTIONS = ".size-option li, .option-size button, select[name='option'] option"


class MusinsaScraper(BaseScraper):
    """무신사 상품 상세 크롤러 — 실사이트 미검증 스켈레톤 (abc_mart.py와 동일한 구조)."""

    def __init__(self, headless: bool = True, min_delay: float = 1.5, max_delay: float = 4.2):
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay

    async def fetch_product(self, style_code: str) -> ScrapedProduct:
        """품번(또는 상품명)으로 검색해 첫 결과의 상세 페이지를 연다 — 실사이트 미검증."""
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self.headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                page = await context.new_page()

                await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
                search_url = SEARCH_URL_TEMPLATE.format(query=quote(style_code))
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

                await page.wait_for_selector(SELECTOR_PRODUCT_LINK, state="attached", timeout=10000)
                product_url = await page.locator(SELECTOR_PRODUCT_LINK).first.get_attribute("href")
                if not product_url:
                    raise RuntimeError(f"무신사 검색 결과에서 '{style_code}' 상품 링크를 찾지 못했습니다 (셀렉터 재검증 필요).")
                # 실사이트로 확인됨(2026-09-23): 상품 카드의 href는 항상 절대 URL이라
                # 상대경로 보정은 필요 없다 — 다만 구조가 바뀌었을 가능성을 대비해 패턴을
                # 한 번 더 검증한다.
                if not PRODUCT_URL_PATTERN.match(product_url):
                    raise RuntimeError(
                        f"찾은 링크({product_url})가 예상한 상품 URL 패턴과 다릅니다 — "
                        "검색 결과 화면 구조가 바뀌었을 수 있어 재검증이 필요합니다."
                    )

                return await self._fetch_by_url_in_page(page, product_url)
            finally:
                await browser.close()

    async def fetch_product_by_url(self, product_url: str) -> ScrapedProduct:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self.headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                page = await context.new_page()
                await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
                return await self._fetch_by_url_in_page(page, product_url)
            finally:
                await browser.close()

    async def _fetch_by_url_in_page(self, page: Page, product_url: str) -> ScrapedProduct:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector(f"{SELECTOR_JSON_LD}, {SELECTOR_PRICE}", timeout=10000)
        except Exception:
            pass
        return await self._parse_product_page(page, product_url)

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
            image = product_data.get("image")
            image_url = (image[0] if isinstance(image, list) and image else image) or ""
        else:
            brand_name = await self._safe_text(page, SELECTOR_BRAND)
            product_name = await self._safe_text(page, SELECTOR_PRODUCT_NAME)
            price = self._parse_price(await self._safe_text(page, SELECTOR_PRICE))
            image_url = ""

        style_code = self._extract_style_code(product_name) or self._extract_style_code(product_url) or ""
        size_stock = await self._extract_size_stock(page)

        return ScrapedProduct(
            style_code=style_code,
            brand_name=brand_name,
            product_name=product_name,
            source_url=product_url,
            price=price,
            image_url=image_url,
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
                disabled_attr = await option.get_attribute("disabled")
                is_sold_out = (
                    disabled_attr is not None
                    or "disabled" in class_attr
                    or "soldout" in class_attr.lower()
                    or "품절" in label
                )
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
