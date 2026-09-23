"""솔라피(Solapi) 카카오 알림톡 + SMS 자동 폴백 연동 (PRD 6.2).

2026-09-23 공식 SDK 소스(GitHub solapi/solapi-python)로 확인된 실제 인증 방식/요청
구조를 그대로 따른다 — 개발자 문서 사이트(docs.solapi.com, developers.solapi.dev)는
이 샌드박스에서 접속이 막혀 있어, SDK 소스코드(solapi/lib/authenticator.py,
solapi/services/message_service.py, solapi/model/request/message.py,
solapi/model/kakao/kakao_option.py)를 직접 읽어 검증했다.

인증: Authorization 헤더에 "HMAC-SHA256 ApiKey={key}, Date={date}, salt={salt},
signature={signature}" — signature는 HMAC-SHA256(secret, date+salt)의 hex digest.

발송 엔드포인트: POST https://api.solapi.com/messages/v4/send-many/detail
바디: {"messages": [{"to", "from", "text"?, "kakaoOptions"?: {"pfId","templateId",
"variables","disableSms"}}]}

카카오 알림톡 템플릿 ID(SOLAPI_SHIPPING_TEMPLATE_ID)는 카카오 채널 등록 +
템플릿 심사 승인이 끝나야 발급되는 값이다 — 아직 승인받지 못했다면(기본 상태)
이 클라이언트는 자동으로 일반 문자(SMS)로만 발송한다. 템플릿이 있어도
`disableSms=False`로 보내, 카카오톡 미수신자에게는 솔라피가 자동으로 문자
대체발송을 해주는 기능을 그대로 활용한다.
"""

import hashlib
import hmac
import uuid
from datetime import datetime

import httpx

from app.core.config import Settings, get_settings
from app.integrations.messaging.base import BaseMessagingClient

SOLAPI_API_HOST = "https://api.solapi.com"
MESSAGE_SEND_PATH = "/messages/v4/send-many/detail"


class SolapiSendError(RuntimeError):
    """솔라피 메시지 발송 API 호출/응답 처리 중 발생한 오류."""


def build_message_payload(phone: str, sender_phone: str, template_id: str, pf_id: str, variables: dict) -> dict:
    """솔라피 메시지 발송 API의 messages[] 항목 1건을 만든다.

    template_id와 pf_id(카카오 채널 ID)가 둘 다 있어야 알림톡으로 보낸다 — 카카오
    채널/템플릿 심사가 아직 안 끝난 계정은 pf_id 또는 template_id가 비어있을 수
    있으므로, 이 경우 사람이 읽을 수 있는 일반 문자(SMS)로 자동 전환한다.
    """
    message: dict = {"to": phone, "from": sender_phone}
    if template_id and pf_id:
        message["kakaoOptions"] = {
            "pfId": pf_id,
            "templateId": template_id,
            # 실API 미검증 — 공식 SDK(pydantic validator)는 "#{변수명}" 형식으로 자동
            # 변환해주지만, 여기선 REST API를 직접 호출하므로 그 형식 그대로 만들어 보낸다.
            "variables": {f"#{{{key}}}": str(value) for key, value in variables.items()},
            "disableSms": False,  # 알림톡 실패/미수신 시 솔라피가 자동으로 문자 대체발송.
        }
    else:
        # 템플릿(카카오 채널 심사 승인)이 아직 없으면 알림톡을 시도조차 할 수 없다 —
        # 안내 내용을 사람이 읽을 수 있는 문자로 직접 조립해서 SMS로 보낸다.
        message["text"] = "\n".join(f"{key}: {value}" for key, value in variables.items())
    return message


def _build_authorization_header(api_key: str, secret_key: str) -> dict:
    date = datetime.now().astimezone().isoformat()
    salt = uuid.uuid1().hex
    signature = hmac.new(secret_key.encode("utf-8"), f"{date}{salt}".encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = f"HMAC-SHA256 ApiKey={api_key}, Date={date}, salt={salt}, signature={signature}"
    return {"Authorization": authorization, "Content-Type": "application/json"}


class SolapiMessagingClient(BaseMessagingClient):
    """솔라피 API 클라이언트 — Mock/Real 겸용."""

    def __init__(self, settings: Settings | None = None, use_mock: bool | None = None):
        self._settings = settings or get_settings()
        self._use_mock = self._settings.use_mock_messaging if use_mock is None else use_mock
        if not self._use_mock:
            if not (self._settings.solapi_api_key and self._settings.solapi_api_secret and self._settings.solapi_sender_phone):
                raise ValueError(
                    "SOLAPI_API_KEY / SOLAPI_API_SECRET / SOLAPI_SENDER_PHONE 환경변수가 설정되어 있지 않습니다."
                )

    async def send_kakao_alert(self, phone: str, template_id: str, variables: dict) -> bool:
        """카카오 알림톡을 발송하고, 실패(또는 템플릿 미설정) 시 자동으로 SMS로 대체 발송한다."""
        if self._use_mock:
            return self._mock_send(phone, template_id, variables)
        return await self._real_send(phone, template_id, variables)

    @staticmethod
    def _mock_send(phone: str, template_id: str, variables: dict) -> bool:
        channel = "카카오 알림톡" if template_id else "SMS(템플릿 미설정)"
        print(f"  [진단] (Mock) {channel} 발송: to={phone}, template_id={template_id!r}, variables={variables}", flush=True)
        return True

    async def _real_send(self, phone: str, template_id: str, variables: dict) -> bool:
        message = build_message_payload(
            phone=phone,
            sender_phone=self._settings.solapi_sender_phone,
            template_id=template_id,
            pf_id=self._settings.solapi_kakao_pfid,
            variables=variables,
        )
        payload = {"messages": [message]}
        headers = _build_authorization_header(self._settings.solapi_api_key, self._settings.solapi_api_secret)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(f"{SOLAPI_API_HOST}{MESSAGE_SEND_PATH}", headers=headers, json=payload)
            if response.status_code >= 400:
                raise SolapiSendError(f"솔라피 메시지 발송 실패 ({response.status_code}): {response.text}")
            body = response.json()

        fail_count = body.get("failedMessageList") or body.get("groupInfo", {}).get("count", {}).get("failedCount")
        if fail_count:
            raise SolapiSendError(f"솔라피 메시지 발송 실패: {body}")
        return True
