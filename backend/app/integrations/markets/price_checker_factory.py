from functools import lru_cache

from app.core.config import get_settings
from app.integrations.markets.coupang_price_checker import CoupangPriceChecker, MockCoupangPriceChecker
from app.integrations.markets.naver_price_checker import (
    MockNaverShoppingPriceChecker,
    NaverShoppingPriceChecker,
)
from app.integrations.markets.price_checker_base import BaseCompetitorPriceChecker
from app.models.enums import MarketType


@lru_cache
def get_price_checker(market_type: MarketType) -> BaseCompetitorPriceChecker:
    """플랫폼별 Mock 스위치(`USE_MOCK_NAVER_PRICE_CHECKER`/`USE_MOCK_COUPANG_PRICE_CHECKER`)에 따라
    Mock 또는 실제 경쟁가 조회 클라이언트를 반환한다."""
    settings = get_settings()

    if market_type == MarketType.NAVER_SMARTSTORE:
        if settings.use_mock_naver_price_checker:
            return MockNaverShoppingPriceChecker()
        return NaverShoppingPriceChecker(settings)

    if market_type == MarketType.COUPANG:
        if settings.use_mock_coupang_price_checker:
            return MockCoupangPriceChecker()
        return CoupangPriceChecker()

    raise NotImplementedError(f"{market_type.value}의 경쟁가 조회 클라이언트는 아직 구현되지 않았습니다.")
