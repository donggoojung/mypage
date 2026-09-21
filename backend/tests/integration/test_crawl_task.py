from sqlalchemy import select

from app.models.enums import SourcePlatform
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
