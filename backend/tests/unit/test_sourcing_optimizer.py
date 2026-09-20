from decimal import Decimal

import pytest

from app.models.enums import SourcePlatform
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services.sourcing_optimizer import NoAvailableSourceError, find_cheapest_source

# 테스트 환경은 USE_MOCK_SCRAPERS=true라서, 소싱처 실시간 재조회는 항상
# app.integrations.scrapers.mock_scraper의 CW2288-111 고정 데이터
# (price=139000, 250:재고5, 260:품절, 270:재고2)로 갱신된다 — 즉 모든 플랫폼의
# "표시가/재고"는 동일해지고, 최저가 판별은 오직 소싱처별 쿠폰 적용률
# (PLATFORM_COUPON_RATES: ABC마트 0%, 폴더 5%)로만 갈린다.


@pytest.fixture
def product_with_two_sources(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.flush()

    db_session.add(
        SourceMapping(
            product_id=product.product_id,
            source_platform=SourcePlatform.ABC_MART,
            source_url="https://mock.abcmart.example.com/products/CW2288-111",
            source_price=139000.0,
            size_stock_json={"250": {"stock": 5, "is_sold_out": False}, "260": {"stock": 0, "is_sold_out": True}},
        )
    )
    db_session.add(
        SourceMapping(
            product_id=product.product_id,
            source_platform=SourcePlatform.FOLDER,
            source_url="https://mock.folder.example.com/products/CW2288-111",
            source_price=139000.0,
            size_stock_json={"250": {"stock": 5, "is_sold_out": False}, "260": {"stock": 0, "is_sold_out": True}},
        )
    )
    db_session.commit()
    return product


@pytest.mark.asyncio
async def test_find_cheapest_source_picks_higher_coupon_rate_platform(product_with_two_sources, db_session):
    # 표시가는 두 플랫폼 다 139,000원으로 동일하지만, 폴더가 쿠폰 5%라 결제 예정가가 더 낮다.
    quote = await find_cheapest_source(db_session, product_with_two_sources.product_id, size="250")

    assert quote.source_platform == SourcePlatform.FOLDER
    assert quote.expected_payment == Decimal("132050")  # 139000 * 0.95


@pytest.mark.asyncio
async def test_find_cheapest_source_skips_sold_out_size(product_with_two_sources, db_session):
    with pytest.raises(NoAvailableSourceError):
        await find_cheapest_source(db_session, product_with_two_sources.product_id, size="260")


@pytest.mark.asyncio
async def test_find_cheapest_source_raises_when_no_mappings(db_session):
    product = MasterProduct(style_code="NO-SOURCE-001", brand_name="테스트", product_name="소싱처없는상품")
    db_session.add(product)
    db_session.commit()

    with pytest.raises(NoAvailableSourceError):
        await find_cheapest_source(db_session, product.product_id, size="250")


@pytest.mark.asyncio
async def test_find_cheapest_source_scales_with_quantity(product_with_two_sources, db_session):
    quote = await find_cheapest_source(db_session, product_with_two_sources.product_id, size="250", quantity=3)

    assert quote.source_platform == SourcePlatform.FOLDER
    assert quote.expected_payment == Decimal("396150")  # 132050 * 3


@pytest.mark.asyncio
async def test_find_cheapest_source_raises_when_quantity_exceeds_stock(product_with_two_sources, db_session):
    # 250 사이즈 재고는 5개뿐이므로 10개 주문은 매입 불가능해야 한다.
    with pytest.raises(NoAvailableSourceError):
        await find_cheapest_source(db_session, product_with_two_sources.product_id, size="250", quantity=10)
