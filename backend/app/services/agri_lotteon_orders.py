"""롯데온 주문 연동: 새 주문 가져오기, 송장 보내기(발송 처리), 고객의 취소 요청 승인·거부.

흐름: 주문 가져오기 → (공급사 발주·송장 받기는 기존 화면) → 송장 보내기.
롯데온의 특징 (설명서 05·06번):
- 새 주문(출고지시)을 가져오면 **반드시** "연동완료"를 롯데온에 알려야 한다. 알리기 전에는 고객이 취소하면 바로 취소되지만,
  알린 뒤에는 고객의 취소 요청을 우리가 승인해야 취소된다.
- 반품·교환은 승인 절차가 없고, 고객이 접수하면 바로 "회수지시"로 내려온다 (2022-06-28부터).
- 송장을 보내기 전에 "발송가능여부"를 확인해야 하고, 실패한 건을 같은 내용으로 계속 보내면 이 컴퓨터의 IP가 차단될 수 있다.
  그래서 실패한 주문은 자동으로 다시 보내지 않고 화면에 이유만 보여준다.

주의 (미검증): 실제 키로 호출해 본 적이 없다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.lotteon import (
    BaseLotteonClient,
    DispatchItem,
    LotteonClaim,
    LotteonError,
    LotteonOrder,
    split_key,
)
from app.models.agri import AgriOption, AgriOrder, AgriProduct
from app.services import agri_pricing as pricing
from app.services.agri_smartstore import option_prices
from app.services.agri_smartstore_orders import NeedsConfirmation, OrderActionError

MARKET = "lotteon"
MOCK_PREFIX = "MOCK-"
ORDERS_VERIFIED = False

# 롯데온 택배사코드 (배송상태 통보 설명서 표 기준)
COURIER_CODES = {
    "롯데택배": "0001",
    "롯데": "0001",
    "CJ대한통운": "0002",
    "대한통운": "0002",
    "CJ택배": "0002",
    "CJ": "0002",
    "우체국택배": "0004",
    "우체국": "0004",
    "로젠택배": "0005",
    "로젠": "0005",
    "한진택배": "0006",
    "한진": "0006",
    "경동택배": "0024",
    "경동": "0024",
    "대신택배": "0028",
    "대신": "0028",
    "일양로지스": "0041",
    "천일택배": "0043",
    "합동택배": "0049",
    "합동": "0049",
    "농협택배": "0081",
    "자체배송": "9000",
}
_CLAIM_LABEL = {"cancel": "취소", "return": "반품", "exchange": "교환"}


def courier_code(name: str | None) -> str | None:
    if not name:
        return None
    key = name.replace(" ", "").upper()
    for label, code in COURIER_CODES.items():
        if label.upper() == key:
            return code
    return name.strip() if name.strip() in set(COURIER_CODES.values()) else None


def _mode_mismatch(order: AgriOrder, client: BaseLotteonClient) -> str | None:
    is_practice = order.market_order_no.startswith(MOCK_PREFIX)
    if client.is_mock and not is_practice:
        return "실제 주문입니다. 지금은 연습 모드라서 처리하지 않았습니다."
    if not client.is_mock and is_practice:
        return "연습으로 만든 주문이라 실제 롯데온으로 보내지 않았습니다."
    return None


def find_option(session: Session, lo: LotteonOrder) -> tuple[AgriOption | None, str]:
    """롯데온 주문이 우리 어느 옵션인지 찾는다 (우리가 등록할 때 넣은 업체단품번호 agri-옵션번호)."""
    match = re.fullmatch(r"agri-(\d+)", lo.eitm_no or "")
    if match:
        option = session.get(AgriOption, int(match.group(1)))
        if option is not None:
            return option, ""
    match = re.fullmatch(r"agri-(\d+)", lo.epd_no or "")
    if not match:
        return None, "우리 시스템으로 올린 상품이 아닙니다 (업체상품번호가 없습니다)."
    product = session.get(AgriProduct, int(match.group(1)))
    if product is None or not product.options:
        return None, f"상품번호 {match.group(1)}을(를) 찾을 수 없습니다."
    if lo.option_name:
        by_label = [o for o in product.options if o.label and o.label in lo.option_name]
        if len(by_label) == 1:
            return by_label[0], ""
    if len(product.options) == 1:
        return product.options[0], ""
    return None, f"옵션을 알아볼 수 없습니다 ({lo.option_name or '옵션 정보 없음'}). 직접 입력해 주세요."


def _find_order(session: Session, key: str) -> AgriOrder | None:
    return session.execute(
        select(AgriOrder).where(AgriOrder.market == MARKET, AgriOrder.market_order_no == key)
    ).unique().scalar_one_or_none()


async def sync_orders(session: Session, client: BaseLotteonClient, settings: dict, hours: int = 24) -> dict:
    """새 주문과 반품·교환 회수지시, 고객의 취소 요청을 가져온다."""
    now = datetime.now(timezone.utc)
    stats: dict = {
        "created": 0, "claims_updated": 0, "cancelled": 0, "marked_shipped": 0, "ignored": 0,
        "skipped": [], "warnings": [],
    }
    fee_rate = str(settings["fee_rates"].get(MARKET, 0))
    to_complete: list[LotteonOrder] = []

    for lo in await client.fetch_orders(now - timedelta(hours=hours), now):
        existing = _find_order(session, lo.key)
        if lo.dv_rtrv_dvs_cd == "RTRV" or lo.od_typ_cd in ("30", "40"):
            # 반품·교환 회수지시: 원래 주문에 표시만 한다 (롯데온에는 반품 승인 절차가 없다).
            original = _find_order(session, f"{lo.od_no}-{lo.od_seq}-{lo.orgl_proc_seq or 1}")
            if original is None:
                stats["skipped"].append({"product_order_id": lo.key, "reason": "원래 주문을 찾을 수 없는 반품·교환입니다."})
                continue
            kind = "exchange" if lo.od_typ_cd == "30" else "return"
            if (original.claim_type, original.claim_status) != (kind, "requested"):
                original.claim_type, original.claim_status = kind, "requested"
                stats["claims_updated"] += 1
            to_complete.append(lo)
            continue
        if lo.od_typ_cd != "10":
            stats["ignored"] += 1
            continue
        if existing is not None:
            to_complete.append(lo)  # 이미 저장했지만 롯데온에 연동완료를 못 보낸 주문
            continue
        option, reason = find_option(session, lo)
        if option is None:
            stats["skipped"].append({"product_order_id": lo.key, "reason": reason})
            continue
        if not lo.receiver_name or not lo.address:
            stats["skipped"].append({"product_order_id": lo.key, "reason": "받는 분 이름이나 주소가 비어 있습니다."})
            continue
        session.add(
            AgriOrder(
                market=MARKET,
                market_order_no=lo.key,
                option_id=option.option_id,
                quantity=lo.quantity,
                buyer_name=lo.buyer_name,
                receiver_name=lo.receiver_name,
                receiver_phone=lo.receiver_phone or "-",
                zipcode=lo.zipcode,
                address=lo.address,
                delivery_memo=lo.memo,
                sale_amount=lo.amount,
                unit_cost=pricing.unit_cost(option.supply_price, option.shipping_fee),
                fee_rate_percent=fee_rate,
                memo=f"롯데온 주문번호 {lo.od_no}",
            )
        )
        stats["created"] += 1
        to_complete.append(lo)
    session.commit()  # 먼저 우리 쪽에 저장한 뒤에 롯데온에 알린다 (알린 뒤에는 고객의 즉시 취소가 막힌다)

    if to_complete:
        try:
            await client.complete_interface(to_complete)
        except LotteonError as exc:
            stats["warnings"].append(f"주문은 저장했지만 롯데온에 '연동완료'를 알리지 못했습니다: {exc}. 다시 [주문 가져오기]를 눌러 주세요.")

    try:
        await _sync_cancel_claims(session, client, now, stats)
    except LotteonError as exc:
        stats["warnings"].append(f"취소 요청 조회에 실패했습니다: {exc}")
    session.commit()
    stats["mode"] = "mock" if client.is_mock else "real"
    return stats


async def _sync_cancel_claims(session: Session, client: BaseLotteonClient, now: datetime, stats: dict) -> None:
    for claim in await client.fetch_cancel_claims(now - timedelta(days=29), now):
        for item in claim.items:
            order = _find_order(session, f"{claim.od_no}-{item.od_seq}-{item.orgl_proc_seq}")
            if order is None:
                continue
            if item.step == "02":
                new = ("cancel", "requested")
            elif item.step == "21":
                if order.status != "cancelled":
                    order.status = "cancelled"
                    stats["cancelled"] += 1
                new = ("cancel", "done")
            elif item.step == "22":  # 고객이 취소 요청을 철회
                new = (None, None) if order.claim_status == "requested" and order.claim_type == "cancel" else (order.claim_type, order.claim_status)
            else:
                continue
            if new != (order.claim_type, order.claim_status):
                order.claim_type, order.claim_status = new
                stats["claims_updated"] += 1


async def dispatch_orders(session: Session, client: BaseLotteonClient, order_ids: list[int]) -> dict:
    """송장을 받은 롯데온 주문의 택배사·송장번호를 롯데온에 입력(발송완료 통보)한다."""
    orders = (
        session.execute(select(AgriOrder).where(AgriOrder.order_id.in_(order_ids), AgriOrder.market == MARKET))
        .unique()
        .scalars()
        .all()
    )
    result: dict = {"sent": [], "failed": [], "skipped": [], "warnings": []}

    def row(order: AgriOrder, **extra) -> dict:
        return {"order_id": order.order_id, "market_order_no": order.market_order_no, **extra}

    items: list[DispatchItem] = []
    by_key: dict[str, AgriOrder] = {}
    for order in sorted(orders, key=lambda o: o.order_id):
        code = courier_code(order.courier)
        reason = None
        if order.status != "invoiced" or not order.tracking_no:
            reason = "송장을 받은 주문(송장 받음)이 아닙니다."
        elif order.claim_status == "requested":
            reason = f"고객의 {_CLAIM_LABEL.get(order.claim_type, '클레임')} 요청이 있어 보내지 않았습니다. 먼저 처리하세요."
        elif (mismatch := _mode_mismatch(order, client)) is not None:
            reason = mismatch
        elif code is None:
            reason = f"택배사 '{order.courier}'의 롯데온 코드를 모릅니다. 택배사 이름을 CJ대한통운 등으로 고쳐 주세요."
        if reason:
            result["skipped"].append(row(order, reason=reason))
            continue
        od_no, od_seq, proc_seq = split_key(order.market_order_no)
        # 발송 전 확인: 이미 취소된 주문을 보내면 실패하고, 반복하면 IP가 차단될 수 있다.
        try:
            check = await client.check_sendable(od_no, od_seq, proc_seq)
        except LotteonError as exc:
            result["failed"].append(row(order, reason=str(exc)))
            continue
        if not check.ok:
            result["failed"].append(row(order, reason=check.message))
            continue
        items.append(DispatchItem(od_no, od_seq, proc_seq, code, order.tracking_no, order.quantity))
        by_key[order.market_order_no] = order

    if items:
        for outcome in await client.dispatch(items):
            order = by_key[outcome.key]
            if outcome.ok:
                order.status = "shipped"
                result["sent"].append(row(order))
            else:
                result["failed"].append(row(order, reason=outcome.message))
    session.commit()
    return result


async def _open_claim(client: BaseLotteonClient, order: AgriOrder) -> tuple[LotteonClaim, str]:
    """이 주문에 걸린 취소 요청(요청 단계)을 롯데온에서 다시 가져와 클레임번호와 사유코드를 알아낸다."""
    od_no, od_seq, proc_seq = split_key(order.market_order_no)
    now = datetime.now(timezone.utc)
    for claim in await client.fetch_cancel_claims(now - timedelta(days=29), now, od_no):
        mine = [i for i in claim.items if i.od_seq == od_seq and i.orgl_proc_seq == proc_seq and i.step == "02"]
        if mine:
            reason = mine[0].reason_code
            # 승인 사유는 고객이 낸 취소 사유를 그대로 쓴다 (설명서의 승인 사유 표는 101~117)
            return LotteonClaim(claim.od_no, claim.clm_no, mine), reason if reason.isdigit() else "106"
    raise OrderActionError("롯데온에서 이 주문의 취소 요청을 찾지 못했습니다. 이미 철회되었거나 처리되었을 수 있습니다. [주문 가져오기]를 눌러 보세요.")


def _check_claim_state(order: AgriOrder, client: BaseLotteonClient, want: str) -> None:
    if order.market != MARKET:
        raise OrderActionError("롯데온 주문이 아닙니다.")
    if order.claim_status != "requested":
        raise OrderActionError("처리할 취소 요청이 없습니다. 먼저 [롯데온 주문 가져오기]를 눌러 보세요.")
    if order.claim_type in ("return", "exchange"):
        raise OrderActionError(
            "롯데온의 반품·교환은 승인 절차가 없고, 고객이 접수하면 바로 회수가 진행됩니다. "
            "공급사와 상품 회수 일정을 잡아 주세요."
        )
    if order.claim_type != "cancel":
        raise OrderActionError("취소 요청이 아닙니다.")
    mismatch = _mode_mismatch(order, client)
    if mismatch:
        raise OrderActionError(mismatch)


async def approve_cancel(session: Session, client: BaseLotteonClient, order: AgriOrder, confirmed: bool) -> AgriOrder:
    """고객의 취소 요청을 승인한다. 승인하면 되돌릴 수 없다."""
    _check_claim_state(order, client, "approve")
    if order.status == "shipped":
        raise OrderActionError("이미 발송 처리된 주문입니다. 취소를 승인하지 말고 [취소 거부(이미 발송)]를 누르세요.")
    if order.status in ("ordered", "invoiced") and not confirmed:
        raise NeedsConfirmation(
            "이미 공급사에 발주한 주문입니다.\n공급사에 연락해서 출고를 막았는지 먼저 확인하세요.\n출고됐다면 취소를 승인하지 말고 거부해야 합니다."
        )
    claim, reason = await _open_claim(client, order)
    outcome = await client.approve_cancel(claim, reason)
    if not outcome.ok:
        raise LotteonError(outcome.message or "취소 승인이 거절되었습니다.")
    order.claim_status = "done"
    order.status = "cancelled"
    session.commit()
    session.refresh(order)
    return order


async def hold_cancel(session: Session, client: BaseLotteonClient, order: AgriOrder) -> AgriOrder:
    """고객의 취소 요청을 거부한다 (이미 발송한 주문 등)."""
    _check_claim_state(order, client, "hold")
    claim, _ = await _open_claim(client, order)
    reason = "901" if order.status == "shipped" else "902"  # 901 이미 발송 / 902 고객과 협의 후 거부
    outcome = await client.hold_cancel(claim, reason)
    if not outcome.ok:
        raise LotteonError(outcome.message or "취소 거부가 거절되었습니다.")
    order.claim_type, order.claim_status = "cancel", "rejected"
    session.commit()
    session.refresh(order)
    return order


def create_practice_order(session: Session, client: BaseLotteonClient, settings: dict, option_id: int, quantity: int) -> LotteonOrder:
    """연습 모드에서 '고객이 주문한 것처럼' 가짜 출고지시를 만든다."""
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
        epd_no=f"agri-{product.product_id}",
        eitm_no=f"agri-{option.option_id}",
        product_name=product.name,
        option_name=option.label,
        quantity=quantity,
        amount=price * quantity,
        buyer_name="연습 고객",
        buyer_phone="010-0000-0000",
        receiver_name="연습 고객",
        receiver_phone="010-0000-0000",
        zipcode="12345",
        address="서울시 연습구 연습로 1",
        memo="연습용 주문입니다",
    )
