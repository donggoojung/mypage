from decimal import Decimal

from sqlalchemy import select

from app.models.enums import ListingStatus, MarketType, SourcePlatform
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.workers.tasks.crawl_tasks import crawl_and_upsert_product, refresh_all_source_mappings


def test_crawl_and_upsert_product_creates_master_product_and_mapping(db_session):
    result = crawl_and_upsert_product("CW2288-111", "abc_mart")

    assert result["style_code"] == "CW2288-111"
    assert result["price"] == 139000.0

    product = db_session.scalar(select(MasterProduct).where(MasterProduct.style_code == "CW2288-111"))
    assert product is not None
    assert product.brand_name == "나이키"

    mapping = db_session.scalar(select(SourceMapping).where(SourceMapping.product_id == product.product_id))
    assert mapping is not None
    assert mapping.size_stock_json["250"]["stock"] == 5


def test_crawl_and_upsert_product_upserts_on_second_run(db_session):
    crawl_and_upsert_product("CW2288-111", "abc_mart")
    result = crawl_and_upsert_product("CW2288-111", "abc_mart")

    products = db_session.scalars(select(MasterProduct).where(MasterProduct.style_code == "CW2288-111")).all()
    mappings = db_session.scalars(select(SourceMapping).where(SourceMapping.product_id == result["product_id"])).all()

    # 같은 품번·같은 소싱처로 두 번 크롤링해도 row가 중복 생성되지 않고 갱신되어야 한다.
    assert len(products) == 1
    assert len(mappings) == 1


def test_refresh_all_source_mappings_updates_stale_price_and_stock(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.flush()
    mapping = SourceMapping(
        product_id=product.product_id,
        source_platform=SourcePlatform.ABC_MART,
        source_url="https://mock.abcmart.example.com/products/CW2288-111",
        source_price=1.0,  # 일부러 오래된(잘못된) 값을 넣어 새로고침으로 바뀌는지 확인한다.
        size_stock_json={"250": {"stock": 0, "is_sold_out": True}},
    )
    db_session.add(mapping)
    db_session.commit()

    result = refresh_all_source_mappings()

    assert result["refreshed"] == [mapping.source_id]
    db_session.refresh(mapping)
    assert mapping.source_price == 139000.0
    assert mapping.size_stock_json["250"]["stock"] == 5
    assert mapping.last_checked_at is not None


def test_refresh_all_source_mappings_skips_platform_without_url_refresh(db_session):
    product = MasterProduct(style_code="NO-REFRESH-001", brand_name="테스트", product_name="테스트상품")
    db_session.add(product)
    db_session.flush()
    mapping = SourceMapping(
        product_id=product.product_id,
        source_platform=SourcePlatform.MUSINSA,
        source_url="https://mock.musinsa.example.com/products/NO-REFRESH-001",
        source_price=50000.0,
        size_stock_json={"270": {"stock": 3, "is_sold_out": False}},
    )
    db_session.add(mapping)
    db_session.commit()

    result = refresh_all_source_mappings()

    assert result["skipped"] == [mapping.source_id]
    db_session.refresh(mapping)
    assert mapping.source_price == 50000.0  # 갱신되지 않고 그대로 유지


# --- 품절/가격 자동동기화(2단계 안전장치) 검증 -------------------------------------
# Mock 스크래퍼(USE_MOCK_SCRAPERS)는 항상 같은 고정값을 돌려준다:
# price=139000.0, size_stock={"250": 재고5(판매중), "260": 재고0(품절), "270": 재고2(판매중)}


def test_refresh_syncs_newly_sold_out_size_to_coupang(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.flush()
    mapping = SourceMapping(
        product_id=product.product_id,
        source_platform=SourcePlatform.ABC_MART,
        source_url="https://mock.abcmart.example.com/products/CW2288-111",
        source_price=139000.0,
        # 실제로는 Mock이 "260"을 품절로 돌려주는데, 기존 DB엔 아직 "판매중"으로 남아있는
        # 상황을 재현한다 — 소싱처에서 방금 품절이 발생한 시나리오.
        size_stock_json={
            "250": {"stock": 5, "is_sold_out": False},
            "260": {"stock": 3, "is_sold_out": False},
            "270": {"stock": 2, "is_sold_out": False},
        },
    )
    db_session.add(mapping)
    listing = MarketListing(
        product_id=product.product_id,
        market_type=MarketType.COUPANG,
        market_product_id="12345678",
        selling_price=Decimal("193827"),  # 이미 최신 계산가와 같게 둬서 가격변경은 안 일어나게 함
        status=ListingStatus.ACTIVE,
        vendor_item_ids_json={"250": "900001", "260": "900002", "270": "900003"},
    )
    db_session.add(listing)
    db_session.commit()

    result = refresh_all_source_mappings()

    assert len(result["synced_to_coupang"]) == 1
    sync_entry = result["synced_to_coupang"][0]
    assert sync_entry["listing_id"] == listing.listing_id
    assert sync_entry["stopped"] == ["260"]
    assert sync_entry["resumed"] == []


def test_refresh_syncs_price_increase_to_coupang(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.flush()
    mapping = SourceMapping(
        product_id=product.product_id,
        source_platform=SourcePlatform.ABC_MART,
        source_url="https://mock.abcmart.example.com/products/CW2288-111",
        source_price=100000.0,  # 낮은 옛날 원가 — 새로고침하면 139000원으로 오른다.
        size_stock_json={
            "250": {"stock": 5, "is_sold_out": False},
            "260": {"stock": 0, "is_sold_out": True},
            "270": {"stock": 2, "is_sold_out": False},
        },
    )
    db_session.add(mapping)
    listing = MarketListing(
        product_id=product.product_id,
        market_type=MarketType.COUPANG,
        market_product_id="12345678",
        selling_price=Decimal("130000"),  # 옛 원가 기준 판매가 — 새 원가로는 안 맞음
        status=ListingStatus.ACTIVE,
        vendor_item_ids_json={"250": "900001", "260": "900002", "270": "900003"},
    )
    db_session.add(listing)
    db_session.commit()

    result = refresh_all_source_mappings()

    sync_entry = result["synced_to_coupang"][0]
    assert sync_entry["price_updated"] is True
    db_session.refresh(listing)
    # 원가 139000원은 10~15만원 구간(매입가 대비 목표마진 20% = 27,800원) + 택배비 4000원 반영 결과.
    assert listing.selling_price == Decimal("193827")


def test_refresh_skips_coupang_sync_for_draft_listing_without_vendor_item_ids(db_session):
    """아직 승인 전(DRAFT)이라 vendorItemId가 없는 상품은 동기화 대상에서 조용히 제외되어야 한다."""
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.flush()
    mapping = SourceMapping(
        product_id=product.product_id,
        source_platform=SourcePlatform.ABC_MART,
        source_url="https://mock.abcmart.example.com/products/CW2288-111",
        source_price=100000.0,
        size_stock_json={"250": {"stock": 5, "is_sold_out": False}},
    )
    db_session.add(mapping)
    listing = MarketListing(
        product_id=product.product_id,
        market_type=MarketType.COUPANG,
        market_product_id="12345678",
        selling_price=Decimal("130000"),
        status=ListingStatus.DRAFT,
        vendor_item_ids_json={},  # 승인 전이라 아직 옵션ID가 없음
    )
    db_session.add(listing)
    db_session.commit()

    result = refresh_all_source_mappings()

    assert result["synced_to_coupang"] == []
    db_session.refresh(listing)
    assert listing.selling_price == Decimal("130000")  # 가격도 그대로(건드리지 않음)
