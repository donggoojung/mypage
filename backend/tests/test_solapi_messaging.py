"""솔라피(Solapi) 카카오 알림톡 + SMS 자동 폴백 연동 검증 (PRD 6.2)."""

import pytest

from app.integrations.messaging.solapi import SolapiMessagingClient, build_message_payload


def test_build_message_payload_uses_kakao_options_when_template_and_pfid_present():
    message = build_message_payload(
        phone="01012345678",
        sender_phone="0212345678",
        template_id="TEMPLATE-001",
        pf_id="PF-001",
        variables={"고객명": "홍길동", "운송장번호": "123456789"},
    )

    assert message["to"] == "01012345678"
    assert message["from"] == "0212345678"
    assert "text" not in message
    assert message["kakaoOptions"]["pfId"] == "PF-001"
    assert message["kakaoOptions"]["templateId"] == "TEMPLATE-001"
    assert message["kakaoOptions"]["variables"] == {"#{고객명}": "홍길동", "#{운송장번호}": "123456789"}
    # 알림톡 미수신 시 솔라피가 자동으로 문자 대체발송을 하도록 항상 꺼둬야 한다.
    assert message["kakaoOptions"]["disableSms"] is False


def test_build_message_payload_falls_back_to_plain_sms_when_template_missing():
    """템플릿 심사가 아직 안 끝났으면(template_id 비어있음) 문자로 대체 발송해야 한다."""
    message = build_message_payload(
        phone="01012345678",
        sender_phone="0212345678",
        template_id="",
        pf_id="PF-001",
        variables={"고객명": "홍길동", "운송장번호": "123456789"},
    )

    assert "kakaoOptions" not in message
    assert "홍길동" in message["text"]
    assert "123456789" in message["text"]


def test_build_message_payload_falls_back_to_plain_sms_when_pfid_missing():
    """카카오 채널(pf_id)이 아직 연동 안 되어 있으면 template_id가 있어도 문자로 대체해야 한다."""
    message = build_message_payload(
        phone="01012345678",
        sender_phone="0212345678",
        template_id="TEMPLATE-001",
        pf_id="",
        variables={"고객명": "홍길동"},
    )

    assert "kakaoOptions" not in message
    assert "text" in message


@pytest.mark.asyncio
async def test_send_kakao_alert_mock_returns_true():
    client = SolapiMessagingClient(use_mock=True)

    result = await client.send_kakao_alert(
        phone="01012345678",
        template_id="TEMPLATE-001",
        variables={"고객명": "홍길동", "택배사": "CJ대한통운", "운송장번호": "123456789"},
    )

    assert result is True


def test_solapi_messaging_client_requires_credentials_when_not_mock():
    with pytest.raises(ValueError, match="SOLAPI"):
        SolapiMessagingClient(use_mock=False)
