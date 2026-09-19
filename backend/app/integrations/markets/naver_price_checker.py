"""네이버쇼핑 검색 오픈API 기반 경쟁가 조회 클라이언트.

https://developers.naver.com/docs/serviceapi/search/shopping/shopping.md
공식 공개 검색 API라 스크래핑/안티봇 우회가 필요 없다. `sort=asc`로 요청하면
네이버가 이미 가격 오름차순으로 정렬해 반환하므로, 첫 번째 결과가 곧 최저가다.
"""

import re

import httpx

from app.core.config import Settings, get_settings
from app.integrations.markets.price_checker_base import BaseCompetitorPriceChecker, CompetitorListing
from app.models.enums import MarketType

NAVER_SHOPPING_SEARCH_URL = "https://openapi.naver.com/v1/search/shop.json"
_HTML_TAG_PATTERN = re.compile(r"<.*?>")


def _strip_html_tags(text: str) -> str:
    return _HTML_TAG_PATTERN.sub("", text)


class NaverShoppingPriceChecker(BaseCompetitorPriceChecker):
    """네이버쇼핑 검색 결과에서 동일/유사 상품의 최저가를 조회한다."""

    def __init__(self, settings: Settings):
        if not settings.naver_search_client_id or not settings.naver_search_client_secret:
            raise ValueError(
                "NAVER_SEARCH_CLIENT_ID / NAVER_SEARCH_CLIENT_SECRET 환경변수가 설정되어 있지 않습니다."
            )
        self._client_id = settings.naver_search_client_id
        self._client_secret = settings.naver_search_client_secret

    async def find_lowest_price(self, keyword: str) -> CompetitorListing | None:
        headers = {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
        }
        params = {"query": keyword, "display": 10, "sort": "asc"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(NAVER_SHOPPING_SEARCH_URL, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

        items = data.get("items") or []
        if not items:
            return None

        cheapest = items[0]
        return CompetitorListing(
            market_type=MarketType.NAVER_SMARTSTORE,
            product_title=_strip_html_tags(cheapest.get("title", "")),
            price=float(cheapest.get("lprice") or 0),
            product_url=cheapest.get("link", ""),
            mall_name=cheapest.get("mallName", ""),
        )


class MockNaverShoppingPriceChecker(BaseCompetitorPriceChecker):
    """실제 API 키 없이 로컬 개발/테스트가 가능한 고정 응답 Mock."""

    def __init__(self, fixed_price: float = 89000.0, mall_name: str = "Mock몰"):
        self._fixed_price = fixed_price
        self._mall_name = mall_name

    async def find_lowest_price(self, keyword: str) -> CompetitorListing | None:
        return CompetitorListing(
            market_type=MarketType.NAVER_SMARTSTORE,
            product_title=f"[Mock 검색결과] {keyword}",
            price=self._fixed_price,
            product_url="https://shopping.naver.com/mock/product/1",
            mall_name=self._mall_name,
        )


def get_naver_price_checker() -> BaseCompetitorPriceChecker:
    settings = get_settings()
    if settings.use_mock_price_checkers:
        return MockNaverShoppingPriceChecker()
    return NaverShoppingPriceChecker(settings)
