#!/usr/bin/env python
"""ABC마트 카테고리/섹션 목록 페이지에서 상품 URL을 모아 파일로 저장한다.

"ABC마트 전체 상품을 한 번에 다 올려줘"는 의도적으로 지원하지 않는다:
  - ABC마트 이용약관상 대량 자동수집은 정상적인 개인 구매/열람 범위를 벗어난다.
  - 쿠팡 등 오픈마켓도 한 계정에서 짧은 시간에 수천 개가 갑자기 등록되면
    어뷰징으로 판단해 계정이 정지될 위험이 있다.
  - 무엇보다, 수백~수천 개를 한 번에 올리면 품절/오분류/저마진 상품이 섞여도
    사람이 확인할 방법이 없다 — 소규모 사업 운영에는 오히려 리스크만 커진다.

대신 "섹션(카테고리)별로, 한 번에 정해진 개수만" 후보 URL을 모으고, 사람이 파일을
한번 훑어본 뒤 배치 등록하는 2단계 흐름을 쓴다.

사용법 (backend/ 디렉토리에서):
    python scripts/discover_category_urls.py <카테고리/섹션 목록 페이지 URL>
    python scripts/discover_category_urls.py <URL> --max 30 --headless
    python scripts/discover_category_urls.py <URL> --out scripts/urls_nike_shoes.txt

주의 — 실사이트 미검증: 목록 페이지의 상품 링크 셀렉터
(app/integrations/scrapers/abc_mart.py의 SELECTOR_PRODUCT_LINK)는 상세 페이지
셀렉터처럼 devtools로 확인된 값이 아니다. 결과가 0개면 실제 목록 페이지를 F12로
열어 상품 링크의 실제 구조를 확인해서 조정해야 한다.

다음 단계: 이 스크립트가 만든 파일을 한번 열어보고 문제없으면
    python scripts/e2e_full_pipeline.py --urls-file <저장한 파일> --headless
로 실제 등록을 진행한다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.scrapers.abc_mart import ABCMartScraper  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("category_url", help="ABC마트 카테고리/섹션 목록 페이지 URL")
    parser.add_argument(
        "--max", type=int, default=30, help="가져올 상품 URL 최대 개수 (기본 30 — 한 번에 너무 많이 모으지 않도록 제한)"
    )
    parser.add_argument("--out", default="scripts/urls.txt", help="저장할 파일 경로 (기본 scripts/urls.txt)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="몇 페이지까지 넘기며 모을지 (기본 10). 사이트가 `?page=N` 방식이 아니라 "
        "rowsPerPage처럼 한 URL에 개수를 지정하는 방식이면 1로 두고, URL에 직접 개수를 늘려서 넣는다.",
    )
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    scraper = ABCMartScraper(headless=args.headless)

    print(f"카테고리 페이지에서 상품 URL 수집 중: {args.category_url} (최대 {args.max}개)")
    urls = await scraper.fetch_category_product_urls(
        args.category_url, max_products=args.max, max_pages=args.max_pages
    )

    if not urls:
        print("\n상품 URL을 하나도 못 찾았습니다 — 카테고리 페이지 구조가 예상과 다를 수 있습니다.")
        print("→ app/integrations/scrapers/abc_mart.py 의 SELECTOR_PRODUCT_LINK 를 devtools로 확인해 조정해주세요.")
        raise SystemExit(1)

    out_path = Path(args.out)
    out_path.write_text("\n".join(urls) + "\n", encoding="utf-8")

    print(f"\n{len(urls)}개 상품 URL을 {out_path} 에 저장했습니다.")
    print("바로 등록하지 말고, 먼저 파일을 열어서 원치 않는 상품이 섞이지 않았는지 확인해주세요.")
    print(f"문제없으면 아래 명령으로 배치 등록을 진행합니다:\n  python scripts/e2e_full_pipeline.py --urls-file {out_path} --headless")


if __name__ == "__main__":
    asyncio.run(main())
