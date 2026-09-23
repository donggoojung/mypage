#!/usr/bin/env python
"""텔레그램 관리자 긴급 알림 설정이 실제로 동작하는지, 진짜 메시지 1건을 보내서 확인한다.

사용법 (backend/ 디렉토리에서, .env에 TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_CHAT_ID를 채우고,
USE_MOCK_TELEGRAM=false로 바꾼 뒤 실행):

    python scripts/test_telegram_send.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.integrations.messaging.telegram_admin import TelegramAdminNotifier, TelegramSendError  # noqa: E402


async def main() -> None:
    settings = get_settings()
    if settings.use_mock_telegram:
        print("USE_MOCK_TELEGRAM=true 입니다 — .env에서 false로 바꾸고, 워커/서버를 재시작한 뒤 다시 실행해주세요.")
        return
    if not (settings.telegram_bot_token and settings.telegram_admin_chat_id):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_CHAT_ID 중 비어있는 값이 있습니다. .env를 확인해주세요.")
        return

    print("[진단] 텔레그램 관리자 알림 테스트 발송 시도")
    notifier = TelegramAdminNotifier(settings=settings, use_mock=False)
    try:
        result = await notifier.send_alert("[보탬] 테스트 알림입니다 — 이 메시지가 보이면 설정이 정상입니다.")
    except TelegramSendError as exc:
        print(f"[실패] 텔레그램 API가 에러를 반환했습니다: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 진단 스크립트이므로 원인을 그대로 보여준다.
        print(f"[실패] 발송 중 예외 발생: {type(exc).__name__}: {exc}")
        return

    if result:
        print("[성공] 발송 요청이 정상 처리되었습니다. 텔레그램 앱에서 봇과의 대화를 확인해주세요.")
    else:
        print("[실패] 발송 결과가 False로 반환되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
