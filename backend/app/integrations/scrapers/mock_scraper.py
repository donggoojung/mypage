from app.integrations.scrapers.base import BaseScraper, ScrapedProduct

# 실제 사이트 호출 없이 로컬 개발/테스트를 가능하게 하는 고정 응답 데이터.
_MOCK_CATALOG: dict[str, ScrapedProduct] = {
    "CW2288-111": ScrapedProduct(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="나이키 에어포스 1 '07 화이트",
        source_url="https://mock.abcmart.example.com/products/CW2288-111",
        price=139000.0,
        size_stock={
            "250": {"stock": 5, "is_sold_out": False},
            "260": {"stock": 0, "is_sold_out": True},
            "270": {"stock": 2, "is_sold_out": False},
        },
        raw_specs={"소재": "천연가죽", "제조국": "베트남", "색상": "화이트"},
    ),
}


class MockScraper(BaseScraper):
    """PRD 작업 규칙 3번: 실제 연동 전 로컬 테스트가 가능한 Mock 구현."""

    async def fetch_product(self, style_code: str) -> ScrapedProduct:
        if style_code in _MOCK_CATALOG:
            return _MOCK_CATALOG[style_code]
        # 카탈로그에 없는 품번도 일관된 형태의 가짜 데이터를 돌려준다.
        return ScrapedProduct(
            style_code=style_code,
            brand_name="Mock Brand",
            product_name=f"Mock Product {style_code}",
            source_url=f"https://mock.source.example.com/products/{style_code}",
            price=99000.0,
            size_stock={"270": {"stock": 1, "is_sold_out": False}},
            raw_specs={"소재": "합성섬유"},
        )

    async def fetch_product_by_url(self, product_url: str) -> ScrapedProduct:
        """ABCMartScraper.fetch_product_by_url과 동일한 시그니처의 Mock 버전 —
        재고 자동 새로고침 태스크를 Mock 모드에서도 테스트할 수 있게 해준다.
        """
        for product in _MOCK_CATALOG.values():
            if product.source_url == product_url:
                return product
        return await self.fetch_product(style_code=product_url.rsplit("/", 1)[-1] or "UNKNOWN")
