import pytest

from app.integrations.markets.coupang_price_checker import MockCoupangPriceChecker
from app.integrations.markets.naver_price_checker import MockNaverShoppingPriceChecker
from app.models.enums import MarketType


@pytest.mark.asyncio
async def test_mock_naver_checker_returns_fixed_price():
    checker = MockNaverShoppingPriceChecker(fixed_price=88000.0)

    result = await checker.find_lowest_price("아디다스 리스폰스 러너 2")

    assert result is not None
    assert result.market_type == MarketType.NAVER_SMARTSTORE
    assert result.price == 88000.0
    assert "아디다스 리스폰스 러너 2" in result.product_title


@pytest.mark.asyncio
async def test_mock_coupang_checker_returns_fixed_price():
    checker = MockCoupangPriceChecker(fixed_price=97000.0)

    result = await checker.find_lowest_price("아디다스 리스폰스 러너 2")

    assert result is not None
    assert result.market_type == MarketType.COUPANG
    assert result.price == 97000.0
