"""쿠팡 취소/반품 감지 및 역물류 처리 서비스 (PRD 7장 반품/교환 자동화 관리 체계).

핵심 위험 시나리오(대표님이 직접 지적한 부분) 대응:
  1. 고객이 소싱처(ABC마트) 매입 완료 "이후"에 취소하면 배송비/매입비 손실 위험이
     있다 — 매입 "이전"(RECEIVED/SOURCING_IN_PROGRESS/HOLD) 취소와 구분해서 처리한다.
  2. 반품 접수 시 반품지 주소가 이미 판매자 거점(coupang_return_* 설정)으로 등록되어
     있으므로 쿠팡 자동수거가 소싱처가 아닌 우리 거점으로 오게 된다 — 물건이 공중에
     뜨는 문제를 방지한다.

주의 — ABC마트 쪽 "반품 접수" 자동화(RPA)는 아직 구현하지 않았다: 구매 RPA(purchase_bot)만
있고 반품 화면 구조는 실사이트로 확인된 바 없어, 검수 통과 시점에 관리자에게 정확한 절차를
텔레그램으로 안내하는 방식으로 "정립된 절차"를 시스템으로 강제한다. 이후 ABC마트 반품 화면
구조가 확인되면 RPA 자동화로 확장할 수 있다.
"""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.markets.coupang import CoupangWingClient
from app.integrations.messaging.telegram_admin import TelegramAdminNotifier
from app.models.customer_order import CustomerOrder
from app.models.enums import InspectionStatus, OrderStatus
from app.models.return_request import ReturnRequest

# 이미 소싱처(ABC마트)에 매입 비용이 나간 것으로 간주하는 주문 상태 — 이 상태에서 취소가
# 감지되면 단순취소(CANCELLED)가 아니라 관리자 확인이 필요한 CANCEL_REQUESTED로 멈춘다.
_ALREADY_PURCHASED_STATUSES = {OrderStatus.ORDER_PURCHASED, OrderStatus.SHIPPED}
# 이미 최종 처리(중복 폴링 무시 대상)된 주문 상태.
_ALREADY_HANDLED_CANCEL_STATUSES = {OrderStatus.CANCELLED, OrderStatus.REFUNDED, OrderStatus.CANCEL_REQUESTED}


async def detect_cancellations_and_returns(session: Session, use_mock: bool | None = None) -> dict:
    """PRD 7.1: 쿠팡 취소/반품을 폴링해 즉시 시스템 상태에 반영한다.

    5분 주기 Celery beat로 호출된다 (order_tasks.detect_new_orders와 동일 주기 —
    대표님이 "가장 치명적"이라 지적한 부분이라 신규 주문 감지와 같은 긴급도로 다룬다).
    """
    settings = get_settings()
    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    notifier = TelegramAdminNotifier(settings=settings, use_mock=use_mock)

    result: dict = {
        "cancelled_before_purchase": [],
        "cancelled_after_purchase_urgent": [],
        "new_return_requests": [],
        "skipped_unknown_order": [],
    }

    cancelled_sheets = await client.fetch_cancelled_order_sheets(settings.coupang_vendor_id)
    for sheet in cancelled_sheets:
        market_order_id = str(sheet["orderId"])
        order = session.query(CustomerOrder).filter_by(market_order_id=market_order_id).first()
        if order is None:
            result["skipped_unknown_order"].append(market_order_id)
            continue
        if order.status in _ALREADY_HANDLED_CANCEL_STATUSES:
            continue  # 이전 폴링에서 이미 처리됨.

        if order.status in _ALREADY_PURCHASED_STATUSES:
            order.status = OrderStatus.CANCEL_REQUESTED
            session.commit()
            result["cancelled_after_purchase_urgent"].append(market_order_id)
            await _notify_urgent_cancel_after_purchase(notifier, order)
        else:
            # 소싱처 발주 전(RECEIVED/SOURCING_IN_PROGRESS/HOLD)이라 비용 손실 없이 안전하게 취소.
            order.status = OrderStatus.CANCELLED
            session.commit()
            result["cancelled_before_purchase"].append(market_order_id)

    return_sheets = await client.fetch_return_requests(settings.coupang_vendor_id)
    for sheet in return_sheets:
        market_claim_id = str(sheet.get("receiptId") or sheet.get("returnId") or "") or None
        market_order_id = str(sheet.get("orderId", ""))
        order = session.query(CustomerOrder).filter_by(market_order_id=market_order_id).first()
        if order is None:
            result["skipped_unknown_order"].append(market_order_id)
            continue

        already_tracked = (
            session.query(ReturnRequest).filter_by(market_claim_id=market_claim_id).first()
            if market_claim_id
            else None
        )
        if already_tracked is not None:
            continue  # 이전 폴링에서 이미 접수 처리됨.

        return_request = ReturnRequest(
            order_id=order.order_id,
            market_claim_id=market_claim_id,
            claim_reason=sheet.get("reasonName") or sheet.get("reason") or "사유 미상",
            inspection_status=InspectionStatus.RECEIVED_AT_WAREHOUSE,
        )
        session.add(return_request)
        order.status = OrderStatus.RETURN_REQUESTED
        session.commit()

        result["new_return_requests"].append(market_order_id)
        await _notify_return_requested(notifier, order, return_request)

    return result


async def _notify_urgent_cancel_after_purchase(notifier: TelegramAdminNotifier, order: CustomerOrder) -> None:
    try:
        text = (
            "🚨[보탬] 긴급: 매입 완료 후 주문취소 감지\n"
            f"쿠팡 주문번호: {order.market_order_id}\n"
            f"사이즈: {order.ordered_size} / 수량: {order.quantity}\n"
            "이미 ABC마트(소싱처)에 매입이 완료된 뒤 고객이 취소했습니다.\n"
            "→ ABC마트 마이페이지에서 해당 주문을 직접 확인해 출고 전이면 취소, "
            "이미 출고됐으면 반품 접수를 진행해주세요 (배송비 손실 방지)."
        )
        await notifier.send_alert(text)
    except Exception as exc:  # noqa: BLE001 - 알림 실패가 상태 전환 자체를 실패로 만들면 안 된다.
        print(f"  긴급 취소 알림 발송 실패({exc}) — 주문 상태(CANCEL_REQUESTED)는 그대로 유지합니다.", flush=True)


async def _notify_return_requested(
    notifier: TelegramAdminNotifier, order: CustomerOrder, return_request: ReturnRequest
) -> None:
    settings = get_settings()
    try:
        text = (
            "[보탬] 반품 접수 감지\n"
            f"쿠팡 주문번호: {order.market_order_id}\n"
            f"사유: {return_request.claim_reason}\n"
            "반품지(우리 거점)로 입고 예정입니다 — 물건 도착 후 개봉해 훼손/시착흔적/텍(Tag)을 "
            "확인하고, 대시보드에서 '검수 통과' 또는 '검수 불합격'을 눌러주세요.\n"
            f"반품지 주소: {settings.coupang_return_address} {settings.coupang_return_address_detail}"
        )
        await notifier.send_alert(text)
    except Exception as exc:  # noqa: BLE001
        print(f"  반품 접수 알림 발송 실패({exc}) — 반품 상태는 그대로 유지합니다.", flush=True)


async def mark_inspection_passed(session: Session, return_id: int, use_mock: bool | None = None) -> ReturnRequest:
    """PRD 7.1 2~3단계: 거점 창고 검수 통과 즉시 소싱처 반품 접수 절차를 관리자에게 안내한다.

    ABC마트 반품 접수 자체는 아직 RPA로 자동화되어 있지 않다(파일 상단 설명 참고) —
    검수 통과 시 정확한 다음 행동을 텔레그램으로 강제 안내해 절차 누락을 막는다.
    """
    return_request = session.get(ReturnRequest, return_id)
    if return_request is None:
        raise ValueError(f"return_id={return_id} 인 return_requests 행이 없습니다.")

    return_request.inspection_status = InspectionStatus.SOURCE_RETURN_REQUESTED
    session.commit()

    settings = get_settings()
    notifier = TelegramAdminNotifier(settings=settings, use_mock=use_mock)
    order = session.get(CustomerOrder, return_request.order_id)
    try:
        text = (
            "[보탬] 반품 검수 통과 — 소싱처(ABC마트) 반품 접수 필요\n"
            f"쿠팡 주문번호: {order.market_order_id if order else return_request.order_id}\n"
            "→ ABC마트 마이페이지 주문내역에서 해당 건 반품 접수를 진행해주세요.\n"
            "접수 및 환불 확인 후 대시보드에서 '소싱처 반품 완료'로 처리해주세요."
        )
        await notifier.send_alert(text)
    except Exception as exc:  # noqa: BLE001
        print(f"  검수통과 알림 발송 실패({exc}) — 검수 상태는 그대로 유지합니다.", flush=True)

    return return_request


def mark_inspection_failed(session: Session, return_id: int) -> ReturnRequest:
    """PRD 7.3: 훼손/시착흔적 등 검수 불합격 — 분쟁 조사 상태로 전환한다."""
    return_request = session.get(ReturnRequest, return_id)
    if return_request is None:
        raise ValueError(f"return_id={return_id} 인 return_requests 행이 없습니다.")
    return_request.inspection_status = InspectionStatus.DISPUTE_INVESTIGATING
    session.commit()
    return return_request


def mark_source_return_completed(
    session: Session, return_id: int, source_refund_amount: float | None = None
) -> ReturnRequest:
    """관리자가 ABC마트 반품 접수/환불 확인을 마친 뒤 수동으로 완료 처리한다 (PRD 7.2 비용 정산)."""
    return_request = session.get(ReturnRequest, return_id)
    if return_request is None:
        raise ValueError(f"return_id={return_id} 인 return_requests 행이 없습니다.")
    return_request.inspection_status = InspectionStatus.SOURCE_RETURN_COMPLETED
    if source_refund_amount is not None:
        return_request.source_refund_amount = source_refund_amount

    order = session.get(CustomerOrder, return_request.order_id)
    if order is not None:
        order.status = OrderStatus.REFUNDED
    session.commit()
    return return_request
