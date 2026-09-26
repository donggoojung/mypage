"""신규 주문 1건을 소싱처 최저가 매입처에 무인 발주하는 전체 흐름 (PRD 5.1~5.2)."""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.messaging.telegram_admin import TelegramAdminNotifier
from app.integrations.rpa.base import REVIEW_ONLY_PREFIX, ShippingInfo
from app.integrations.rpa.factory import get_rpa_client
from app.models.customer_order import CustomerOrder
from app.models.enums import OrderStatus
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.services.sourcing_optimizer import find_cheapest_source


class OrderProcessingError(RuntimeError):
    """주문 처리(소싱처 판별 또는 무인 발주) 중 복구 불가능한 오류."""


async def process_new_order(session: Session, order_id: int, use_mock: bool | None = None) -> OrderFulfillment:
    """RECEIVED 상태 주문 1건을 최저가 소싱처에서 무인 매입하고 ORDER_PURCHASED로 전환한다."""
    order = session.get(CustomerOrder, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} 인 customer_orders 행이 없습니다.")
    if order.status != OrderStatus.RECEIVED:
        raise OrderProcessingError(f"order_id={order_id} 는 이미 처리 중이거나 완료된 주문입니다 (status={order.status}).")

    product = session.get(MasterProduct, order.product_id)
    if product is None:
        raise ValueError(f"product_id={order.product_id} 인 master_products 행이 없습니다.")

    order.status = OrderStatus.SOURCING_IN_PROGRESS
    session.commit()

    try:
        quote = await find_cheapest_source(session, order.product_id, order.ordered_size, order.quantity)

        rpa_client = get_rpa_client(
            source_base_url=quote.source_url, use_mock=use_mock, source_platform=quote.source_platform
        )
        shipping_info = ShippingInfo(
            recipient_name=order.recipient_name,
            recipient_phone=order.recipient_phone,
            shipping_addr=order.shipping_addr,
        )
        # RPA_CONFIRM_FINAL_PAYMENT=false(기본값, 반자동 승인)면 실제 결제 직전 단계까지만
        # 진행하고 멈춘다 — 이 경우는 에러가 아니라 정상적인 "결제 승인 대기" 지점이라, 아래
        # REVIEW_ONLY 분기에서 처리하고 여기서 raise하지 않는다. true(완전 무인)로 바꾸면
        # 사람 확인 없이 바로 이 자리에서 실제 결제까지 끝난다.
        confirm_final_payment = get_settings().rpa_confirm_final_payment
        source_order_id = await rpa_client.purchase_order(
            quote.style_code, order.ordered_size, shipping_info, confirm_final_payment=confirm_final_payment
        )
        if source_order_id.startswith(REVIEW_ONLY_PREFIX):
            # 반자동 승인: 배송지 입력까지는 정상적으로 끝났고 결제 버튼만 안 눌렀을 뿐이니
            # HOLD(에러)가 아니라 PENDING_PAYMENT_APPROVAL로 멈춘다. 나중에 관리자가 대시보드에서
            # [결제 승인]을 누르면 approve_and_complete_purchase()가 이어서 실제 결제를 완료한다.
            fulfillment = OrderFulfillment(
                order_id=order.order_id,
                source_platform=quote.source_platform,
                source_order_id=None,
                cost_paid=float(quote.expected_payment),
            )
            session.add(fulfillment)
            order.status = OrderStatus.PENDING_PAYMENT_APPROVAL
            session.commit()
            await _notify_admin_pending_approval(order, product, quote, use_mock)
            return fulfillment
    except Exception as exc:
        # 30분 재고 동기화 주기 사이의 순간 품절/발주 실패 보완: 조용히 실패만 반복하지
        # 않도록 '보류(HOLD)'로 명확히 멈추고, 관리자에게 즉시 텔레그램으로 알린다 —
        # 사람이 확인(대체 소싱처 수동 처리, 고객 취소/환불 등)하기 전에는 자동 재시도하지 않는다.
        order.status = OrderStatus.HOLD
        session.commit()
        await _notify_admin_of_hold(order, product, exc, use_mock)
        raise

    fulfillment = OrderFulfillment(
        order_id=order.order_id,
        source_platform=quote.source_platform,
        source_order_id=source_order_id,
        cost_paid=float(quote.expected_payment),
    )
    session.add(fulfillment)
    order.status = OrderStatus.ORDER_PURCHASED
    session.commit()

    return fulfillment


async def approve_and_complete_purchase(session: Session, order_id: int, use_mock: bool | None = None) -> OrderFulfillment:
    """PENDING_PAYMENT_APPROVAL 상태 주문 1건의 실제 결제를 대표님 승인 후 이어서 완료한다.

    대시보드의 [결제 승인] 버튼이 호출하는 흐름 — 승인 대기 중 가격/재고가 바뀌었을 수 있어
    최신가를 한 번 더 조회한 뒤, RPA에 confirm_final_payment=True로 다시 진행시켜 실제
    결제 버튼까지 클릭한다.
    """
    order = session.get(CustomerOrder, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} 인 customer_orders 행이 없습니다.")
    if order.status != OrderStatus.PENDING_PAYMENT_APPROVAL:
        raise OrderProcessingError(
            f"order_id={order_id} 는 결제 승인 대상이 아닙니다 (status={order.status}, "
            "PENDING_PAYMENT_APPROVAL이어야 함)."
        )

    product = session.get(MasterProduct, order.product_id)
    pending_fulfillment = (
        session.query(OrderFulfillment)
        .filter_by(order_id=order.order_id, source_order_id=None)
        .order_by(OrderFulfillment.fulfillment_id.desc())
        .first()
    )
    if pending_fulfillment is None:
        raise OrderProcessingError(f"order_id={order_id} 에 승인 대기 중인 매입 기록(order_fulfillments)이 없습니다.")

    try:
        quote = await find_cheapest_source(session, order.product_id, order.ordered_size, order.quantity)

        rpa_client = get_rpa_client(
            source_base_url=quote.source_url, use_mock=use_mock, source_platform=quote.source_platform
        )
        shipping_info = ShippingInfo(
            recipient_name=order.recipient_name,
            recipient_phone=order.recipient_phone,
            shipping_addr=order.shipping_addr,
        )
        source_order_id = await rpa_client.purchase_order(
            quote.style_code, order.ordered_size, shipping_info, confirm_final_payment=True
        )
        if source_order_id.startswith(REVIEW_ONLY_PREFIX):
            # confirm_final_payment=True로 보냈는데도 못 끝냈다면(결제 버튼을 못 찾는 등)
            # 승인 대기가 아니라 진짜 문제 상황이므로 에러로 다뤄야 한다.
            raise OrderProcessingError(f"결제 승인 처리 중 RPA가 실제 결제를 완료하지 못했습니다({source_order_id}).")
    except Exception as exc:
        order.status = OrderStatus.HOLD
        session.commit()
        await _notify_admin_of_hold(order, product, exc, use_mock)
        raise

    pending_fulfillment.source_platform = quote.source_platform
    pending_fulfillment.source_order_id = source_order_id
    pending_fulfillment.cost_paid = float(quote.expected_payment)
    order.status = OrderStatus.ORDER_PURCHASED
    session.commit()

    return pending_fulfillment


async def _notify_admin_pending_approval(
    order: CustomerOrder, product: MasterProduct, quote, use_mock: bool | None
) -> None:
    """부가 기능(관리자 알림)이 실패해도 PENDING_PAYMENT_APPROVAL 전환 자체는 되돌리지 않는다."""
    try:
        notifier = TelegramAdminNotifier(use_mock=use_mock)
        text = (
            "[보탬] 결제 승인 대기중\n"
            f"쿠팡 주문번호: {order.market_order_id}\n"
            f"상품: {product.product_name} / 사이즈 {order.ordered_size} / 수량 {order.quantity}\n"
            f"소싱처: {quote.source_platform.value} / 예상 결제금액: {int(quote.expected_payment):,}원\n"
            "대시보드(localhost:8080)에서 배송지/가격 확인 후 [결제 승인]을 눌러주세요."
        )
        await notifier.send_alert(text)
    except Exception as notify_exc:  # noqa: BLE001
        print(f"  결제 승인 대기 알림 발송 실패({notify_exc}) — 주문 상태는 그대로 유지합니다.", flush=True)


async def _notify_admin_of_hold(
    order: CustomerOrder, product: MasterProduct, exc: Exception, use_mock: bool | None
) -> None:
    """부가 기능(관리자 알림)이 실패해도 주문을 HOLD로 멈춘 결과 자체는 되돌리지 않는다."""
    try:
        notifier = TelegramAdminNotifier(use_mock=use_mock)
        text = (
            "[보탬] 주문 매입 보류(HOLD) 발생\n"
            f"쿠팡 주문번호: {order.market_order_id}\n"
            f"상품: {product.product_name} / 사이즈 {order.ordered_size} / 수량 {order.quantity}\n"
            f"사유: {exc}\n"
            "대시보드에서 확인 후 대체 소싱처 수동 처리 또는 고객 취소/환불을 진행해주세요."
        )
        await notifier.send_alert(text)
    except Exception as notify_exc:  # noqa: BLE001 - 알림 발송 실패가 HOLD 전환 자체를 실패로 만들면 안 된다.
        print(f"  관리자 텔레그램 알림 발송 실패({notify_exc}) — 주문 상태(HOLD)는 그대로 유지합니다.", flush=True)
