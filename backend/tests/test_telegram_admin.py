"""텔레그램 관리자 긴급 알림 연동 검증 (품절/발주실패 HOLD 대응 보완)."""

import pytest

from app.integrations.messaging.telegram_admin import TelegramAdminNotifier


@pytest.mark.asyncio
async def test_send_alert_mock_returns_true():
    notifier = TelegramAdminNotifier(use_mock=True)

    result = await notifier.send_alert("[보탬] 테스트 알림입니다.")

    assert result is True


def test_telegram_admin_notifier_requires_credentials_when_not_mock():
    with pytest.raises(ValueError, match="TELEGRAM"):
        TelegramAdminNotifier(use_mock=False)
