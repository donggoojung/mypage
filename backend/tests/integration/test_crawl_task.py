from sqlalchemy import select

from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.workers.tasks.crawl_tasks import crawl_and_upsert_product


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
