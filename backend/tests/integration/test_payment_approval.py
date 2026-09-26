"""반자동 결제 승인 흐름 테스트 (RPA_CONFIRM_FINAL_PAYMENT=false 기본값 대응).

RPA가 배송지 입력까지 마치고 실제 결제 버튼 직전에서 멈추면(REVIEW_ONLY_PREFIX) 에러(HOLD)가
아니라 PENDING_PAYMENT_APPROVAL로 전환되고, 관리자가 [결제 승인]을 눌러야만
approve_and_complete_purchase()가 실제 결제를 이어서 완료하는지 검증한다.
"""

import pytest

from app.integrations.rpa.base import REVIEW_ONLY_PREFIX, RPAPurchaseError
from app.models.customer_order import CustomerOrder
from app.models.enums import MarketType, OrderStatus, SourcePlatform
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.models.source_mapping import SourceMapping
from app.services import order_processor


class _FakeRPAClient:
    """purchase_order 반환값을 원하는 대로 고정하는 테스트 전용 RPA 클라이언트."""

    def __init__(self, review_only: bool = False, raise_error: bool = False):
        self._review_only = review_only
        self._raise_error = raise_error

    async def purchase_order(self, style_code, size, shipping_info, confirm_final_payment=False):
        if self._raise_error:
            raise RPAPurchaseError("가짜 RPA 실패(테스트용)")
        if self._review_only:
            return f"{REVIEW_ONLY_PREFIX}-fake"
        return f"REAL-{style_code}-{size}-999"


@pytest.fixture
def product(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.commit()
    return product


@pytest.fixture
def received_order_with_source(product, db_session):
    db_session.add(
        SourceMapping(
            product_id=product.product_id,
            source_platform=SourcePlatform.ABC_MART,
            source_url="https://mock.abcmart.example.com/products/CW2288-111",
            source_price=139000.0,
            size_stock_json={"250": {"stock": 5, "is_sold_out": False}},
        )
    )
    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id="700000001",
        product_id=product.product_id,
        ordered_size="250",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구 테헤란로 1 101호",
        paid_amount=192834,
    )
    db_session.add(order)
    db_session.commit()
    return order


async def test_process_new_order_sets_pending_approval_when_rpa_stops_before_payment(
    received_order_with_source, db_session, monkeypatch
):
    monkeypatch.setattr(order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClient(review_only=True))

    fulfillment = await order_processor.process_new_order(db_session, received_order_with_source.order_id, use_mock=True)

    assert fulfillment.source_order_id is None
    assert fulfillment.cost_paid == 139000.0

    db_session.refresh(received_order_with_source)
    assert received_order_with_source.status == OrderStatus.PENDING_PAYMENT_APPROVAL


async def test_approve_and_complete_purchase_completes_payment(received_order_with_source, db_session, monkeypatch):
    monkeypatch.setattr(order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClient(review_only=True))
    await order_processor.process_new_order(db_session, received_order_with_source.order_id, use_mock=True)
    db_session.refresh(received_order_with_source)
    assert received_order_with_source.status == OrderStatus.PENDING_PAYMENT_APPROVAL

    monkeypatch.setattr(order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClient(review_only=False))
    fulfillment = await order_processor.approve_and_complete_purchase(
        db_session, received_order_with_source.order_id, use_mock=True
    )

    assert fulfillment.source_order_id == "REAL-CW2288-111-250-999"
    db_session.refresh(received_order_with_source)
    assert received_order_with_source.status == OrderStatus.ORDER_PURCHASED

    assert db_session.query(OrderFulfillment).filter_by(order_id=received_order_with_source.order_id).count() == 1


async def test_approve_and_complete_purchase_rejects_non_pending_order(received_order_with_source, db_session):
    with pytest.raises(order_processor.OrderProcessingError):
        await order_processor.approve_and_complete_purchase(db_session, received_order_with_source.order_id, use_mock=True)


async def test_approve_and_complete_purchase_holds_on_rpa_failure(received_order_with_source, db_session, monkeypatch):
    monkeypatch.setattr(order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClient(review_only=True))
    await order_processor.process_new_order(db_session, received_order_with_source.order_id, use_mock=True)

    monkeypatch.setattr(order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClient(raise_error=True))
    with pytest.raises(RPAPurchaseError):
        await order_processor.approve_and_complete_purchase(db_session, received_order_with_source.order_id, use_mock=True)

    db_session.refresh(received_order_with_source)
    assert received_order_with_source.status == OrderStatus.HOLD
