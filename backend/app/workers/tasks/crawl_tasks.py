import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import SessionLocalSync
from app.integrations.scrapers.factory import get_scraper
from app.models.enums import SourcePlatform
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping


@celery_app.task(name="crawl_tasks.crawl_and_upsert_product")
def crawl_and_upsert_product(style_code: str, source_platform: str = SourcePlatform.ABC_MART.value) -> dict:
    """PRD 1차 지시: 소싱처를 스크래핑해 master_products/source_mappings에 적재하는 백그라운드 작업.

    Celery 태스크는 동기 컨텍스트에서 실행되므로, 비동기 스크래퍼는 asyncio.run으로 호출하고
    DB는 동기 세션(SessionLocalSync)을 사용한다.
    """
    platform = SourcePlatform(source_platform)
    scraper = get_scraper(platform)
    scraped = asyncio.run(scraper.fetch_product(style_code))

    with SessionLocalSync() as session:
        product = session.scalar(select(MasterProduct).where(MasterProduct.style_code == scraped.style_code))
        if product is None:
            product = MasterProduct(
                style_code=scraped.style_code,
                brand_name=scraped.brand_name,
                product_name=scraped.product_name,
                raw_specs_json=scraped.raw_specs,
            )
            session.add(product)
            session.flush()  # product_id 채번

        mapping = session.scalar(
            select(SourceMapping).where(
                SourceMapping.product_id == product.product_id,
                SourceMapping.source_platform == platform,
            )
        )
        if mapping is None:
            mapping = SourceMapping(product_id=product.product_id, source_platform=platform)
            session.add(mapping)

        mapping.source_url = scraped.source_url
        mapping.source_price = scraped.price
        mapping.size_stock_json = scraped.size_stock
        mapping.last_checked_at = datetime.now(UTC)

        session.commit()
        return {
            "product_id": product.product_id,
            "source_id": mapping.source_id,
            "style_code": product.style_code,
            "price": float(mapping.source_price),
        }
