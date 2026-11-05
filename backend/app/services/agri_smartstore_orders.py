"""스마트스토어 주문 연동: 새 주문 가져오기, 송장 보내기(발송 처리), 취소·반품 승인.

흐름: 주문 가져오기 → (공급사 발주·송장 받기는 기존 화면) → 송장 보내기 → 마켓 발송 처리 끝.
취소·반품은 고객이 요청하면 주문 목록에 표시하고, 대표님이 확인한 뒤에만 승인한다.

주의 (미검증): 네이버 쪽 항목 이름과 값은 공식 문서를 직접 보지 못하고 오픈소스 예시를 보고 작성했다.
택배사 코드, 취소·반품 요청 상태 값, 발송 전에 발주 확인이 필요한지는 실제 키로 확인해야 한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.naver_commerce import (
    BaseSmartstoreClient,
    DispatchItem,
    MarketOrder,
    NaverCommerceError,
)
from app.models.agri import AgriOption, AgriOrder, AgriProduct
from app.services import agri_pricing as pricing
from app.services import agri_smartstore as smartstore

MARKET = smartstore.MARKET
ORDERS_VERIFIED = False
MOCK_PREFIX = "MOCK-"

# 새 주문으로 받아들이는 네이버 상태 (결제 완료)
NEW_STATUSES = {"PAYED"}
# 이 상태가 되면 우리 쪽도 취소로 본다
ENDED_STATUSES = {"CANCELED", "RETURNED", "CANCELED_BY_NOPAYMENT"}
# 이 상태면 이미 발송된 것
SHIPPED_STATUSES = {"DELIVERING", "DELIVERED", "PURCHASE_DECIDED"}

# 화면에 적는 택배사 이름 → 네이버 택배사 코드 (미검증)
COURIER_CODES = {
    "CJ대한통운": "CJGLS",
    "대한통운": "CJGLS",
    "CJ택배": "CJGLS",
    "CJ": "CJGLS",
    "롯데택배": "LOTTE",
    "롯데": "LOTTE",
    "한진택배": "HANJIN",
    "한진": "HANJIN",
    "우체국택배": "EPOST",
    "우체국": "EPOST",
    "로젠택배": "LOGEN",
    "로젠": "LOGEN",
    "경동택배": "KDEXP",
    "경동": "KDEXP",
    "합동택배": "HDEXP",
    "대신택배": "DAESIN",
    "일양로지스": "ILYANG",
}


class OrderActionError(Exception):
    """사용자가 고칠 수 있는 이유로 처리할 수 없을 때 (화면에 그대로 보여준다)."""


class NeedsConfirmation(OrderActionError):
    """대표님이 '확인했다'고 해야 진행할 수 있을 때."""


def courier_code(name: str | None) -> str | None:
    if not name:
        return None
    key = name.replace(" ", "").upper()
    for label, code in COURIER_CODES.items():
        if label.upper() == key:
            return code
    # 네이버 코드를 그대로 적은 경우 (CJGLS 등)
    return name.strip().upper() if name.strip().upper() in COURIER_CODES.values() else None


def _is_mock_id(market_order_no: str) -> bool:
    return market_order_no.startswith(MOCK_PREFIX)


def _mode_mismatch(order: AgriOrder, client: BaseSmartstoreClient) -> str | None:
    """연습 주문을 실제 스토어에 보내거나, 실제 주문을 연습으로 처리하는 일을 막는다."""
    if client.is_mock and not _is_mock_id(order.market_order_no):
        return "실제 주문입니다. 지금은 연습 모드라서 처리하지 않았습니다."
    if not client.is_mock and _is_mock_id(order.market_order_no):
        return "연습으로 만든 주문이라 실제 스토어로 보내지 않았습니다."
    return None


def _claim_of(mo: MarketOrder) -> tuple[str | None, str | None]:
    """네이버의 클레임 종류·상태를 우리 표시(cancel/return/exchange, requested/done/rejected)로."""
    kind_raw = (mo.claim_type or "").upper()
    if "RETURN" in kind_raw:
        kind = "return"
    elif "EXCHANGE" in kind_raw:
        kind = "exchange"
    elif "CANCEL" in kind_raw:
        kind = "cancel"
    else:
        return None, None
    status_raw = (mo.claim_status or "").upper()
    if "DONE" in status_raw or status_raw.endswith("COMPLETE") or status_raw.endswith("COMPLETED"):
        return kind, "done"
    if "REJECT" in status_raw:
        return kind, "rejected"
    return kind, "requested"


def find_option(session: Session, mo: MarketOrder) -> tuple[AgriOption | None, str]:
    """네이버 주문이 우리 어느 상품·옵션인지 찾는다. 못 찾으면 이유를 돌려준다."""
    match = re.fullmatch(r"agri-(\d+)", mo.product_code or "")
    if not match:
        return None, "우리 시스템으로 올린 상품이 아닙니다 (판매자 상품코드가 없습니다)."
    product = session.get(AgriProduct, int(match.group(1)))
    if product is None:
        return None, f"상품번호 {match.group(1)}이(가) 지워졌습니다."
    options = list(product.options)
    if not options:
        return None, "옵션이 없는 상품입니다."
    if mo.option_code:
        by_code = [o for o in options if o.supplier_code and o.supplier_code == mo.option_code]
        if len(by_code) == 1:
            return by_code[0], ""
    if mo.option_text:
        by_label = [o for o in options if o.label and o.label in mo.option_text]
        if len(by_label) == 1:
            return by_label[0], ""
    if len(options) == 1:
        return options[0], ""
    return None, f"옵션을 알아볼 수 없습니다 ({mo.option_text or mo.option_code or '옵션 정보 없음'}). 직접 입력해 주세요."


def _apply_market_state(order: AgriOrder, mo: MarketOrder, stats: dict) -> None:
    """이미 있는 주문에 마켓에서 바뀐 내용(취소·반품 요청, 완료, 발송)을 반영한다."""
    kind, status = _claim_of(mo)
    if mo.status in ENDED_STATUSES:
        if order.status != "cancelled":
            order.status = "cancelled"
            stats["cancelled"] += 1
        kind = kind or "cancel"
        status = "done"
    elif mo.status in SHIPPED_STATUSES and order.status in ("new", "ordered", "invoiced"):
        order.status = "shipped"
        stats["marked_shipped"] += 1
    if (kind, status) != (order.claim_type, order.claim_status):
        order.claim_type, order.claim_status = kind, status
        stats["claims_updated"] += 1


async def sync_orders(session: Session, client: BaseSmartstoreClient, settings: dict, hours: int = 24) -> dict:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    market_orders = await client.fetch_orders(since, until)

    stats = {"created": 0, "claims_updated": 0, "cancelled": 0, "marked_shipped": 0, "ignored": 0, "skipped": []}
    fee_rate = str(settings["fee_rates"].get(MARKET, 0))
    for mo in market_orders:
        if not mo.product_order_id:
            continue
        existing = session.execute(
            select(AgriOrder).where(AgriOrder.market == MARKET, AgriOrder.market_order_no == mo.product_order_id)
        ).unique().scalar_one_or_none()
        if existing is not None:
            _apply_market_state(existing, mo, stats)
            continue
        if mo.status not in NEW_STATUSES:
            stats["ignored"] += 1  # 결제 대기, 이미 끝난 주문 등은 가져오지 않는다
            continue
        kind, claim_status = _claim_of(mo)
        option, reason = find_option(session, mo)
        if option is None:
            stats["skipped"].append({"product_order_id": mo.product_order_id, "reason": reason})
            continue
        if not mo.receiver_name or not mo.address:
            stats["skipped"].append(
                {"product_order_id": mo.product_order_id, "reason": "받는 분 이름이나 주소가 비어 있습니다."}
            )
            continue
        session.add(
            AgriOrder(
                market=MARKET,
                market_order_no=mo.product_order_id,
                option_id=option.option_id,
                quantity=max(mo.quantity, 1),
                buyer_name=mo.buyer_name,
                receiver_name=mo.receiver_name,
                receiver_phone=mo.receiver_phone or "-",
                zipcode=mo.zipcode,
                address=mo.address,
                delivery_memo=mo.memo,
                sale_amount=mo.paid_amount,
                unit_cost=pricing.unit_cost(option.supply_price, option.shipping_fee),
                fee_rate_percent=fee_rate,
                memo=f"스마트스토어 주문번호 {mo.order_id}" if mo.order_id else None,
                claim_type=kind,
                claim_status=claim_status,
            )
        )
        stats["created"] += 1
    session.commit()
    stats["mode"] = "mock" if client.is_mock else "real"
    stats["checked"] = len(market_orders)
    return stats


async def dispatch_orders(session: Session, client: BaseSmartstoreClient, order_ids: list[int]) -> dict:
    """송장을 받은 스마트스토어 주문의 택배사·송장번호를 네이버에 입력한다."""
    orders = (
        session.execute(select(AgriOrder).where(AgriOrder.order_id.in_(order_ids), AgriOrder.market == MARKET))
        .unique()
        .scalars()
        .all()
    )
    result: dict = {"sent": [], "failed": [], "skipped": [], "warnings": []}

    def skip(order: AgriOrder, reason: str) -> None:
        result["skipped"].append({"order_id": order.order_id, "market_order_no": order.market_order_no, "reason": reason})

    items: list[DispatchItem] = []
    by_id: dict[str, AgriOrder] = {}
    for order in sorted(orders, key=lambda o: o.order_id):
        if order.status != "invoiced" or not order.tracking_no:
            skip(order, "송장을 받은 주문(송장 받음)이 아닙니다.")
        elif order.claim_status == "requested":
            skip(order, f"고객의 {_CLAIM_LABEL.get(order.claim_type, '클레임')} 요청이 있어 보내지 않았습니다. 먼저 처리하세요.")
        elif (mismatch := _mode_mismatch(order, client)) is not None:
            skip(order, mismatch)
        elif (code := courier_code(order.courier)) is None:
            skip(order, f"택배사 '{order.courier}'의 네이버 코드를 모릅니다. 택배사 이름을 CJ대한통운 등으로 고쳐 주세요.")
        else:
            items.append(DispatchItem(order.market_order_no, code, order.tracking_no))
            by_id[order.market_order_no] = order
    if not items:
        return result

    # 발주 확인이 먼저 필요한 경우가 있어서 먼저 보낸다. 이미 확인된 주문이라 실패해도 발송은 시도한다.
    try:
        for outcome in await client.confirm_orders([i.product_order_id for i in items]):
            if not outcome.ok:
                result["warnings"].append(f"{outcome.product_order_id} {outcome.message}")
    except NaverCommerceError as exc:
        result["warnings"].append(str(exc))

    for outcome in await client.dispatch_orders(items):
        order = by_id[outcome.product_order_id]
        row = {"order_id": order.order_id, "market_order_no": order.market_order_no}
        if outcome.ok:
            order.status = "shipped"
            result["sent"].append(row)
        else:
            result["failed"].append({**row, "reason": outcome.message})
    session.commit()
    return result


_CLAIM_LABEL = {"cancel": "취소", "return": "반품", "exchange": "교환"}


async def approve_claim(session: Session, client: BaseSmartstoreClient, order: AgriOrder, confirmed: bool) -> AgriOrder:
    """고객의 취소 또는 반품 요청을 승인한다. 승인하면 되돌릴 수 없다."""
    if order.market != MARKET:
        raise OrderActionError("스마트스토어 주문이 아닙니다.")
    if order.claim_status != "requested" or order.claim_type not in ("cancel", "return"):
        raise OrderActionError("승인할 취소·반품 요청이 없습니다. 먼저 [스마트스토어 주문 가져오기]를 눌러 보세요.")
    mismatch = _mode_mismatch(order, client)
    if mismatch:
        raise OrderActionError(mismatch)
    if order.claim_type == "cancel" and order.status in ("ordered", "invoiced") and not confirmed:
        raise NeedsConfirmation(
            "이미 공급사에 발주한 주문입니다.\n공급사에 연락해서 출고를 막았는지 먼저 확인하세요.\n출고됐다면 취소를 승인하지 말고 반품으로 받아야 합니다."
        )
    if order.claim_type == "return" and not confirmed:
        raise NeedsConfirmation("반품 승인은 되돌릴 수 없습니다. 상품 상태와 환불 금액을 확인했는지 묻는 확인이 필요합니다.")

    if order.claim_type == "cancel":
        outcome = await client.approve_cancel(order.market_order_no)
    else:
        outcome = await client.approve_return(order.market_order_no)
    if not outcome.ok:
        raise NaverCommerceError(outcome.message or "승인이 거절되었습니다.")
    order.claim_status = "done"
    order.status = "cancelled"
    session.commit()
    session.refresh(order)
    return order


def create_practice_order(session: Session, client: BaseSmartstoreClient, settings: dict, option_id: int, quantity: int) -> MarketOrder:
    """연습 모드에서 '고객이 주문한 것처럼' 가짜 주문을 만든다 (주문 가져오기를 연습하는 용도)."""
    if not client.is_mock:
        raise OrderActionError("연습 모드에서만 만들 수 있습니다. 실제 모드에서는 고객의 진짜 주문만 들어옵니다.")
    option = session.get(AgriOption, option_id)
    if option is None:
        raise OrderActionError("상품 옵션을 골라 주세요.")
    product = option.product
    price = next((r["price"] for r in smartstore.option_prices(product, settings) if r["option"].option_id == option_id), None)
    if price is None:
        cost = pricing.unit_cost(option.supply_price, option.shipping_fee)
        min_profit = int(settings["min_profit"].get(product.category, settings["min_profit"].get("etc", 0)))
        price = pricing.recommended_price(
            cost, min_profit, float(settings["fee_rates"].get(MARKET, 0)), settings.get("price_unit", 100)
        )
    return client.add_order(  # type: ignore[attr-defined]
        product_code=f"agri-{product.product_id}",
        option_code=option.supplier_code,
        option_text=option.label,
        product_name=product.name,
        quantity=quantity,
        paid_amount=price * quantity,
        buyer_name="연습 고객",
        receiver_name="연습 고객",
        receiver_phone="010-0000-0000",
        zipcode="12345",
        address="서울시 연습구 연습로 1",
        memo="연습용 주문입니다",
        status="PAYED",
    )
