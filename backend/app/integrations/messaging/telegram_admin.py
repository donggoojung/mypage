"""텔레그램 봇으로 대표님(관리자)에게 긴급 알림을 보낸다.

품절/발주실패 등 사람이 즉시 확인해야 하는 상황 전용 — 고객에게 가는 카카오
알림톡/SMS(app/integrations/messaging/solapi.py)와는 수신자가 다른 별개 채널이다.

2026-09-23 텔레그램 공식 문서(core.telegram.org)는 이 샌드박스에서 접속이 막혀 있어,
WebSearch로 다수의 독립 출처(공식 문서 발췌, curl 예제, GitHub 예제)를 교차 확인해
검증했다 — 대표님 실제 봇 토큰으로 실발송 테스트까지 마쳐야 완전히 검증된 것이다.

엔드포인트: POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage
바디: {"chat_id": "...", "text": "..."}
응답: {"ok": true, "result": {...}} 또는 {"ok": false, "description": "..."}
"""

import httpx

from app.core.config import Settings, get_settings

TELEGRAM_API_HOST = "https://api.telegram.org"


class TelegramSendError(RuntimeError):
    """텔레그램 알림 발송 API 호출/응답 처리 중 발생한 오류."""


class TelegramAdminNotifier:
    """관리자 긴급 알림 전용 텔레그램 클라이언트 — Mock/Real 겸용."""

    def __init__(self, settings: Settings | None = None, use_mock: bool | None = None):
        self._settings = settings or get_settings()
        self._use_mock = self._settings.use_mock_telegram if use_mock is None else use_mock
        if not self._use_mock:
            if not (self._settings.telegram_bot_token and self._settings.telegram_admin_chat_id):
                raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_CHAT_ID 환경변수가 설정되어 있지 않습니다.")

    async def send_alert(self, text: str) -> bool:
        if self._use_mock:
            return self._mock_send(text)
        return await self._real_send(text)

    @staticmethod
    def _mock_send(text: str) -> bool:
        print(f"  [진단] (Mock) 텔레그램 관리자 알림 발송:\n{text}", flush=True)
        return True

    async def _real_send(self, text: str) -> bool:
        url = f"{TELEGRAM_API_HOST}/bot{self._settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self._settings.telegram_admin_chat_id, "text": text}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)
            body = response.json()
        if not body.get("ok"):
            raise TelegramSendError(f"텔레그램 알림 발송 실패: {body}")
        return True
