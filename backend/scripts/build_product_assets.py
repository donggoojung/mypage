#!/usr/bin/env python
"""로컬 이미지 1장으로 AI 누끼→배경합성→상세페이지 렌더링 파이프라인을 실행해본다 (PRD 3장).

사용법 (backend/ 디렉토리에서 실행):
    python scripts/build_product_assets.py <원본 이미지 경로>
    python scripts/build_product_assets.py sample.jpg --category 운동화 --color-tone beige
    python scripts/build_product_assets.py sample.jpg --style-code CW2288-111

기본은 Mock 모드(.env의 USE_MOCK_AI_VISION=true)로 동작해 빠르게 파이프라인 전체를 확인할 수 있다.
실제 rembg/Replicate를 쓰려면 .env에서 USE_MOCK_AI_VISION=false 로 바꾸고
REPLICATE_API_TOKEN을 채운 뒤 다시 실행한다 (첫 실행은 rembg 모델 다운로드로 다소 오래 걸릴 수 있다).

결과 이미지는 scripts/output/ 아래에 저장되며, 콘솔에는 (Mock 모드일 때) 업로드된 가짜 CDN URL이 출력된다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.asset_pipeline import generate_product_assets  # noqa: E402
from app.integrations.storage.s3_client import get_storage_client, MockS3StorageClient  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

DEFAULT_SPECS = {"소재": "합성섬유/천연가죽", "제조국": "베트남", "색상": "화이트"}
DEFAULT_SIZE_STOCK = {
    "250": {"stock": 5, "is_sold_out": False},
    "260": {"stock": 0, "is_sold_out": True},
    "270": {"stock": 2, "is_sold_out": False},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image_path", help="원본 상품 이미지 파일 경로 (jpg/png)")
    parser.add_argument("--style-code", default="TEST-0001", help="품번 (기본 TEST-0001)")
    parser.add_argument("--brand-name", default="테스트브랜드", help="브랜드명")
    parser.add_argument("--product-name", default="테스트 상품", help="상품명")
    parser.add_argument("--category", default="운동화", help="AI 배경 합성 프롬프트에 쓰일 카테고리")
    parser.add_argument("--color-tone", default="white", help="배경 합성 컬러톤 (white/black/beige/neutral)")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    image_bytes = Path(args.image_path).read_bytes()

    print(f"[1/1] AI 이미지 생성 파이프라인 실행 중 (품번: {args.style_code})...")
    result = await generate_product_assets(
        style_code=args.style_code,
        brand_name=args.brand_name,
        product_name=args.product_name,
        category=args.category,
        color_tone=args.color_tone,
        specs=DEFAULT_SPECS,
        size_stock=DEFAULT_SIZE_STOCK,
        source_image_bytes=image_bytes,
    )

    print("\n=== 결과 ===")
    print(f"썸네일(누끼+배경합성): {result.thumbnail_url}")
    print(f"상세페이지(WebP)     : {result.detail_page_url}")

    storage = get_storage_client()
    if isinstance(storage, MockS3StorageClient):
        OUTPUT_DIR.mkdir(exist_ok=True)
        for key, data in storage.uploaded.items():
            out_path = OUTPUT_DIR / key.replace("/", "_")
            out_path.write_bytes(data)
            print(f"  → 로컬 저장: {out_path}")
        print(f"\nMock 모드라 실제 업로드는 안 됐고, 결과 파일을 {OUTPUT_DIR}/ 에 저장해뒀습니다. 열어서 확인해보세요.")


if __name__ == "__main__":
    asyncio.run(main())
