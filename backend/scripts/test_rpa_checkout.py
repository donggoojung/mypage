#!/usr/bin/env python
"""RPA 무인발주(PlaywrightRPAClient)가 실제 ABC마트에서 어디까지 동작하는지
눈으로 직접 확인하는 스크립트.

`app/integrations/rpa/purchase_bot.py`의 4개 메서드(사이즈선택→장바구니→주문하기→
배송지입력→결제)는 실제 화면 구조를 아직 확인하지 못한 "최선의 추정" 구현이다.
이 스크립트는 headless=False(브라우저 창을 화면에 띄움)로 고정해서, 어느 단계에서
멈추는지/틀리는지 사람이 직접 보고 판단할 수 있게 한다.

기본값(--confirm-final-payment 안 줌)은 실제 결제 버튼을 누르지 않고 그 직전에서
멈춘다 — 안전하게 여러 번 테스트해볼 수 있다. 흐름이 전부 맞는 걸 확인한 뒤에만
--confirm-final-payment를 주는 걸 권장한다(그 순간 진짜 결제가 나갈 수 있음).

사용법 (backend/ 디렉토리, 가상환경 활성화 상태에서 실행):
    # 1. 먼저 로그인 세션을 저장해둔다 (한 번만 하면 됨, 만료되면 다시).
    python scripts/save_abc_mart_session.py

    # 2. 실제 품번/사이즈/배송지로 흐름을 테스트한다 (결제 직전까지만, 안전).
    python scripts/test_rpa_checkout.py CW2288-111 250 --name 홍길동 --phone 01012345678 --addr "서울시 강남구 테헤란로 1"

    # 3. 흐름이 전부 맞다고 확인되면, 진짜 결제까지 테스트 (주의 — 실제 돈이 나감):
    python scripts/test_rpa_checkout.py CW2288-111 250 --name 홍길동 --phone 01012345678 --addr "..." --confirm-final-payment
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Playwright가 브라우저 서브프로세스를 띄우는 동안 stdout이 줄단위로 즉시 안 비워지고
# 뭉쳐서 나오는 경우가 있어(파워셀에서 print()한 진단 로그가 한참 뒤에야 보임), 강제로
# 줄단위 출력으로 고정한다.
sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.rpa.base import ShippingInfo  # noqa: E402
from app.integrations.rpa.factory import _load_saved_session_cookies  # noqa: E402
from app.integrations.rpa.purchase_bot import PlaywrightRPAClient, RPAPurchaseError  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("style_code", help="소싱처(ABC마트) 품번")
    parser.add_argument("size", help="구매할 사이즈 (예: 250)")
    parser.add_argument("--name", required=True, help="수령인 이름")
    parser.add_argument("--phone", required=True, help="수령인 연락처")
    parser.add_argument("--addr", required=True, help="배송지 주소")
    parser.add_argument("--message", default="", help="배송 메모 (선택)")
    parser.add_argument(
        "--confirm-final-payment",
        action="store_true",
        help="실제 결제 버튼까지 누른다 (기본은 결제 직전 단계에서 멈춤 — 반드시 여러 번 먼저 확인 후에만 사용).",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    session_cookies = _load_saved_session_cookies(settings.abc_mart_session_file)
    if not session_cookies:
        print(f"로그인 세션 파일이 없습니다: {settings.abc_mart_session_file}")
        print("먼저 python scripts/save_abc_mart_session.py 를 실행해서 로그인 세션을 저장해주세요.")
        raise SystemExit(1)

    client = PlaywrightRPAClient(
        source_base_url="https://abcmart.a-rt.com",
        session_cookies=session_cookies,
        settings=settings,
        headless=False,  # 사람이 눈으로 확인해야 하니 항상 창을 띄운다.
        # 실패한 단계에서 창을 바로 안 닫고 멈춰서, 로그인된 실제 화면을 직접 보고
        # 캡처할 시간을 준다 (이 수동 테스트 스크립트에서만 켠다).
        pause_on_error=True,
    )
    shipping_info = ShippingInfo(
        recipient_name=args.name, recipient_phone=args.phone, shipping_addr=args.addr, shipping_message=args.message
    )

    if args.confirm_final_payment:
        print("\n주의 — --confirm-final-payment가 켜져 있어 실제 결제까지 진행합니다.")
        print("5초 안에 Ctrl+C를 누르면 중단할 수 있습니다...\n")
        await asyncio.sleep(5)

    try:
        result = await client.purchase_order(
            args.style_code, args.size, shipping_info, confirm_final_payment=args.confirm_final_payment
        )
    except RPAPurchaseError as exc:
        print(f"\n실패(안전하게 멈춤, 결제는 안 나갔습니다): {exc}")
        raise SystemExit(1) from exc

    if result.startswith("REVIEW_ONLY"):
        print(f"\n결제 직전 단계까지 정상 진행했고, 최종 결제는 누르지 않았습니다. ({result})")
        print("여기까지 화면이 맞게 보였다면, --confirm-final-payment를 붙여 실제 결제까지 테스트해도 됩니다.")
    else:
        print(f"\n실제 결제가 완료됐습니다. 소싱처 주문번호: {result}")


if __name__ == "__main__":
    asyncio.run(main())
