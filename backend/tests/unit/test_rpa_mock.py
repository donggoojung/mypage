import pytest

from app.integrations.rpa.base import ShippingInfo
from app.integrations.rpa.factory import get_rpa_client
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
