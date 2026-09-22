#!/usr/bin/env python
"""쿠팡 WING(판매자센터)에 로그인한 뒤, 그 로그인 상태(쿠키)를 파일로 저장하는 스크립트.

자동가격조정을 대신 꺼주는 RPA(wing_price_lock_bot.py)가 WING 화면에 들어가려면
"로그인된 상태"가 필요하다. 아이디/비밀번호를 코드에 직접 넣는 대신, 이 스크립트로
사람이 직접 브라우저 창에서 로그인하고 나면 그 로그인 상태(쿠키)만 파일로 저장해서
재사용한다.

사용법 (backend/ 디렉토리, 가상환경 활성화 상태에서 실행):
    python scripts/save_wing_session.py

실행하면 브라우저 창이 하나 뜬다. 그 창에서:
    1. 평소 쓰는 쿠팡 WING 계정으로 로그인한다.
    2. 로그인이 완료된 걸 확인했으면, 이 창(파워셀)으로 돌아와서 Enter를 누른다.

그러면 로그인 상태가 scripts/output/wing_session.json 파일로 저장된다.
이 파일은 절대 깃허브에 올라가지 않는다(.gitignore에 scripts/output/이 이미 등록되어 있음).

세션은 로그아웃하거나 일정 기간이 지나면 만료될 수 있다 — RPA가 "세션 만료" 비슷한
에러로 실패하면, 이 스크립트를 다시 실행해서 새로 저장하면 된다.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.scrapers.stealth import STEALTH_INIT_SCRIPT, chromium_launch_kwargs, new_context_kwargs  # noqa: E402

WING_HOME_URL = "https://wing.coupang.com/"
OUTPUT_PATH = Path(__file__).resolve().parent / "output" / "wing_session.json"


async def main() -> None:
    from playwright.async_api import async_playwright

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        # headless=False 고정 — 사람이 직접 로그인해야 하는 창이라 항상 화면에 띄운다.
        browser = await playwright.chromium.launch(**chromium_launch_kwargs(headless=False))
        try:
            context = await browser.new_context(**new_context_kwargs())
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            page = await context.new_page()
            await page.goto(WING_HOME_URL, wait_until="domcontentloaded", timeout=20000)

            print("\n브라우저 창이 열렸습니다.")
            print("1) 그 창에서 평소 쓰는 쿠팡 WING 계정으로 로그인하세요.")
            print("2) 로그인이 끝났으면, 이 창(파워셀)으로 돌아와서 Enter를 눌러주세요.\n")
            await asyncio.to_thread(input, "로그인 완료 후 Enter >>> ")

            cookies = await context.cookies()
            OUTPUT_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n저장 완료: {OUTPUT_PATH} (쿠키 {len(cookies)}개)")
            print("이제 scripts/test_wing_price_lock.py로 자동가격조정 끄기 RPA를 테스트할 수 있습니다.")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
