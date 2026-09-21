"""ABC마트 URL 1개로 [크롤링 → 마진계산 → DB저장 → AI이미지 생성 → 쿠팡 등록]
전체 과정을 실행하는 핵심 로직 (PRD 1~4단계 통합).

`scripts/e2e_full_pipeline.py`(CLI)와 `app/workers/tasks/pipeline_tasks.py`
(웹 대시보드에서 호출하는 Celery 태스크) 양쪽이 이 모듈을 공유한다 — 로직은 여기
한 곳에만 있고, 두 진입점은 입출력 형태만 다르게 감싼다.
"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.core.database import SessionLocalSync
from app.integrations.markets.coupang import (
    CoupangRegistrationError,
    register_product_for_master_product,
)
from app.integrations.scrapers.abc_mart import ABCMartScraper
from app.models.enums import GenerationStatus, SourcePlatform
from app.models.generated_asset import GeneratedAsset
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services.asset_pipeline import generate_product_assets
from app.services.margin_engine import calculate_simple_markup_price


@dataclass
class PipelineOptions:
    headless: bool = True
    # 정가(크롤링된 표시가) 대비 단순 마크업 비율 — 판매가 = 원가 × (1 + target_margin_rate).
    target_margin_rate: Decimal = Decimal("0.30")
    display_category_code: int = 56137
    category: str = "운동화"
    color_tone: str = "neutral"
    request_approval: bool = False


@dataclass
class PipelineResult:
    url: str
    style_code: str
    brand_name: str
    product_name: str
    purchase_cost: Decimal
    selling_price: Decimal
    thumbnail_url: str | None
    listing_id: int
    market_product_id: str | None
    listing_status: str


class PipelineError(RuntimeError):
    """파이프라인 5단계 중 한 곳이라도 실패하면 발생한다."""


async def run_pipeline_for_url(url: str, options: PipelineOptions | None = None) -> PipelineResult:
    """상품 URL 1개에 대해 [크롤링→마진계산→DB저장→AI이미지→쿠팡등록] 5단계를 전부 실행한다."""
    options = options or PipelineOptions()

    # --- [1/5] 실제 ABC마트 크롤링 ---
    print(f"[1/5] ABC마트 크롤링 중: {url}")
    scraper = ABCMartScraper(headless=options.headless)
    scraped = await scraper.fetch_product_by_url(url)

    if not scraped.style_code or scraped.price <= 0:
        raise PipelineError(f"품번 또는 가격 추출 실패 (style_code={scraped.style_code!r}, price={scraped.price})")

    print(
        f"  브랜드={scraped.brand_name!r}, 상품명={scraped.product_name!r}, "
        f"품번={scraped.style_code}, 원가={scraped.price:,.0f}원"
    )

    # --- [2/5] 판매가 계산: 정가 × (1 + 목표 마진율) 단순 마크업 ---
    print("[2/5] 판매가 계산 중...")
    purchase_cost = Decimal(str(scraped.price))
    selling_price = calculate_simple_markup_price(purchase_cost, options.target_margin_rate)
    print(
        f"  원가(정가) {purchase_cost:,.0f}원 × (1+{options.target_margin_rate:.0%}) "
        f"→ 쿠팡 판매가 {selling_price:,}원"
    )

    # --- [3/5] DB에 MasterProduct + SourceMapping upsert ---
    print("[3/5] DB에 상품 정보 저장 중...")
    with SessionLocalSync() as session:
        product = session.query(MasterProduct).filter_by(style_code=scraped.style_code).first()
        if product is None:
            product = MasterProduct(style_code=scraped.style_code)
            session.add(product)
        product.brand_name = scraped.brand_name or product.brand_name or "미상"
        product.product_name = scraped.product_name or product.product_name or scraped.style_code
        product.raw_specs_json = scraped.raw_specs or product.raw_specs_json or {}
        session.flush()

        mapping = (
            session.query(SourceMapping)
            .filter_by(product_id=product.product_id, source_platform=SourcePlatform.ABC_MART)
            .first()
        )
        if mapping is None:
            mapping = SourceMapping(product_id=product.product_id, source_platform=SourcePlatform.ABC_MART)
            session.add(mapping)
        mapping.source_url = url
        mapping.source_image_url = scraped.image_url
        mapping.source_price = scraped.price
        mapping.size_stock_json = scraped.size_stock
        session.commit()
        product_id = product.product_id
        brand_name = product.brand_name
        product_name = product.product_name

    # --- [4/5] AI 이미지/상세페이지 생성 ---
    print("[4/5] AI 이미지/상세페이지 생성 중...")
    thumbnail_url: str | None = None
    if not scraped.image_url:
        print("  원본 이미지 URL이 없어 AI 이미지 생성을 건너뜁니다.")
    else:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(scraped.image_url)
            response.raise_for_status()
            source_image_bytes = response.content

        assets = await generate_product_assets(
            style_code=scraped.style_code,
            brand_name=brand_name,
            product_name=product_name,
            category=options.category,
            color_tone=options.color_tone,
            specs=scraped.raw_specs or {},
            size_stock=scraped.size_stock or {},
            source_image_bytes=source_image_bytes,
        )
        thumbnail_url = assets.thumbnail_url
        print(f"  썸네일     : {assets.thumbnail_url}")
        print(f"  상세페이지 : {assets.detail_page_url}")

        with SessionLocalSync() as session:
            asset = session.query(GeneratedAsset).filter_by(product_id=product_id).first()
            if asset is None:
                asset = GeneratedAsset(product_id=product_id)
                session.add(asset)
            asset.ai_thumbnail_url = assets.thumbnail_url
            asset.ai_detail_image_url = assets.detail_page_url
            asset.exif_cleared = True
            asset.generation_status = GenerationStatus.COMPLETED
            session.commit()

    # --- [5/5] 쿠팡 등록 준비/실행 ---
    print("[5/5] 쿠팡 상품 등록 준비 중...")
    try:
        listing = await asyncio.to_thread(
            _register_coupang_sync,
            product_id,
            options.display_category_code,
            selling_price,
            scraped.size_stock or {},
            options.request_approval,
        )
    except (ValueError, CoupangRegistrationError) as exc:
        raise PipelineError(f"쿠팡 등록 실패: {exc}") from exc
    print(f"  market_listing_id={listing.listing_id}, 상태={listing.status.value}")

    return PipelineResult(
        url=url,
        style_code=scraped.style_code,
        brand_name=brand_name,
        product_name=product_name,
        purchase_cost=purchase_cost,
        selling_price=listing.selling_price,
        thumbnail_url=thumbnail_url,
        listing_id=listing.listing_id,
        market_product_id=listing.market_product_id,
        listing_status=listing.status.value,
    )


def _register_coupang_sync(product_id, display_category_code, selling_price, size_stock, request_approval):
    """register_product_for_master_product는 내부적으로 asyncio.run()을 쓰는 동기 함수라,
    이미 이벤트 루프가 돌고 있는 호출자(스크립트의 async main, 혹은 Celery 태스크가
    asyncio.run으로 감싼 코루틴) 안에서 직접 부르면
    "asyncio.run() cannot be called from a running event loop" 에러가 난다.
    별도 스레드(asyncio.to_thread)에서 새 DB 세션과 함께 실행해 이 충돌을 피한다.
    """
    with SessionLocalSync() as session:
        return register_product_for_master_product(
            session=session,
            product_id=product_id,
            display_category_code=display_category_code,
            selling_price=selling_price,
            size_stock=size_stock,
            request_approval=request_approval,
        )
