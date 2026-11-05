"""주문 처리 두 가지: 발송 지연 알리기(2-10), 발송 전에 들어온 취소 요청 처리(2-9).

2-10 발송 지연: 출고가 늦을 때 **가짜 송장을 넣지 않고** 마켓에 "발송 지연"을 알린다 (가짜 송장은 판매 정지 사유).
  - 롯데온: 발송약정일 통보 API로 자동 처리 (고객에게 알림이 간다).
  - 스마트스토어: 발송 지연 API 주소를 아직 확인하지 못해서 자동으로 보내지 않는다. 우리 쪽에는 기록을 남기고,
    스마트스토어센터에서 직접 "발송지연 처리"를 하도록 안내한다.

2-9 발송 전 취소: 이미 공급사에 발주한 주문에 고객의 취소 요청이 오면, **공급사가 출고를 막았는지**에 따라 처리한다.
  - 막았다 → 취소 승인
  - 이미 출고했다 → 송장을 입력하고 취소를 거부 (롯데온은 자동, 스마트스토어는 센터에서 직접)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.lotteon import BaseLotteonClient, DelayItem, LotteonError, split_key
from app.integrations.markets.naver_commerce import BaseSmartstoreClient
from app.models.agri import AgriOrder
from app.services import agri_lotteon_orders as lotteon_orders
from app.services import agri_smartstore_orders as smartstore_orders
from app.services.agri_smartstore_orders import OrderActionError

KST = timezone(timedelta(hours=9))
# 롯데온 발송지연사유코드 (설명서 138번): STK_SHTG 재고부족 / CUST_REQ 고객요청
LOTTEON_DELAY_REASONS = {"stock": "STK_SHTG", "customer": "CUST_REQ"}
DELAY_REASON_LABELS = {"stock": "출고 준비 지연(재고·산지 사정)", "customer": "고객 요청"}
# 발송 늦음 표시 기준 (일): 이 기간이 지나도록 발송하지 못하면 화면에 표시한다
DEFAULT_OVERDUE_DAYS = 3


def overdue_days(market: str, settings: dict) -> int:
    if market == "lotteon":
        return int((settings.get("lotteon") or {}).get("send_days", 2)) or 1
    return DEFAULT_OVERDUE_DAYS


def is_overdue(order: AgriOrder, settings: dict, now: datetime | None = None) -> bool:
    """발송 기한(주문 후 며칠)을 넘겼는데 아직 발송하지 않았고, 지연 알림도 안 한 주문."""
    if order.status not in ("new", "ordered", "invoiced") or order.claim_status == "requested" or order.ordered_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    if order.delayed_until is not None and order.delayed_until > now:
        return False
    return now - order.ordered_at > timedelta(days=overdue_days(order.market, settings))


def _append_memo(order: AgriOrder, text: str) -> None:
    stamp = datetime.now(KST).strftime("%m/%d %H:%M")
    order.memo = f"{order.memo}\n[{stamp}] {text}" if order.memo else f"[{stamp}] {text}"


def parse_due(value: str) -> datetime:
    """'2026-11-09' 같은 날짜를 그날 낮 12시(한국 시간)로. 오늘보다 앞서면 안 된다."""
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise OrderActionError("발송 예정일을 2026-11-09 같은 모양으로 넣어 주세요.") from exc
    if day < datetime.now(KST).date():
        raise OrderActionError("발송 예정일이 오늘보다 앞입니다.")
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=KST)


async def delay_orders(
    session: Session,
    order_ids: list[int],
    due_text: str,
    reason: str,
    lotteon_client: BaseLotteonClient,
    smartstore_client: BaseSmartstoreClient,
) -> dict:
    """주문들의 발송 지연을 알린다 (롯데온은 자동, 스마트스토어는 안내만)."""
    if reason not in DELAY_REASON_LABELS:
        raise OrderActionError("지연 사유를 골라 주세요.")
    due = parse_due(due_text)
    orders = session.execute(select(AgriOrder).where(AgriOrder.order_id.in_(order_ids))).unique().scalars().all()
    result: dict = {"done": [], "manual": [], "failed": [], "skipped": []}

    def row(order: AgriOrder, **extra) -> dict:
        return {"order_id": order.order_id, "market": order.market, "market_order_no": order.market_order_no, **extra}

    lotteon_items: list[DelayItem] = []
    by_key: dict[str, AgriOrder] = {}
    for order in sorted(orders, key=lambda o: o.order_id):
        if order.status in ("shipped", "cancelled"):
            result["skipped"].append(row(order, reason="이미 발송했거나 취소된 주문입니다."))
        elif order.claim_status == "requested":
            result["skipped"].append(row(order, reason="고객의 취소·반품 요청이 있어 먼저 그것을 처리해야 합니다."))
        elif order.market == "lotteon":
            mismatch = lotteon_orders._mode_mismatch(order, lotteon_client)
            if mismatch:
                result["skipped"].append(row(order, reason=mismatch))
                continue
            od_no, od_seq, proc_seq = split_key(order.market_order_no)
            lotteon_items.append(DelayItem(od_no, od_seq, proc_seq, LOTTEON_DELAY_REASONS[reason], due))
            by_key[order.market_order_no] = order
        elif order.market == "smartstore":
            mismatch = smartstore_orders._mode_mismatch(order, smartstore_client)
            if mismatch:
                result["skipped"].append(row(order, reason=mismatch))
                continue
            _record_delay(order, due, reason, "스마트스토어센터에서 발송지연 처리를 직접 함")
            result["manual"].append(row(order))
        else:
            result["skipped"].append(row(order, reason="이 마켓은 아직 발송 지연 알림을 지원하지 않습니다. 판매자센터에서 직접 처리하세요."))

    if lotteon_items:
        try:
            outcomes = await lotteon_client.inform_delay(lotteon_items)
        except LotteonError as exc:
            # 한 건이라도 거절되면 목록 전체가 거절될 수 있다 (예: 발송약정일 최대기간 초과). 화면에 이유를 보여준다.
            for order in by_key.values():
                result["failed"].append(row(order, reason=str(exc)))
            outcomes = []
        for outcome in outcomes:
            order = by_key[outcome.key]
            if outcome.ok:
                _record_delay(order, due, reason, "롯데온에 발송 지연을 알림")
                result["done"].append(row(order))
            else:
                result["failed"].append(row(order, reason=outcome.message))
    session.commit()
    return result


def _record_delay(order: AgriOrder, due: datetime, reason: str, note: str) -> None:
    order.delayed_until = due
    order.delay_reason = reason
    _append_memo(order, f"발송 지연 {due.astimezone(KST):%m/%d}까지 ({DELAY_REASON_LABELS[reason]}) — {note}")


async def resolve_cancel_request(
    session: Session,
    order: AgriOrder,
    decision: str,
    lotteon_client: BaseLotteonClient,
    smartstore_client: BaseSmartstoreClient,
    courier: str | None = None,
    tracking_no: str | None = None,
) -> dict:
    """발송 전 취소 요청을 '공급사 출고 여부'에 따라 처리한다.

    decision = "blocked": 공급사가 출고를 막았다 → 취소 승인
    decision = "shipped": 공급사가 이미 출고했다 → 송장을 저장하고 취소를 거부 (롯데온) / 안내 (스마트스토어)
    """
    if order.claim_status != "requested" or order.claim_type != "cancel":
        raise OrderActionError("처리할 취소 요청이 없습니다. 먼저 [주문 가져오기]를 눌러 보세요.")
    if order.market not in ("lotteon", "smartstore"):
        raise OrderActionError("이 마켓은 아직 자동 처리를 지원하지 않습니다. 판매자센터에서 직접 처리하세요.")

    if decision == "blocked":
        if order.market == "lotteon":
            await lotteon_orders.approve_cancel(session, lotteon_client, order, confirmed=True)
        else:
            await smartstore_orders.approve_claim(session, smartstore_client, order, confirmed=True)
        _append_memo(order, "공급사가 출고를 막은 것을 확인하고 취소를 승인함")
        session.commit()
        return {"result": "approved", "message": "취소를 승인했습니다."}

    if decision != "shipped":
        raise OrderActionError("처리 방법을 골라 주세요.")
    courier = (courier or order.courier or "").strip()
    tracking = re.sub(r"[^0-9A-Za-z]", "", tracking_no or order.tracking_no or "")
    if not courier or not tracking:
        raise OrderActionError("이미 출고했다면 택배사와 송장번호를 넣어 주세요. (공급사에게 받으세요)")
    order.courier, order.tracking_no = courier, tracking
    if order.status in ("new", "ordered"):
        order.status = "invoiced"
    _append_memo(order, f"공급사가 이미 출고함({courier} {tracking}) — 고객의 취소 요청을 거부")
    if order.market == "lotteon":
        await lotteon_orders.hold_cancel(session, lotteon_client, order, shipped=True)
        session.commit()
        return {"result": "held", "message": "취소 요청을 거부(이미 발송)하고 송장을 저장했습니다. 이제 [롯데온에 송장 보내기]를 눌러 송장을 입력하세요."}
    session.commit()
    return {
        "result": "manual",
        "message": "송장을 저장했습니다. 스마트스토어는 취소 거부 주소를 아직 확인하지 못해서, 스마트스토어센터에서 이 주문의 취소 요청을 '거부'(발송 처리)해 주세요.",
    }
