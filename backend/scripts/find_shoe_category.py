#!/usr/bin/env python
"""쿠팡 실API로 "신발" 관련 전시카테고리 코드를 직접 찾는 진단 스크립트.

기억이나 추측이 아니라, 여러 명확한 신발 상품명으로 쿠팡 카테고리 자동추천 API를
직접 호출하고, 그 결과 코드의 고시정보가 실제로 "신발"을 포함하는지까지 확인한다.
기본 폴백 카테고리 코드(DEFAULT_DISPLAY_CATEGORY_CODE)를 정할 때, 실제로 검증된
값만 쓰기 위한 용도.

사용법:
    python scripts/find_shoe_category.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.integrations.markets.coupang import CoupangWingClient, predict_display_category_code, select_notice_category  # noqa: E402

# 신발이라는 걸 절대 헷갈릴 수 없는, 아주 명확한 상품명들로 시도한다.
CANDIDATE_PRODUCT_NAMES = [
    "나이키 에어포스1 운동화",
    "아디다스 스탠스미스 운동화",
    "나이키 운동화",
    "여성 운동화",
    "남성 운동화 신발",
    "구두",
]


async def main() -> None:
    client = CoupangWingClient(settings=get_settings())
    for name in CANDIDATE_PRODUCT_NAMES:
        print(f"\n=== 상품명: {name!r} ===")
        try:
            code = await predict_display_category_code(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  카테고리 자동추천 실패: {exc}")
            continue
        print(f"  자동추천 카테고리 코드: {code}")
        try:
            # resolve_notice_info()는 원본 JSON을 전부 화면에 찍는 진단용 함수라
            # 여기선 쓰지 않고, 같은 일을 하는 내부 함수 2개를 직접 조합해서
            # 결과 요약만 깔끔하게 출력한다.
            metadata = await client.fetch_category_metadata(code)
            notice_category_name, detail_keys = select_notice_category(metadata)
        except Exception as exc:  # noqa: BLE001
            print(f"  고시정보 조회 실패: {exc}")
            continue
        is_shoe = "신발" in notice_category_name
        print(f"  고시카테고리: {notice_category_name!r} {'← 신발 맞음!' if is_shoe else '(신발 아님)'}")
        print(f"  고시항목: {detail_keys}")


if __name__ == "__main__":
    asyncio.run(main())
