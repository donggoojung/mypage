#!/usr/bin/env python
"""솔라피(Solapi) 설정이 실제로 동작하는지, 진짜 문자 1건을 보내서 확인한다.

사용법 (backend/ 디렉토리에서, .env에 SOLAPI_API_KEY/SECRET/SENDER_PHONE을 채우고,
USE_MOCK_MESSAGING=false로 바꾼 뒤 실행):

    python scripts/test_solapi_send.py 01012345678

인자를 생략하면 SOLAPI_SENDER_PHONE(방금 등록한 본인 번호)으로 스스로에게 보낸다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.integrations.messaging.solapi import SolapiMessagingClient, SolapiSendError  # noqa: E402


async def main(phone: str) -> None:
    settings = get_settings()
    if settings.use_mock_messaging:
        print("USE_MOCK_MESSAGING=true 입니다 — .env에서 false로 바꾸고, 워커/서버를 재시작한 뒤 다시 실행해주세요.")
        return
    if not (settings.solapi_api_key and settings.solapi_api_secret and settings.solapi_sender_phone):
        print("SOLAPI_API_KEY / SOLAPI_API_SECRET / SOLAPI_SENDER_PHONE 중 비어있는 값이 있습니다. .env를 확인해주세요.")
        return

    print(f"[진단] 발신번호={settings.solapi_sender_phone} -> 수신번호={phone} 로 테스트 발송 시도")
    client = SolapiMessagingClient(settings=settings, use_mock=False)
    try:
        result = await client.send_kakao_alert(
            phone=phone,
            template_id=settings.solapi_shipping_template_id,
            variables={"고객명": "테스트", "상품명": "테스트 상품", "택배사": "CJ대한통운", "운송장번호": "123456789"},
        )
    except SolapiSendError as exc:
        print(f"[실패] 솔라피 발송 API가 에러를 반환했습니다: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 진단 스크립트이므로 원인을 그대로 보여준다.
        print(f"[실패] 발송 중 예외 발생: {type(exc).__name__}: {exc}")
        return

    if result:
        print("[성공] 발송 요청이 정상 처리되었습니다. 잠시 후 휴대폰으로 문자가 오는지 확인해주세요.")
    else:
        print("[실패] 발송 결과가 False로 반환되었습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="솔라피 실제 발송 테스트")
    parser.add_argument("phone", nargs="?", default=None, help="수신번호 (하이픈 없이 숫자만). 생략하면 발신번호로 스스로에게 보낸다.")
    args = parser.parse_args()

    target_phone = args.phone or get_settings().solapi_sender_phone
    if not target_phone:
        print("수신번호를 인자로 넘기거나, .env의 SOLAPI_SENDER_PHONE을 먼저 채워주세요.")
        sys.exit(1)

    asyncio.run(main(target_phone))
