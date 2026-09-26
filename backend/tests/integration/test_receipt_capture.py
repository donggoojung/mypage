"""결제완료 화면 영수증 자동 캡처/보관 테스트 (PRD 9.1 지식재산권 분쟁 대응).

RPA가 실제 결제를 완료했을 때 캡처한 스크린샷을 order_processor가 스토리지에 올리고
OrderFulfillment.receipt_url에 저장하는지 검증한다.
"""

import pytest

from app.integrations.rpa.base import REVIEW_ONLY_PREFIX
from app.models.customer_order import CustomerOrder
from app.models.enums import MarketType, OrderStatus, SourcePlatform
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.models.source_mapping import SourceMapping
from app.services import order_processor


class _FakeRPAClientWithReceipt:
    """실제 결제 완료 시 고정된 스크린샷 바이트를 돌려주는 테스트 전용 RPA 클라이언트."""

    def __init__(self, review_only: bool = False):
        self._review_only = review_only

    async def purchase_order(self, style_code, size, shipping_info, confirm_final_payment=False):
        if self._review_only:
            return f"{REVIEW_ONLY_PREFIX}-fake"
        return f"REAL-{style_code}-{size}-999"

    def get_last_receipt_screenshot(self):
        if self._review_only:
            return None  # 결제 전이라 아직 캡처할 화면이 없다.
        return b"fake-png-bytes"


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


async def test_process_new_order_saves_receipt_url_on_full_auto_purchase(
    received_order_with_source, db_session, monkeypatch
):
    monkeypatch.setattr(
        order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClientWithReceipt(review_only=False)
    )

    fulfillment = await order_processor.process_new_order(db_session, received_order_with_source.order_id, use_mock=True)

    assert fulfillment.receipt_url is not None
    assert fulfillment.receipt_url.endswith(".png")


async def test_pending_approval_purchase_has_no_receipt_yet(received_order_with_source, db_session, monkeypatch):
    monkeypatch.setattr(
        order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClientWithReceipt(review_only=True)
    )

    fulfillment = await order_processor.process_new_order(db_session, received_order_with_source.order_id, use_mock=True)

    assert fulfillment.receipt_url is None


async def test_approve_and_complete_purchase_saves_receipt_url(received_order_with_source, db_session, monkeypatch):
    monkeypatch.setattr(
        order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClientWithReceipt(review_only=True)
    )
    await order_processor.process_new_order(db_session, received_order_with_source.order_id, use_mock=True)

    monkeypatch.setattr(
        order_processor, "get_rpa_client", lambda **kwargs: _FakeRPAClientWithReceipt(review_only=False)
    )
    fulfillment = await order_processor.approve_and_complete_purchase(
        db_session, received_order_with_source.order_id, use_mock=True
    )

    assert fulfillment.receipt_url is not None
    assert db_session.query(OrderFulfillment).filter_by(order_id=received_order_with_source.order_id).count() == 1
