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
    predict_display_category_code,
    register_product_for_master_product,
)
from app.integrations.scrapers.abc_mart import ABCMartScraper
from app.models.enums import GenerationStatus, MarketType, SourcePlatform
from app.models.generated_asset import GeneratedAsset
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services.asset_pipeline import generate_product_assets
from app.services.margin_engine import (
    COUPANG_OUTBOUND_SHIPPING_COST,
    calculate_coupang_selling_price,
    calculate_selling_price_for_platform,
)

# 쿠팡 카테고리 자동추천 API가 실패했을 때(예: 계정에 해당 API 권한이 없어 403) 쓰는
# 폴백 값 — "운동화" 전시카테고리 예시 코드. 정확한 카테고리가 중요하면 고급 옵션에서
# display_category_code를 직접 지정해야 한다.
DEFAULT_DISPLAY_CATEGORY_CODE = 56137


@dataclass
class PipelineOptions:
    headless: bool = True
    # None(기본값)이면 사용자가 확정한 쿠팡 구간별 목표마진율 정책
    # (calculate_coupang_selling_price)을 그대로 쓴다 — 매입원가 5만원 이하 30%,
    # 5~9.9만원 25%, 10~15만원 20%, 15만원 초과 15%, 최소 고정마진 1만원 보장.
    # 값을 직접 넣으면(고급 옵션) 그 마진율 하나로 전 구간에 강제 적용한다.
    target_margin_rate: Decimal | None = None
    # None(기본값)이면 쿠팡 카테고리 자동추천 API로 상품명에 맞는 코드를 자동으로 찾는다.
    # 값을 직접 넣으면(사용자가 고급 옵션에 입력) 자동추천 없이 그 값을 그대로 쓴다.
    display_category_code: int | None = None
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

    # --- [2/5] 판매가 계산: 역마진 방지 공식 (쿠팡 수수료 11.88% + 택배비 반영) ---
    print("[2/5] 판매가 계산 중...")
    purchase_cost = Decimal(str(scraped.price))
    if options.target_margin_rate is not None:
        selling_price = calculate_selling_price_for_platform(
            market_type=MarketType.COUPANG,
            purchase_cost=purchase_cost,
            target_margin_rate=options.target_margin_rate,
            source_shipping_cost=COUPANG_OUTBOUND_SHIPPING_COST,
        )
        print(
            f"  원가(정가) {purchase_cost:,.0f}원, 수동 지정 목표마진율 {options.target_margin_rate:.0%} "
            f"→ 쿠팡 판매가 {selling_price:,}원"
        )
    else:
        selling_price = calculate_coupang_selling_price(purchase_cost)
        print(
            f"  원가(정가) {purchase_cost:,.0f}원, 구간별 자동 목표마진율(최소 고정마진 1만원 보장) "
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
    display_category_code = options.display_category_code
    if display_category_code is None:
        try:
            display_category_code = await predict_display_category_code(f"{brand_name} {product_name}")
            print(f"  전시카테고리 자동추천: {display_category_code} (상품명 기반, 수동 지정 안 함)")
        except (CoupangRegistrationError, httpx.HTTPError) as exc:
            # 카테고리 자동추천 API가 실API 미검증 상태라(계정별 별도 승인이 필요할 수 있음),
            # 이게 실패했다고 등록 전체를 막으면 안 된다 — 기본값으로 넘어가고, 정확한
            # 카테고리가 중요한 상품이면 대시보드 고급 옵션에서 직접 코드를 지정하라고 안내한다.
            display_category_code = DEFAULT_DISPLAY_CATEGORY_CODE
            print(f"  전시카테고리 자동추천 실패({exc}) — 기본값 {display_category_code}로 등록합니다.")
            print("  정확한 카테고리가 필요하면 고급 옵션에서 직접 코드를 지정해주세요.")
    try:
        listing = await asyncio.to_thread(
            _register_coupang_sync,
            product_id,
            display_category_code,
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
