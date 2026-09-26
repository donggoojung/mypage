"""HOLD 전환 시 쿠팡 자동 판매중지 테스트 (PRD 9.2 품절 패널티 방어 1단계).

매입 실패(품절/발주오류)로 주문이 HOLD로 멈추는 순간, 그 사이즈가 쿠팡에 승인되어
있으면(vendor_item_ids_json에 값이 있으면) 즉시 판매중지 API를 호출해 추가 주문
유입을 막아야 한다.
"""

import pytest

from app.integrations.markets.coupang import CoupangWingClient
from app.models.customer_order import CustomerOrder
from app.models.enums import ListingStatus, MarketType, OrderStatus
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.services.sourcing_optimizer import NoAvailableSourceError
from app.workers.tasks import order_tasks


@pytest.fixture
def product(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.commit()
    return product


def _make_order(db_session, product, market_order_id="700000099"):
    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id=market_order_id,
        product_id=product.product_id,
        ordered_size="250",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구 테헤란로 1",
        paid_amount=192834,
    )
    db_session.add(order)
    db_session.commit()
    return order


def test_hold_calls_stop_selling_item_when_listing_is_live(product, db_session, monkeypatch):
    listing = MarketListing(
        product_id=product.product_id,
        market_type=MarketType.COUPANG,
        market_product_id="12345678",
        selling_price=139000,
        status=ListingStatus.ACTIVE,
        vendor_item_ids_json={"250": "90000001"},
    )
    db_session.add(listing)
    order = _make_order(db_session, product)

    stopped_items = []

    async def _fake_stop_selling_item(self, vendor_item_id):
        stopped_items.append(vendor_item_id)
        return {"code": "SUCCESS"}

    monkeypatch.setattr(CoupangWingClient, "stop_selling_item", _fake_stop_selling_item)

    with pytest.raises(NoAvailableSourceError):
        order_tasks.process_order(order.order_id, use_mock=True)

    db_session.refresh(order)
    assert order.status == OrderStatus.HOLD
    assert stopped_items == ["90000001"]


def test_hold_skips_stop_selling_when_listing_not_approved_yet(product, db_session, monkeypatch):
    # DRAFT 상태(vendor_item_ids_json 비어있음) — 아직 쿠팡이 옵션ID를 발급하지 않은 경우.
    listing = MarketListing(
        product_id=product.product_id,
        market_type=MarketType.COUPANG,
        market_product_id="12345678",
        selling_price=139000,
        status=ListingStatus.DRAFT,
        vendor_item_ids_json={},
    )
    db_session.add(listing)
    order = _make_order(db_session, product)

    stopped_items = []

    async def _fake_stop_selling_item(self, vendor_item_id):
        stopped_items.append(vendor_item_id)
        return {"code": "SUCCESS"}

    monkeypatch.setattr(CoupangWingClient, "stop_selling_item", _fake_stop_selling_item)

    with pytest.raises(NoAvailableSourceError):
        order_tasks.process_order(order.order_id, use_mock=True)

    db_session.refresh(order)
    assert order.status == OrderStatus.HOLD
    assert stopped_items == []
