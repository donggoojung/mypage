"""쿠팡 경쟁가 확인(부가 정보) 기능 테스트.

대표님 결정: 판매가는 경쟁가에 맞춰 자동으로 조정하지 않고("일단 책정한 대로"),
경쟁 상품 수/최저가만 기록해뒀다가 가격 차이가 큰 상품을 나중에 검토할 수 있게 한다.
"""

from decimal import Decimal

import pytest

from app.integrations.markets.price_checker_base import CompetitorListing
from app.models.enums import MarketType
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.services import product_pipeline
from app.services.product_pipeline import COMPETITOR_PRICE_GAP_WARNING_PERCENT, _check_coupang_competitor_price


class _FakeChecker:
    def __init__(self, listing: CompetitorListing | None = None, raise_error: bool = False):
        self._listing = listing
        self._raise_error = raise_error

    async def find_lowest_price(self, keyword: str) -> CompetitorListing | None:
        if self._raise_error:
            raise RuntimeError("가짜 크롤링 실패(테스트용)")
        return self._listing


async def test_returns_count_and_price_when_competitor_found(monkeypatch):
    listing = CompetitorListing(
        market_type=MarketType.COUPANG,
        product_title="나이키 에어포스 1",
        price=90000.0,
        product_url="https://www.coupang.com/vp/products/1",
        competitor_count=5,
    )
    monkeypatch.setattr(product_pipeline, "get_price_checker", lambda market_type: _FakeChecker(listing))

    count, price = await _check_coupang_competitor_price("나이키", "에어포스 1", Decimal("139000"))

    assert count == 5
    assert price == Decimal("90000")


async def test_returns_zero_count_when_no_competitor_found(monkeypatch):
    monkeypatch.setattr(product_pipeline, "get_price_checker", lambda market_type: _FakeChecker(None))

    count, price = await _check_coupang_competitor_price("희귀브랜드", "희귀상품", Decimal("139000"))

    assert count == 0
    assert price is None


async def test_returns_none_when_checker_fails(monkeypatch):
    monkeypatch.setattr(product_pipeline, "get_price_checker", lambda market_type: _FakeChecker(raise_error=True))

    count, price = await _check_coupang_competitor_price("나이키", "에어포스 1", Decimal("139000"))

    assert count is None
    assert price is None


async def test_high_gap_still_returns_values_without_adjusting_price(monkeypatch, capsys):
    """가격 차이가 경고 임계값을 넘어도 반환값(판매가 자체)은 건드리지 않는다 — 정보만 로그로 남긴다."""
    listing = CompetitorListing(
        market_type=MarketType.COUPANG,
        product_title="경쟁상품",
        price=50000.0,
        product_url="https://www.coupang.com/vp/products/2",
        competitor_count=2,
    )
    monkeypatch.setattr(product_pipeline, "get_price_checker", lambda market_type: _FakeChecker(listing))

    our_price = Decimal("100000")  # 경쟁 최저가보다 100% 높음 — 임계값(20%) 초과.
    count, price = await _check_coupang_competitor_price("브랜드", "상품", our_price)

    assert count == 2
    assert price == Decimal("50000")
    gap_percent = float((our_price - price) / price * 100)
    assert gap_percent >= COMPETITOR_PRICE_GAP_WARNING_PERCENT
    captured = capsys.readouterr()
    assert "검토해주세요" in captured.out


@pytest.fixture
def product(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.commit()
    return product


class _NoCloseSessionWrapper:
    """테스트 전용 — with 블록이 끝나도 공유 db_session 픽스처를 닫지 않는다."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *args):
        pass


def test_register_coupang_sync_persists_competitor_fields(product, db_session, monkeypatch):
    """_register_coupang_sync가 경쟁가 정보를 실제로 listing에 저장하고 커밋하는지 확인."""

    def _fake_register_product_for_master_product(**kwargs):
        listing = MarketListing(
            product_id=kwargs["product_id"],
            market_type=MarketType.COUPANG,
            market_product_id="12345678",
            selling_price=kwargs["selling_price"],
        )
        db_session.add(listing)
        db_session.flush()
        return listing

    monkeypatch.setattr(
        product_pipeline, "register_product_for_master_product", _fake_register_product_for_master_product
    )
    monkeypatch.setattr(product_pipeline, "SessionLocalSync", lambda: _NoCloseSessionWrapper(db_session))

    listing = product_pipeline._register_coupang_sync(
        product.product_id,
        68010,
        Decimal("139000"),
        {"250": {"stock": 5, "is_sold_out": False}},
        False,
        competitor_count=7,
        competitor_lowest_price=Decimal("120000"),
    )

    assert listing.competitor_count == 7
    assert float(listing.competitor_lowest_price) == 120000.0

    reloaded = db_session.query(MarketListing).filter_by(listing_id=listing.listing_id).first()
    assert reloaded.competitor_count == 7
