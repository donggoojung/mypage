#!/usr/bin/env python
"""ABC마트 상품 URL로 [크롤링 → 마진계산 → AI이미지/상세페이지 생성 → 쿠팡 등록 준비]
전체 과정을 실행하는 엔드투엔드(E2E) 스크립트 (PRD 1~4단계 통합 실행).

URL 1개만 처리하거나(단건), 여러 URL을 텍스트 파일로 넣어 한 번에 여러 상품을
순서대로 처리할 수 있다(배치). 배치 모드에서는 상품 1개가 실패해도 나머지는
계속 진행하고, 끝에 성공/실패 요약을 보여준다.

사용법 (backend/ 디렉토리, 가상환경 활성화 상태에서 실행):
    # 단건
    python scripts/e2e_full_pipeline.py <ABC마트 상품 상세 URL>
    python scripts/e2e_full_pipeline.py <URL> --headless
    python scripts/e2e_full_pipeline.py <URL> --display-category-code 56137
    python scripts/e2e_full_pipeline.py <URL> --request-approval   # (주의) 실계정+실API면 실제 승인요청까지 나감

    # 배치 (한 줄에 URL 하나씩 적은 텍스트 파일)
    python scripts/e2e_full_pipeline.py --urls-file scripts/urls.txt --headless

기본은 전부 Mock/안전 모드로 동작한다:
  - USE_MOCK_SCRAPERS 설정과 무관하게 이 스크립트는 항상 ABCMartScraper로 "실제" 크롤링한다
    (마진 계산의 입력값이 진짜여야 의미가 있기 때문 — abcmart.com 접속이 가능한 환경에서 실행할 것).
  - AI 이미지 생성은 .env의 USE_MOCK_AI_VISION 설정을 그대로 따른다.
  - 쿠팡 등록은 .env의 USE_MOCK_MARKETS 설정을 따르고, --request-approval을 주지 않으면
    실계정이어도 "임시저장"까지만 진행하고 실제 판매 심사요청은 보내지 않는다.

DB에는 MasterProduct/SourceMapping/GeneratedAsset이 실제로 upsert된다
(fulfillment 개발 DB를 사용 — 테스트 DB가 아니다).

참고: DB에 한번 저장된 상품의 가격/재고를 "그 이후에도" 계속 최신으로 유지하려면,
이 스크립트를 반복 실행하는 대신 Celery beat로 등록된 주기 작업
(`crawl_tasks.refresh_all_source_mappings`, 기본 30분마다)을 쓴다 — 아래 안내 참고.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocalSync  # noqa: E402
from app.integrations.markets.coupang import (  # noqa: E402
    CoupangRegistrationError,
    register_product_for_master_product,
)
from app.integrations.scrapers.abc_mart import ABCMartScraper  # noqa: E402
from app.models.enums import GenerationStatus, MarketType, SourcePlatform  # noqa: E402
from app.models.generated_asset import GeneratedAsset  # noqa: E402
from app.models.master_product import MasterProduct  # noqa: E402
from app.models.source_mapping import SourceMapping  # noqa: E402
from app.services.asset_pipeline import generate_product_assets  # noqa: E402
from app.services.margin_engine import ReverseMarginError, calculate_selling_price_for_platform  # noqa: E402


@dataclass
class PipelineResult:
    url: str
    style_code: str
    listing_id: int
    market_product_id: str | None
    selling_price: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="ABC마트 상품 상세 페이지 URL (단건 실행)")
    parser.add_argument(
        "--urls-file",
        help="한 줄에 URL 하나씩 적힌 텍스트 파일 경로 (배치 실행, `#`으로 시작하는 줄은 무시)",
    )
    parser.add_argument("--headless", action="store_true", help="브라우저 창 없이 크롤링 (기본은 창을 띄워 확인)")
    parser.add_argument("--fixed-margin", type=Decimal, default=Decimal("5000"))
    parser.add_argument("--target-margin-rate", type=Decimal, default=Decimal("0.30"))
    parser.add_argument("--customer-shipping-charge", type=Decimal, default=Decimal("3000"))
    parser.add_argument("--source-shipping-cost", type=Decimal, default=Decimal("0"))
    parser.add_argument(
        "--display-category-code",
        type=int,
        default=56137,
        help="쿠팡 전시카테고리 코드 (기본값은 예시 코드 — 실제 카테고리에 맞게 지정 필요)",
    )
    parser.add_argument("--category", default="운동화", help="AI 배경 합성 프롬프트용 카테고리")
    parser.add_argument("--color-tone", default="neutral", help="AI 배경 합성 컬러톤")
    parser.add_argument(
        "--request-approval",
        action="store_true",
        help="쿠팡에 실제 판매 심사요청까지 보낸다 (기본은 임시저장까지만 — 반드시 결과를 먼저 확인한 뒤 사용).",
    )
    args = parser.parse_args()

    if not args.url and not args.urls_file:
        parser.error("URL 1개를 직접 넘기거나, --urls-file 로 여러 개를 배치 처리해야 합니다.")
    return args


def _load_urls_from_file(path: str) -> list[str]:
    # utf-8-sig: 메모장/파워셀(Out-File -Encoding utf8)이 파일 맨 앞에 넣는 보이지 않는
    # BOM 문자를 자동으로 제거한다. BOM이 남아있으면 첫 줄 URL 앞에 눈에 안 보이는 문자가
    # 붙어서 "invalid URL" 같은 알 수 없는 에러로 이어진다.
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


async def run_pipeline_for_url(url: str, args: argparse.Namespace) -> PipelineResult:
    """상품 URL 1개에 대해 [크롤링→마진계산→DB저장→AI이미지→쿠팡등록] 5단계를 전부 실행한다."""

    # --- [1/5] 실제 ABC마트 크롤링 ---
    print(f"[1/5] ABC마트 크롤링 중: {url}")
    scraper = ABCMartScraper(headless=args.headless)
    scraped = await scraper.fetch_product_by_url(url)

    if not scraped.style_code or scraped.price <= 0:
        raise ValueError(f"품번 또는 가격 추출 실패 (style_code={scraped.style_code!r}, price={scraped.price})")

    print(
        f"  브랜드={scraped.brand_name!r}, 상품명={scraped.product_name!r}, "
        f"품번={scraped.style_code}, 원가={scraped.price:,.0f}원"
    )
    print(f"  사이즈 재고: {scraped.size_stock or '(추출 실패)'}")

    # --- [2/5] 마진 엔진 (쿠팡 채널 기준 판매가) ---
    print("\n[2/5] 마진 엔진으로 쿠팡 채널 판매가 계산 중...")
    purchase_cost = Decimal(str(scraped.price))
    selling_price = calculate_selling_price_for_platform(
        market_type=MarketType.COUPANG,
        purchase_cost=purchase_cost,
        fixed_margin=args.fixed_margin,
        source_shipping_cost=args.source_shipping_cost,
        customer_shipping_charge=args.customer_shipping_charge,
        target_margin_rate=args.target_margin_rate,
    )
    print(f"  원가 {purchase_cost:,.0f}원 → 쿠팡 판매가 {selling_price:,}원")

    # --- [3/5] DB에 MasterProduct + SourceMapping upsert ---
    print("\n[3/5] DB에 상품 정보 저장 중...")
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
        print(f"  product_id={product_id} 로 저장 완료 (style_code={product.style_code})")

    # --- [4/5] AI 이미지/상세페이지 생성 ---
    print("\n[4/5] AI 이미지/상세페이지 생성 중...")
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
            product_name=scraped.product_name,
            category=args.category,
            color_tone=args.color_tone,
            specs=scraped.raw_specs or {},
            size_stock=scraped.size_stock or {},
            source_image_bytes=source_image_bytes,
        )
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
    print("\n[5/5] 쿠팡 상품 등록 준비 중...")
    listing = await asyncio.to_thread(
        _register_coupang_sync,
        product_id,
        args.display_category_code,
        selling_price,
        scraped.size_stock or {},
        args.request_approval,
    )

    print(f"  market_listing_id={listing.listing_id}")
    print(f"  쿠팡 상품ID(sellerProductId)={listing.market_product_id}")
    print(f"  상태={listing.status.value}, 등록가={listing.selling_price:,}원")
    if not args.request_approval:
        print("  (--request-approval 없이 실행해서 '임시저장' 상태입니다 — 실제 판매 심사요청은 안 나갔습니다.)")

    return PipelineResult(
        url=url,
        style_code=scraped.style_code,
        listing_id=listing.listing_id,
        market_product_id=listing.market_product_id,
        selling_price=listing.selling_price,
    )


def _register_coupang_sync(product_id, display_category_code, selling_price, size_stock, request_approval):
    """register_product_for_master_product는 내부적으로 asyncio.run()을 쓰는 동기 함수라,
    이미 이벤트 루프가 돌고 있는 이 스크립트(async main) 안에서 직접 부르면
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


async def main() -> None:
    args = parse_args()
    urls = _load_urls_from_file(args.urls_file) if args.urls_file else [args.url]

    if len(urls) > 1:
        print(f"배치 모드: {len(urls)}개 URL을 순서대로 처리합니다.\n")

    succeeded: list[PipelineResult] = []
    failed: list[tuple[str, str]] = []

    for idx, url in enumerate(urls, start=1):
        if len(urls) > 1:
            print(f"\n{'=' * 60}\n[{idx}/{len(urls)}] {url}\n{'=' * 60}")
        try:
            result = await run_pipeline_for_url(url, args)
            succeeded.append(result)
        except Exception as exc:
            print(f"\n실패: {exc}")
            failed.append((url, str(exc)))

    if len(urls) > 1:
        print(f"\n{'=' * 60}\n=== 배치 처리 요약: 성공 {len(succeeded)}건 / 실패 {len(failed)}건 ===")
        for result in succeeded:
            print(f"  ✓ {result.style_code}: listing_id={result.listing_id}, 판매가={result.selling_price:,}원")
        for url, error in failed:
            print(f"  ✗ {url}: {error}")
    else:
        print("\n=== E2E 파이프라인 완료 ===")

    if failed and not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
