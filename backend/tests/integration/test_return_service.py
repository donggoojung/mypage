"""쿠팡 취소/반품 감지 및 역물류 처리 서비스 테스트 (PRD 7장).

실제 쿠팡 API 대신 CoupangWingClient의 fetch_cancelled_order_sheets/fetch_return_requests를
monkeypatch로 대체해, "매입 전/후 취소 구분", "반품 접수 → 검수 → 소싱처 반품 완료"
상태 전이 로직 자체를 검증한다.
"""

import pytest

from app.integrations.markets.coupang import CoupangWingClient
from app.models.customer_order import CustomerOrder
from app.models.enums import InspectionStatus, MarketType, OrderStatus
from app.models.master_product import MasterProduct
from app.models.return_request import ReturnRequest
from app.services import return_service


def _async_return(value):
    """monkeypatch로 CoupangWingClient의 async 메서드를 고정값 반환으로 대체하는 헬퍼."""

    async def _fn(self, *args, **kwargs):
        return value

    return _fn


@pytest.fixture
def product(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.commit()
    return product


def _make_order(db_session, product, market_order_id, status):
    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id=market_order_id,
        product_id=product.product_id,
        ordered_size="250",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구 테헤란로 1 101호",
        paid_amount=192834,
        status=status,
    )
    db_session.add(order)
    db_session.commit()
    return order


async def test_cancel_before_purchase_marks_cancelled_without_alert(product, db_session, monkeypatch):
    order = _make_order(db_session, product, "700000001", OrderStatus.RECEIVED)

    monkeypatch.setattr(
        CoupangWingClient, "fetch_cancelled_order_sheets", _async_return([{"orderId": 700000001}])
    )
    monkeypatch.setattr(CoupangWingClient, "fetch_return_requests", _async_return([]))

    result = await return_service.detect_cancellations_and_returns(db_session, use_mock=True)

    assert result["cancelled_before_purchase"] == ["700000001"]
    assert result["cancelled_after_purchase_urgent"] == []
    db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED


async def test_cancel_after_purchase_marks_cancel_requested_and_alerts(product, db_session, monkeypatch):
    order = _make_order(db_session, product, "700000002", OrderStatus.ORDER_PURCHASED)

    monkeypatch.setattr(
        CoupangWingClient, "fetch_cancelled_order_sheets", _async_return([{"orderId": 700000002}])
    )
    monkeypatch.setattr(CoupangWingClient, "fetch_return_requests", _async_return([]))

    alerts = []

    async def _fake_send_alert(self, text):
        alerts.append(text)
        return True

    from app.integrations.messaging.telegram_admin import TelegramAdminNotifier

    monkeypatch.setattr(TelegramAdminNotifier, "send_alert", _fake_send_alert)

    result = await return_service.detect_cancellations_and_returns(db_session, use_mock=True)

    assert result["cancelled_after_purchase_urgent"] == ["700000002"]
    db_session.refresh(order)
    assert order.status == OrderStatus.CANCEL_REQUESTED
    assert len(alerts) == 1
    assert "긴급" in alerts[0]


async def test_cancel_polling_is_idempotent(product, db_session, monkeypatch):
    _make_order(db_session, product, "700000003", OrderStatus.RECEIVED)

    monkeypatch.setattr(
        CoupangWingClient, "fetch_cancelled_order_sheets", _async_return([{"orderId": 700000003}])
    )
    monkeypatch.setattr(CoupangWingClient, "fetch_return_requests", _async_return([]))

    await return_service.detect_cancellations_and_returns(db_session, use_mock=True)
    second_result = await return_service.detect_cancellations_and_returns(db_session, use_mock=True)

    assert second_result["cancelled_before_purchase"] == []


async def test_new_return_request_creates_record_and_sets_status(product, db_session, monkeypatch):
    order = _make_order(db_session, product, "700000004", OrderStatus.DELIVERED)

    monkeypatch.setattr(CoupangWingClient, "fetch_cancelled_order_sheets", _async_return([]))
    monkeypatch.setattr(
        CoupangWingClient,
        "fetch_return_requests",
        _async_return([{"orderId": 700000004, "receiptId": "R1", "reasonName": "사이즈 불만족"}]),
    )

    result = await return_service.detect_cancellations_and_returns(db_session, use_mock=True)

    assert result["new_return_requests"] == ["700000004"]
    db_session.refresh(order)
    assert order.status == OrderStatus.RETURN_REQUESTED

    return_request = db_session.query(ReturnRequest).filter_by(order_id=order.order_id).first()
    assert return_request is not None
    assert return_request.claim_reason == "사이즈 불만족"
    assert return_request.inspection_status == InspectionStatus.RECEIVED_AT_WAREHOUSE


async def test_return_request_polling_is_idempotent(product, db_session, monkeypatch):
    _make_order(db_session, product, "700000005", OrderStatus.DELIVERED)

    monkeypatch.setattr(CoupangWingClient, "fetch_cancelled_order_sheets", _async_return([]))
    monkeypatch.setattr(
        CoupangWingClient,
        "fetch_return_requests",
        _async_return([{"orderId": 700000005, "receiptId": "R2", "reasonName": "단순변심"}]),
    )

    await return_service.detect_cancellations_and_returns(db_session, use_mock=True)
    second_result = await return_service.detect_cancellations_and_returns(db_session, use_mock=True)

    assert second_result["new_return_requests"] == []
    assert db_session.query(ReturnRequest).count() == 1


async def test_unknown_market_order_id_is_skipped(db_session, monkeypatch):
    monkeypatch.setattr(
        CoupangWingClient, "fetch_cancelled_order_sheets", _async_return([{"orderId": 999999999}])
    )
    monkeypatch.setattr(CoupangWingClient, "fetch_return_requests", _async_return([]))

    result = await return_service.detect_cancellations_and_returns(db_session, use_mock=True)

    assert result["skipped_unknown_order"] == ["999999999"]


def test_mark_inspection_failed_sets_dispute_investigating(product, db_session):
    order = _make_order(db_session, product, "700000006", OrderStatus.RETURN_REQUESTED)
    return_request = ReturnRequest(
        order_id=order.order_id, claim_reason="불량", inspection_status=InspectionStatus.RECEIVED_AT_WAREHOUSE
    )
    db_session.add(return_request)
    db_session.commit()

    updated = return_service.mark_inspection_failed(db_session, return_request.return_id)

    assert updated.inspection_status == InspectionStatus.DISPUTE_INVESTIGATING


async def test_mark_inspection_passed_moves_to_source_return_requested(product, db_session):
    order = _make_order(db_session, product, "700000007", OrderStatus.RETURN_REQUESTED)
    return_request = ReturnRequest(
        order_id=order.order_id,
        claim_reason="사이즈 불만족",
        inspection_status=InspectionStatus.RECEIVED_AT_WAREHOUSE,
    )
    db_session.add(return_request)
    db_session.commit()

    updated = await return_service.mark_inspection_passed(db_session, return_request.return_id, use_mock=True)

    assert updated.inspection_status == InspectionStatus.SOURCE_RETURN_REQUESTED


def test_mark_source_return_completed_sets_refunded_and_amount(product, db_session):
    order = _make_order(db_session, product, "700000008", OrderStatus.RETURN_REQUESTED)
    return_request = ReturnRequest(
        order_id=order.order_id,
        claim_reason="사이즈 불만족",
        inspection_status=InspectionStatus.SOURCE_RETURN_REQUESTED,
    )
    db_session.add(return_request)
    db_session.commit()

    updated = return_service.mark_source_return_completed(
        db_session, return_request.return_id, source_refund_amount=5000.0
    )

    assert updated.inspection_status == InspectionStatus.SOURCE_RETURN_COMPLETED
    assert float(updated.source_refund_amount) == 5000.0
    db_session.refresh(order)
    assert order.status == OrderStatus.REFUNDED
