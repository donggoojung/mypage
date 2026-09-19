from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models.enums import MarketType


@dataclass
class CompetitorListing:
    """검색 결과에서 찾은 경쟁 상품 1건."""

    market_type: MarketType
    product_title: str
    price: float
    product_url: str
    mall_name: str = ""


class BaseCompetitorPriceChecker(ABC):
    """네이버쇼핑/쿠팡 등에서 동일·유사 상품의 현재 최저 판매가를 조회하는 인터페이스."""

    @abstractmethod
    async def find_lowest_price(self, keyword: str) -> CompetitorListing | None:
        """키워드(보통 브랜드+상품명 또는 품번)로 검색해 최저가 리스팅을 반환한다.

        검색 결과가 없으면 None을 반환한다.
        """
        raise NotImplementedError
