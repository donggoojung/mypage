"""토스쇼핑 주문 연동: 주문 가져오기, 송장 보내기(배송중으로), 취소 승인·거절, 반품 승인.

토스쇼핑의 특징 (개발 문서 claim/cancel.md):
- 고객이 **결제완료** 상태에서 취소하면 즉시 환불되고 판매자에게 알림도 클레임도 없다. 그래서 공급사에 발주하기 전에
  [주문 가져오기]를 한 번 더 눌러 취소 여부를 확인해야 한다.
- 상품준비중·발송지연 상태에서 고객이 취소를 요청하면 승인 또는 거절을 해야 한다. 거절은 택배사·송장번호를 넣는 것으로 하고,
  그러면 주문이 배송중이 된다 (= 공급사가 이미 출고한 경우).
- 클레임은 10~60분 간격으로 확인하라고 안내돼 있다 (10분보다 짧으면 이용 제한 가능).

주의 (미검증): 실제 키로 호출해 본 적이 없다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.toss import BaseTossClient, TossClaim, TossError, TossOrder
from app.models.agri import AgriOption, AgriOrder, AgriProduct
from app.services import agri_pricing as pricing
from app.services.agri_smartstore import option_prices
from app.services.agri_smartstore_orders import NeedsConfirmation, OrderActionError

MARKET = "toss"
MOCK_PREFIX = "MOCK-"
ORDERS_VERIFIED = False

NEW_STATUSES = {"PAID", "PREPARING_PRODUCT"}
ENDED_STATUSES = {"CANCELED_PAYMENT", "COMPLETED_RETURN"}
SHIPPED_STATUSES = {"DELIVERING", "DELIVERED", "CONFIRMED_ORDER"}
CLAIM_BY_STATUS = {
    "CLAIM_REQUESTED_CANCEL": ("cancel", "requested"),
    "CLAIM_REJECTED_CANCEL": ("cancel", "rejected"),
    "REQUESTED_RETURN": ("return", "requested"),
    "ONGOING_RETURN": ("return", "requested"),
    "CLAIM_REJECTED_RETURN": ("return", "rejected"),
    "REQUESTED_EXCHANGE": ("exchange", "requested"),
    "ONGOING_EXCHANGE": ("exchange", "requested"),
    "CLAIM_REJECTED_EXCHANGE": ("exchange", "rejected"),
    "COMPLETED_EXCHANGE": ("exchange", "done"),
}
_CLAIM_LABEL = {"cancel": "취소", "return": "반품", "exchange": "교환"}

# 화면에 적는 택배사 이름 → 토스쇼핑이 받는 택배사 이름 (문서의 deliveryCompany 목록)
COURIERS = {
    "CJ대한통운": "CJ대한통운", "대한통운": "CJ대한통운", "CJ택배": "CJ대한통운", "CJ": "CJ대한통운",
    "우체국택배": "우체국택배", "우체국": "우체국택배",
    "한진택배": "한진택배", "한진": "한진택배",
    "롯데택배": "롯데택배", "롯데": "롯데택배",
    "로젠택배": "로젠택배", "로젠": "로젠택배",
    "경동택배": "경동택배", "경동": "경동택배",
    "대신택배": "대신택배", "대신": "대신택배",
    "합동택배": "합동택배", "합동": "합동택배",
    "일양로지스": "일양로지스", "천일택배": "천일택배", "건영택배": "건영택배", "농협택배": "농협택배",
    "CU편의점택배": "CU편의점택배", "GS25편의점택배": "GS25편의점택배", "직접전달": "직접전달",
}


def courier_name(name: str | None) -> str | None:
    if not name:
        return None
    key = name.replace(" ", "").upper()
    for label, value in COURIERS.items():
        if label.upper() == key:
            return value
    return None


def _is_practice(order: AgriOrder) -> bool:
    return order.market_order_no.startswith(MOCK_PREFIX)


def _mode_mismatch(order: AgriOrder, client: BaseTossClient) -> str | None:
    if client.is_mock and not _is_practice(order):
        return "실제 주문입니다. 지금은 연습 모드라서 처리하지 않았습니다."
    if not client.is_mock and _is_practice(order):
        return "연습으로 만든 주문이라 실제 토스쇼핑으로 보내지 않았습니다."
    return None


def find_option(session: Session, to: TossOrder) -> tuple[AgriOption | None, str]:
    """토스쇼핑 주문이 우리 어느 옵션인지 찾는다 (등록할 때 넣은 옵션 관리 코드 agri-옵션번호)."""
    match = re.fullmatch(r"agri-(\d+)", to.item_code or "")
    if match:
        option = session.get(AgriOption, int(match.group(1)))
        if option is not None:
            return option, ""
    match = re.fullmatch(r"agri-(\d+)", to.product_code or "")
    if not match:
        return None, "우리 시스템으로 올린 상품이 아닙니다 (상품 관리 코드가 없습니다)."
    product = session.get(AgriProduct, int(match.group(1)))
    if product is None or not product.options:
        return None, f"상품번호 {match.group(1)}을(를) 찾을 수 없습니다."
    if to.option_name:
        by_label = [o for o in product.options if o.label and o.label in to.option_name]
        if len(by_label) == 1:
            return by_label[0], ""
    if len(product.options) == 1:
        return product.options[0], ""
    return None, f"옵션을 알아볼 수 없습니다 ({to.option_name or '옵션 정보 없음'}). 직접 입력해 주세요."


def _find_order(session: Session, order_product_id: str) -> AgriOrder | None:
    return session.execute(
        select(AgriOrder).where(AgriOrder.market == MARKET, AgriOrder.market_order_no == order_product_id)
    ).unique().scalar_one_or_none()


def _apply_state(order: AgriOrder, to: TossOrder, stats: dict) -> None:
    """이미 있는 주문에 토스쇼핑에서 바뀐 내용(취소·반품 요청, 환불, 발송)을 반영한다."""
    new = CLAIM_BY_STATUS.get(to.status)
    if to.status == "CANCELED_PAYMENT":
        if order.status != "cancelled":
            order.status = "cancelled"
            stats["cancelled"] += 1
        new = ("cancel", "done")
    elif to.status == "COMPLETED_RETURN":
        if order.status != "cancelled":
            order.status = "cancelled"
            stats["cancelled"] += 1
        new = ("return", "done")
    elif to.status in SHIPPED_STATUSES and order.status in ("new", "ordered", "invoiced"):
        order.status = "shipped"
        stats["marked_shipped"] += 1
        # 승인·거절 처리에서 이미 처리된 취소 요청이면 표시를 바꾸지 않는다
    if new and (new != (order.claim_type, order.claim_status)):
        order.claim_type, order.claim_status = new
        stats["claims_updated"] += 1


async def sync_orders(session: Session, client: BaseTossClient, settings: dict, hours: int = 24) -> dict:
    """새 주문과 취소·반품·교환 요청을 가져온다 (읽기만 한다)."""
    today = datetime.now(timezone(timedelta(hours=9))).date()
    start = max(today - timedelta(days=max(hours // 24, 1)), today - timedelta(days=30))
    stats: dict = {"created": 0, "claims_updated": 0, "cancelled": 0, "marked_shipped": 0, "ignored": 0, "skipped": [], "warnings": []}
    fee_rate = str(settings["fee_rates"].get(MARKET, 0))
    orders = await client.fetch_orders(start, today)
    for to in orders:
        existing = _find_order(session, to.order_product_id)
        if existing is not None:
            _apply_state(existing, to, stats)
            continue
        if to.status not in NEW_STATUSES:
            stats["ignored"] += 1
            continue
        option, reason = find_option(session, to)
        if option is None:
            stats["skipped"].append({"product_order_id": to.order_product_id, "reason": reason})
            continue
        if not to.receiver_name or not to.address:
            stats["skipped"].append({"product_order_id": to.order_product_id, "reason": "받는 분 이름이나 주소가 비어 있습니다."})
            continue
        session.add(
            AgriOrder(
                market=MARKET,
                market_order_no=to.order_product_id,
                option_id=option.option_id,
                quantity=to.quantity,
                buyer_name=to.buyer_name,
                receiver_name=to.receiver_name,
                receiver_phone=to.receiver_phone or "-",
                zipcode=to.zipcode,
                address=to.address,
                delivery_memo=to.memo,
                sale_amount=to.price,
                unit_cost=pricing.unit_cost(option.supply_price, option.shipping_fee),
                fee_rate_percent=fee_rate,
                memo=f"토스쇼핑 주문번호 {to.order_id}" if to.order_id else None,
            )
        )
        stats["created"] += 1
    session.commit()
    stats["mode"] = "mock" if client.is_mock else "real"
    stats["checked"] = len(orders)
    if stats["created"]:
        stats["warnings"].append("토스쇼핑은 고객이 '결제완료' 상태에서 취소하면 알림 없이 바로 환불됩니다. 공급사에 발주하기 전에 [주문 가져오기]를 한 번 더 눌러 취소를 확인하세요.")
    return stats


async def dispatch_orders(session: Session, client: BaseTossClient, order_ids: list[int]) -> dict:
    """송장을 받은 토스쇼핑 주문의 택배사·송장번호를 입력한다 (주문이 배송중이 된다)."""
    orders = (
        session.execute(select(AgriOrder).where(AgriOrder.order_id.in_(order_ids), AgriOrder.market == MARKET))
        .unique()
        .scalars()
        .all()
    )
    result: dict = {"sent": [], "failed": [], "skipped": [], "warnings": []}

    def row(order: AgriOrder, **extra) -> dict:
        return {"order_id": order.order_id, "market_order_no": order.market_order_no, **extra}

    for order in sorted(orders, key=lambda o: o.order_id):
        company = courier_name(order.courier)
        reason = None
        if order.status != "invoiced" or not order.tracking_no:
            reason = "송장을 받은 주문(송장 받음)이 아닙니다."
        elif order.claim_status == "requested":
            reason = f"고객의 {_CLAIM_LABEL.get(order.claim_type, '클레임')} 요청이 있어 보내지 않았습니다. 먼저 처리하세요."
        elif (mismatch := _mode_mismatch(order, client)) is not None:
            reason = mismatch
        elif company is None:
            reason = f"택배사 '{order.courier}'를 토스쇼핑 택배사 이름으로 바꾸지 못했습니다. CJ대한통운 같은 이름으로 고쳐 주세요."
        if reason:
            result["skipped"].append(row(order, reason=reason))
            continue
        try:
            outcome = await client.dispatch(order.market_order_no, company, order.tracking_no)
        except TossError as exc:
            result["failed"].append(row(order, reason=str(exc)))  # 같은 내용을 자동으로 다시 보내지 않는다
            continue
        if outcome.ok:
            order.status = "shipped"
            result["sent"].append(row(order))
        else:
            result["failed"].append(row(order, reason=outcome.message))
    session.commit()
    return result


async def _open_claim(client: BaseTossClient, order: AgriOrder, claim_type: str) -> TossClaim:
    for claim in await client.fetch_claims(claim_type, "REQUESTED"):
        if claim.order_product_id == order.market_order_no:
            return claim
    raise OrderActionError("토스쇼핑에서 이 주문의 요청 중인 클레임을 찾지 못했습니다. 이미 철회되었거나 처리되었을 수 있습니다. [주문 가져오기]를 눌러 보세요.")


def _check_state(order: AgriOrder, client: BaseTossClient, claim_type: str) -> None:
    if order.market != MARKET:
        raise OrderActionError("토스쇼핑 주문이 아닙니다.")
    if order.claim_status != "requested" or order.claim_type != claim_type:
        raise OrderActionError(f"처리할 {_CLAIM_LABEL[claim_type]} 요청이 없습니다. 먼저 [토스쇼핑 주문 가져오기]를 눌러 보세요.")
    mismatch = _mode_mismatch(order, client)
    if mismatch:
        raise OrderActionError(mismatch)


async def approve_cancel(session: Session, client: BaseTossClient, order: AgriOrder, confirmed: bool) -> AgriOrder:
    """고객의 취소 요청을 승인한다 (환불되고 되돌릴 수 없다)."""
    _check_state(order, client, "cancel")
    if order.status in ("ordered", "invoiced") and not confirmed:
        raise NeedsConfirmation(
            "이미 공급사에 발주한 주문입니다.\n공급사에 연락해서 출고를 막았는지 먼저 확인하세요.\n출고됐다면 취소를 승인하지 말고 송장을 넣어 거절해야 합니다."
        )
    claim = await _open_claim(client, order, "CANCEL")
    outcome = await client.approve_cancel(claim.claim_id)
    if not outcome.ok:
        raise TossError(outcome.message or "취소 승인이 거절되었습니다.")
    order.claim_status, order.status = "done", "cancelled"
    session.commit()
    session.refresh(order)
    return order


async def reject_cancel(session: Session, client: BaseTossClient, order: AgriOrder, courier: str, tracking_no: str) -> AgriOrder:
    """취소 요청을 거절한다. 공급사가 이미 출고했을 때만 쓴다 (택배사·송장번호를 넣으면 주문이 배송중이 된다)."""
    _check_state(order, client, "cancel")
    company = courier_name(courier)
    if company is None:
        raise OrderActionError(f"택배사 '{courier}'를 토스쇼핑 택배사 이름으로 바꾸지 못했습니다. CJ대한통운 같은 이름으로 넣어 주세요.")
    if not tracking_no:
        raise OrderActionError("송장번호가 필요합니다.")
    claim = await _open_claim(client, order, "CANCEL")
    outcome = await client.reject_cancel(claim.claim_id, company, tracking_no)
    if not outcome.ok:
        raise TossError(outcome.message or "취소 거절이 거절되었습니다.")
    order.claim_status = "rejected"
    order.status = "shipped"  # 거절하면 토스쇼핑에서 주문이 배송중이 되고 송장이 등록된다
    order.courier, order.tracking_no = courier, tracking_no
    session.commit()
    session.refresh(order)
    return order


async def approve_return(session: Session, client: BaseTossClient, order: AgriOrder, confirmed: bool) -> AgriOrder:
    """고객의 반품 요청을 승인한다 (되돌릴 수 없다). 수거·환불은 파트너스에서 이어서 처리한다."""
    _check_state(order, client, "return")
    if not confirmed:
        raise NeedsConfirmation("반품 승인은 되돌릴 수 없습니다. 상품 상태와 환불 금액을 확인했는지 묻는 확인이 필요합니다.")
    claim = await _open_claim(client, order, "RETURN")
    outcome = await client.approve_return(claim.claim_id)
    if not outcome.ok:
        raise TossError(outcome.message or "반품 승인이 거절되었습니다.")
    session.commit()
    session.refresh(order)
    return order


def create_practice_order(session: Session, client: BaseTossClient, settings: dict, option_id: int, quantity: int) -> TossOrder:
    """연습 모드에서 '고객이 주문한 것처럼' 가짜 주문을 만든다."""
    if not client.is_mock:
        raise OrderActionError("연습 모드에서만 만들 수 있습니다. 실제 모드에서는 고객의 진짜 주문만 들어옵니다.")
    option = session.get(AgriOption, option_id)
    if option is None:
        raise OrderActionError("상품 옵션을 골라 주세요.")
    product = option.product
    price = next((r["price"] for r in option_prices(product, settings, MARKET) if r["option"].option_id == option_id), None)
    if price is None:
        cost = pricing.unit_cost(option.supply_price, option.shipping_fee)
        min_profit = int(settings["min_profit"].get(product.category, settings["min_profit"].get("etc", 0)))
        price = pricing.recommended_price(cost, min_profit, float(settings["fee_rates"].get(MARKET, 0)), settings.get("price_unit", 100))
    return client.add_order(  # type: ignore[attr-defined]
        product_code=f"agri-{product.product_id}",
        item_code=f"agri-{option.option_id}",
        product_name=product.name,
        option_name=option.label,
        quantity=quantity,
        price=price * quantity,
        buyer_name="연습 고객",
        receiver_name="연습 고객",
        receiver_phone="010-0000-0000",
        zipcode="12345",
        address="서울시 연습구 연습로 1",
        memo="연습용 주문입니다",
    )
