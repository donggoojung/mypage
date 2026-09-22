#!/usr/bin/env python
"""WING "자동가격조정" 끄기 RPA(wing_price_lock_bot.py)가 실제 화면에서 어디까지
동작하는지 눈으로 직접 확인하는 스크립트.

메뉴 클릭/검색/토글 찾기 로직은 실제 WING 화면 구조를 아직 확인 못 한 "최선의 추정"
구현이다. headless=False로 고정해서, 어느 단계에서 멈추는지 사람이 직접 보고 판단한다.

사용법 (backend/ 디렉토리, 가상환경 활성화 상태에서 실행):
    # 1. 먼저 WING 로그인 세션을 저장해둔다 (한 번만 하면 됨, 만료되면 다시).
    python scripts/save_wing_session.py

    # 2. 실제 품번으로 테스트한다.
    python scripts/test_wing_price_lock.py HQ2312
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.integrations.rpa.wing_price_lock_bot import WingPriceLockBot, WingPriceLockError  # noqa: E402

SESSION_FILE = Path(__file__).resolve().parent / "output" / "wing_session.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("style_code", help="쿠팡에 등록된 상품의 품번 (예: HQ2312)")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not SESSION_FILE.exists():
        print(f"로그인 세션 파일이 없습니다: {SESSION_FILE}")
        print("먼저 python scripts/save_wing_session.py 를 실행해서 로그인 세션을 저장해주세요.")
        raise SystemExit(1)

    session_cookies = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    settings = get_settings()

    bot = WingPriceLockBot(settings=settings, use_mock=False, session_cookies=session_cookies)

    try:
        result = await bot.disable_auto_price_adjustment(args.style_code, pause_on_error=True)
    except WingPriceLockError as exc:
        print(f"\n실패: {exc}")
        raise SystemExit(1) from exc

    print(f"\n완료: 자동가격조정이 꺼진 상태입니다. (반환값: {result})")


if __name__ == "__main__":
    asyncio.run(main())
