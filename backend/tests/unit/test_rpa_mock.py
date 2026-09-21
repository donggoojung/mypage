import json

import pytest

from app.integrations.rpa.base import ShippingInfo
from app.integrations.rpa.factory import _load_saved_session_cookies, get_rpa_client
from app.integrations.rpa.purchase_bot import MockRPAClient, RPAPurchaseError


@pytest.mark.asyncio
async def test_mock_rpa_client_returns_order_id_for_valid_shipping_info():
    client = MockRPAClient()
    shipping_info = ShippingInfo(recipient_name="홍길동", recipient_phone="0501-1234-5678", shipping_addr="서울시 강남구")

    order_id = await client.purchase_order("CW2288-111", "250", shipping_info)

    assert order_id.startswith("MOCK-CW2288-111-250-")


@pytest.mark.asyncio
async def test_mock_rpa_client_rejects_missing_shipping_fields():
    client = MockRPAClient()
    shipping_info = ShippingInfo(recipient_name="", recipient_phone="0501-1234-5678", shipping_addr="서울시 강남구")

    with pytest.raises(RPAPurchaseError, match="recipient_name"):
        await client.purchase_order("CW2288-111", "250", shipping_info)


def test_factory_returns_mock_client_by_default():
    client = get_rpa_client()
    assert isinstance(client, MockRPAClient)


def test_factory_respects_explicit_use_mock_false_flag():
    client = get_rpa_client(source_base_url="https://www.abcmart.com", session_cookies=[], use_mock=False)
    from app.integrations.rpa.purchase_bot import PlaywrightRPAClient

    assert isinstance(client, PlaywrightRPAClient)


@pytest.mark.asyncio
async def test_mock_rpa_client_ignores_confirm_final_payment_flag():
    """Mock은 실제 결제가 없으니 confirm_final_payment 값과 무관하게 항상 성공해야 한다."""
    client = MockRPAClient()
    shipping_info = ShippingInfo(recipient_name="홍길동", recipient_phone="0501-1234-5678", shipping_addr="서울시 강남구")

    order_id = await client.purchase_order("CW2288-111", "250", shipping_info, confirm_final_payment=False)

    assert order_id.startswith("MOCK-CW2288-111-250-")


def test_load_saved_session_cookies_returns_empty_list_when_file_missing(tmp_path):
    missing_path = tmp_path / "no-such-session.json"

    assert _load_saved_session_cookies(str(missing_path)) == []


def test_load_saved_session_cookies_reads_saved_file(tmp_path):
    session_file = tmp_path / "abc_mart_session.json"
    cookies = [{"name": "session_id", "value": "abc123", "domain": "abcmart.a-rt.com"}]
    session_file.write_text(json.dumps(cookies), encoding="utf-8")

    assert _load_saved_session_cookies(str(session_file)) == cookies


def test_factory_auto_loads_saved_session_when_not_given_explicitly(tmp_path, monkeypatch):
    session_file = tmp_path / "abc_mart_session.json"
    cookies = [{"name": "session_id", "value": "abc123", "domain": "abcmart.a-rt.com"}]
    session_file.write_text(json.dumps(cookies), encoding="utf-8")

    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "abc_mart_session_file", str(session_file))

    client = get_rpa_client(source_base_url="https://www.abcmart.com", use_mock=False)

    assert client._session_cookies == cookies
