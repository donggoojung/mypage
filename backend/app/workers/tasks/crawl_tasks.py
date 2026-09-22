import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import SessionLocalSync
from app.integrations.markets.coupang import sync_stock_and_price_to_coupang
from app.integrations.scrapers.factory import get_scraper
from app.models.enums import MarketType, SourcePlatform
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services.margin_engine import calculate_simple_markup_price

# 매핑 시점에 저장해둔 source_url로 재조회한다 — 품번만으로 직접 조회하는 실API가
# 아직 없는 소싱처(2차 지시 후속)라도, 이미 알고 있는 상세페이지 URL로는 재크롤링이 가능하다.
_URL_REFRESHABLE_PLATFORMS = {SourcePlatform.ABC_MART}
# product_pipeline.py의 등록 시 마크업률과 동일한 기본값 — 재고동기화로 가격을 다시
# 계산할 때도 같은 정책을 써야 판매가가 등록 시점과 일관된다.
_DEFAULT_MARKUP_RATE = Decimal("0.30")


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
    """등록된 모든 소싱처 매핑의 가격/재고를 실시간으로 다시 조회해 갱신하고,
    변경 사항(품절/재입고/가격변동)을 실제 쿠팡 등록 상품에도 즉시 반영한다.

    Celery beat로 주기 실행하면(예: 30분마다) "소싱처에서 품절됐는데 쿠팡에서는 계속
    판매중이라 고객이 주문 → 강제취소 패널티/계정정지"로 이어지는 위탁판매 최대
    리스크를 막을 수 있다. 이미 저장된 `source_url`로 재조회하기 때문에, 품번만으로
    직접 조회하는 실API가 없는 소싱처(무신사/폴더 등)는 아직 건너뛴다.

    쿠팡 동기화는 listing.vendor_item_ids_json이 채워져 있는(= 승인/판매중인) 상품에만
    적용된다 — 아직 임시저장(DRAFT) 상태인 상품은 쿠팡이 옵션ID를 발급하지 않아
    동기화할 대상 자체가 없으므로 조용히 건너뛴다.
    """
    with SessionLocalSync() as session:
        mappings = session.query(SourceMapping).all()
        refreshed, skipped, failed, synced_to_coupang = [], [], [], []

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

            old_size_stock = mapping.size_stock_json or {}
            new_size_stock = scraped.size_stock or {}
            new_selling_price = calculate_simple_markup_price(Decimal(str(scraped.price)), _DEFAULT_MARKUP_RATE)

            mapping.source_price = scraped.price
            mapping.size_stock_json = new_size_stock
            mapping.last_checked_at = datetime.now(UTC)
            refreshed.append(mapping.source_id)

            listings = (
                session.query(MarketListing)
                .filter_by(product_id=mapping.product_id, market_type=MarketType.COUPANG)
                .all()
            )
            for listing in listings:
                sync_result = sync_stock_and_price_to_coupang(
                    session=session,
                    listing=listing,
                    old_size_stock=old_size_stock,
                    new_size_stock=new_size_stock,
                    new_selling_price=new_selling_price,
                )
                if sync_result["stopped"] or sync_result["resumed"] or sync_result["price_updated"]:
                    synced_to_coupang.append({"listing_id": listing.listing_id, **sync_result})

        session.commit()
        return {
            "refreshed": refreshed,
            "skipped": skipped,
            "failed": failed,
            "synced_to_coupang": synced_to_coupang,
        }
