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

참고: 브라우저에서 URL만 붙여넣고 버튼 클릭으로 실행하고 싶다면, 이 스크립트 대신
웹 대시보드(`uvicorn app.main:app`으로 서버를 띄운 뒤 브라우저로 접속)를 쓸 수 있다 —
같은 로직(app/services/product_pipeline.py)을 공유한다.
"""

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.product_pipeline import PipelineOptions, PipelineResult, run_pipeline_for_url  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="ABC마트 상품 상세 페이지 URL (단건 실행)")
    parser.add_argument(
        "--urls-file",
        help="한 줄에 URL 하나씩 적힌 텍스트 파일 경로 (배치 실행, `#`으로 시작하는 줄은 무시)",
    )
    parser.add_argument("--headless", action="store_true", help="브라우저 창 없이 크롤링 (기본은 창을 띄워 확인)")
    parser.add_argument(
        "--target-margin-rate",
        type=Decimal,
        default=Decimal("0.30"),
        help="정가 대비 마크업 비율 — 판매가 = 원가(정가) × (1 + 이 값) (기본 0.30 = 30%%)",
    )
    parser.add_argument(
        "--display-category-code",
        type=int,
        default=None,
        help="쿠팡 전시카테고리 코드 (생략하면 쿠팡 카테고리 자동추천 API가 상품명으로 자동으로 찾는다)",
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


def _options_from_args(args: argparse.Namespace) -> PipelineOptions:
    return PipelineOptions(
        headless=args.headless,
        target_margin_rate=args.target_margin_rate,
        display_category_code=args.display_category_code,
        category=args.category,
        color_tone=args.color_tone,
        request_approval=args.request_approval,
    )


async def main() -> None:
    args = parse_args()
    urls = _load_urls_from_file(args.urls_file) if args.urls_file else [args.url]
    options = _options_from_args(args)

    if len(urls) > 1:
        print(f"배치 모드: {len(urls)}개 URL을 순서대로 처리합니다.\n")

    succeeded: list[PipelineResult] = []
    failed: list[tuple[str, str]] = []

    for idx, url in enumerate(urls, start=1):
        if len(urls) > 1:
            print(f"\n{'=' * 60}\n[{idx}/{len(urls)}] {url}\n{'=' * 60}")
        try:
            result = await run_pipeline_for_url(url, options)
            succeeded.append(result)
            if not args.request_approval:
                print("  (--request-approval 없이 실행해서 '임시저장' 상태입니다 — 실제 판매 심사요청은 안 나갔습니다.)")
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
