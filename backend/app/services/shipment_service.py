"""매입 완료된 주문 1건의 운송장을 수집해 쿠팡에 발송처리하는 흐름 (PRD 6.1)."""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.markets.coupang import CoupangRegistrationError, CoupangWingClient
from app.integrations.messaging.solapi import SolapiMessagingClient
from app.integrations.rpa.factory import get_rpa_client
from app.models.customer_order import CustomerOrder
from app.models.enums import OrderStatus
from app.models.order_fulfillment import OrderFulfillment


class ShipmentConfirmationError(RuntimeError):
    """발송처리(운송장 조회 또는 쿠팡 송장업로드) 중 복구 불가능한 오류."""


class ShipmentNotReadyError(RuntimeError):
    """소싱처가 아직 발송 준비 중이라 운송장이 발급되지 않은 상태 — 나중에 다시 폴링해야 한다."""


async def confirm_shipment_for_order(session: Session, order_id: int, use_mock: bool | None = None) -> OrderFulfillment:
    """ORDER_PURCHASED 상태 주문 1건의 소싱처 운송장을 조회해 쿠팡에 발송처리하고 SHIPPED로 전환한다."""
    order = session.get(CustomerOrder, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} 인 customer_orders 행이 없습니다.")
    if order.status != OrderStatus.ORDER_PURCHASED:
        raise ShipmentConfirmationError(
            f"order_id={order_id} 는 발송처리 대상이 아닙니다 (status={order.status}, ORDER_PURCHASED여야 함)."
        )

    fulfillment = (
        session.query(OrderFulfillment).filter_by(order_id=order.order_id).order_by(OrderFulfillment.fulfillment_id.desc()).first()
    )
    if fulfillment is None or not fulfillment.source_order_id:
        raise ShipmentConfirmationError(f"order_id={order_id} 에 매입 완료 기록(order_fulfillments)이 없습니다.")
    if not order.market_shipment_box_id or not order.market_vendor_item_id:
        raise ShipmentConfirmationError(
            f"order_id={order_id} 에 쿠팡 발송처리에 필요한 shipmentBoxId/vendorItemId가 없습니다 "
            "(주문 감지 시점의 발주서 응답에 없었을 수 있음)."
        )

    # source_base_url은 이 클라이언트의 fetch_tracking_info가 쓰지 않는다(마이페이지 주문내역
    # URL이 고정값이라) — purchase_order와 시그니처를 맞추기 위해 빈 문자열을 넘긴다.
    rpa_client = get_rpa_client(source_base_url="", use_mock=use_mock, source_platform=fulfillment.source_platform)
    tracking = await rpa_client.fetch_tracking_info(fulfillment.source_order_id)
    if tracking is None:
        raise ShipmentNotReadyError(
            f"order_id={order_id} (소싱처 주문번호={fulfillment.source_order_id})의 운송장이 "
            "아직 발급되지 않았습니다 — 나중에 다시 확인해야 합니다."
        )

    fulfillment.courier_code = tracking.courier_code
    fulfillment.tracking_no = tracking.tracking_no
    session.commit()

    settings = get_settings()
    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    response = await client.confirm_shipping(
        vendor_id=settings.coupang_vendor_id,
        order_id=int(order.market_order_id),
        vendor_item_id=int(order.market_vendor_item_id),
        shipment_box_id=int(order.market_shipment_box_id),
        delivery_company_code=tracking.courier_code,
        invoice_number=tracking.tracking_no,
    )
    if response.get("code") != "SUCCESS":
        raise CoupangRegistrationError(f"쿠팡 발송처리(송장업로드) 실패: {response}")

    order.status = OrderStatus.SHIPPED
    session.commit()

    await _notify_customer_of_shipment(order, tracking, settings, use_mock)

    return fulfillment


async def _notify_customer_of_shipment(order: CustomerOrder, tracking, settings, use_mock: bool | None) -> None:
    """PRD 6.2: 고객에게 "출고되었습니다" 카카오 알림톡(또는 문자)을 선제 발송한다.

    "언제 오나요?" 같은 단순 배송 문의 CS를 줄이는 게 목적이지, 발송 자체가 주문
    처리의 필수 조건은 아니다 — 이미 쿠팡 발송처리(order.status=SHIPPED)까지 끝난
    뒤에 하는 부가 기능이므로, 알림 발송이 실패해도 주문 처리 결과를 되돌리지 않는다.
    """
    try:
        product_name = order.product.product_name if order.product else "주문하신 상품"
        messaging_client = SolapiMessagingClient(settings=settings, use_mock=use_mock)
        await messaging_client.send_kakao_alert(
            phone=order.recipient_phone,
            template_id=settings.solapi_shipping_template_id,
            variables={
                "고객명": order.recipient_name,
                "상품명": product_name,
                "택배사": tracking.courier_name,
                "운송장번호": tracking.tracking_no,
            },
        )
        print(f"  [진단] 발송 안내(알림톡/문자) 발송 완료: order_id={order.order_id}", flush=True)
    except Exception as exc:  # noqa: BLE001 - 부가 기능(고객 안내)이 발송처리 자체를 실패로 만들면 안 된다.
        print(f"  발송 안내(알림톡/문자) 발송 실패({exc}) — 주문 상태(SHIPPED)는 그대로 유지합니다.", flush=True)
