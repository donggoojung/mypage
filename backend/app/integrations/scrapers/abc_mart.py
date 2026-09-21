"""ABC마트 상품 상세페이지 실크롤러 (PRD 2.1, 2.2).

데이터 추출 우선순위:
  1순위: `<script type="application/ld+json">`에 담긴 schema.org Product 구조화 데이터
         (브랜드/상품명/가격 — 실사이트에서 확인됨, 2026-09-19).
  2순위: 1순위가 없을 때 DOM 셀렉터(SELECTOR_*)로 폴백.

품번(스타일코드)은 상세 페이지 상단에 "스타일코드 : S28216-62" 형태로 라벨과 함께
명시되어 있어(실사이트 확인됨), 정규식 추측 대신 이 라벨을 페이지 텍스트에서 직접 찾는다.
사이즈 선택은 `ul.size-list` 안의 `button.btn-prod-size` 구조로 확인됨(실사이트 확인됨).

사이트 구조는 바뀔 수 있으므로, `python scripts/test_scraper_margin.py <실제 상품 URL>` 로
주기적으로 재검증하고 파싱이 비면 SELECTOR_* 값을 다시 확인한다.
"""

import asyncio
import json
import random
import re

from playwright.async_api import Page, async_playwright

from app.integrations.scrapers.base import BaseScraper, ScrapedProduct
from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs

# 나이키 CW2288-111, 아디다스 S28216-62 형태(영문 1~3자 + 숫자 3~6자리 - 숫자 2~5자리)의
# 품번 정규식 (PRD 2.1). 페이지에 "스타일코드" 라벨이 없을 때의 최후 폴백으로만 쓴다.
STYLE_CODE_PATTERN = re.compile(r"\b[A-Z]{1,3}\d{3,6}-\d{2,5}\b")
# 2026-09-19 실사이트(abcmart.a-rt.com) 개발자도구로 확인: 상세 페이지 상단에
# "스타일코드 : S28216-62" 형태로 라벨과 함께 명시된다. 이게 정규식 추측보다 훨씬 안정적이라
# 페이지 텍스트에서 이 라벨을 직접 찾는 것을 최우선으로 시도한다.
STYLE_CODE_LABEL_PATTERN = re.compile(r"스타일코드\s*[:：]\s*(\S+)")

# 2026-09-19 실사이트 개발자도구로 검증된 셀렉터.
SELECTOR_JSON_LD = 'script[type="application/ld+json"]'
SELECTOR_BRAND = ".prod-brand, .brand-name"
SELECTOR_PRODUCT_NAME = ".prod-name, .goods-name, h1"
SELECTOR_PRICE = ".prod-price .price, .sale-price"
# 실사이트 구조: <ul class="size-list"><li>...<button class="btn-prod-size">260</button></li></ul>
SELECTOR_SIZE_OPTIONS = ".size-list .btn-prod-size, .size-option li, .option-size li"

# 주의 — 실사이트 미검증(카테고리/목록 페이지는 아직 devtools로 확인 못함).
# 상품 목록에서 결과가 0개로 나오면 이 셀렉터를 F12로 실제 값 확인 후 조정해야 한다.
SELECTOR_PRODUCT_LINK = "a[href*='/product/']"
CATEGORY_PAGE_QUERY_PARAM = "page"


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
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self.headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
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

    async def fetch_category_product_urls(
        self, category_url: str, max_products: int = 50, max_pages: int = 10
    ) -> list[str]:
        """카테고리/섹션 목록 페이지를 순회하며 상품 상세 URL을 모은다 (배치 등록용).

        주의 — 실사이트 미검증: SELECTOR_PRODUCT_LINK와 `?page=N` 페이지네이션 방식은
        상세 페이지 셀렉터처럼 devtools로 확인된 값이 아니다. 결과가 0개면 실제 목록
        페이지를 F12로 열어 SELECTOR_PRODUCT_LINK를 조정해야 한다.

        전체 사이트를 한 번에 긁는 기능은 의도적으로 지원하지 않는다 — `max_products`로
        섹션 1개당 가져올 상품 수를 제한해, 사람이 결과를 한번 검토하고 등록할 수 있게 한다.
        """
        urls: list[str] = []
        seen: set[str] = set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(self.headless))
            try:
                context = await browser.new_context(**new_context_kwargs())
                await context.add_init_script(STEALTH_INIT_SCRIPT)
                page = await context.new_page()

                for page_num in range(1, max_pages + 1):
                    if len(urls) >= max_products:
                        break

                    await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
                    page_url = self._with_page_param(category_url, page_num)
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=20000)

                    try:
                        await page.wait_for_selector(SELECTOR_PRODUCT_LINK, timeout=10000)
                    except Exception:
                        break  # 상품이 없거나(마지막 페이지) 목록 페이지 구조가 예상과 다름

                    try:
                        hrefs = await page.locator(SELECTOR_PRODUCT_LINK).evaluate_all("els => els.map(e => e.href)")
                    except Exception:
                        break

                    # 같은 페이지 안의 중복 링크도 걸러지도록, 확인과 동시에 seen에 추가한다.
                    new_hrefs = []
                    for href in hrefs:
                        if href and href not in seen:
                            seen.add(href)
                            new_hrefs.append(href)
                    if not new_hrefs:
                        break  # 새로 나온 상품이 없으면(마지막 페이지 반복) 중단

                    for href in new_hrefs:
                        urls.append(href)
                        if len(urls) >= max_products:
                            break
            finally:
                await browser.close()

        return urls[:max_products]

    @staticmethod
    def _with_page_param(url: str, page_num: int) -> str:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query[CATEGORY_PAGE_QUERY_PARAM] = str(page_num)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

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

        style_code = (
            await self._extract_style_code_from_page_text(page)
            or self._extract_style_code(product_name)
            or self._extract_style_code(product_url)
            or ""
        )
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
                    or "sold-out" in class_attr.lower()
                )
                size_stock[label] = {"stock": 0 if is_sold_out else None, "is_sold_out": is_sold_out}
        except Exception:
            pass
        return size_stock

    async def _extract_style_code_from_page_text(self, page: Page) -> str | None:
        """ABC마트 상세 페이지에 "스타일코드 : S28216-62" 형태로 명시된 라벨을 직접 찾는다."""
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return None
        match = STYLE_CODE_LABEL_PATTERN.search(body_text)
        return match.group(1) if match else None

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
