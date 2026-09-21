import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import SessionLocalSync
from app.integrations.scrapers.factory import get_scraper
from app.models.enums import SourcePlatform
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping

# 매핑 시점에 저장해둔 source_url로 재조회한다 — 품번만으로 직접 조회하는 실API가
# 아직 없는 소싱처(2차 지시 후속)라도, 이미 알고 있는 상세페이지 URL로는 재크롤링이 가능하다.
_URL_REFRESHABLE_PLATFORMS = {SourcePlatform.ABC_MART}


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
        mapping.source_image_url = scraped.image_url or None
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


@celery_app.task(name="crawl_tasks.refresh_all_source_mappings")
def refresh_all_source_mappings() -> dict:
    """등록된 모든 소싱처 매핑의 가격/재고를 실시간으로 다시 조회해 갱신한다.

    Celery beat로 주기 실행하면(예: 30분마다) "3번 DB에 저장한 재고"가 계속 최신 상태로
    유지된다. 이미 저장된 `source_url`로 재조회하기 때문에, 품번만으로 직접 조회하는
    실API가 없는 소싱처(무신사/폴더 등)는 아직 건너뛴다.
    """
    with SessionLocalSync() as session:
        mappings = session.query(SourceMapping).all()
        refreshed, skipped, failed = [], [], []

        for mapping in mappings:
            if mapping.source_platform not in _URL_REFRESHABLE_PLATFORMS or not mapping.source_url:
                skipped.append(mapping.source_id)
                continue

            scraper = get_scraper(mapping.source_platform)
            try:
                scraped = asyncio.run(scraper.fetch_product_by_url(mapping.source_url))
            except Exception as exc:
                failed.append({"source_id": mapping.source_id, "error": str(exc)})
                continue

            mapping.source_price = scraped.price
            mapping.size_stock_json = scraped.size_stock
            mapping.last_checked_at = datetime.now(UTC)
            refreshed.append(mapping.source_id)

        session.commit()
        return {"refreshed": refreshed, "skipped": skipped, "failed": failed}
