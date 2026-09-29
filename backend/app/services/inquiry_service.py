"""쿠팡 고객문의 수집 → AI 답변 초안 → 관리자 확인 후 답변 등록.

답변은 절대 자동으로 등록하지 않는다 — AI가 사이즈/재고를 잘못 말하면 반품·클레임으로
이어지므로, 초안은 대시보드에 띄우고 대표님이 확인(수정)한 뒤 [답변 등록]을 눌러야만
쿠팡에 올라간다. 쿠팡은 답변이 늦으면 판매자 점수를 깎기 때문에, 새 문의는 텔레그램으로
바로 알린다(쿠팡 고객센터 이관 문의는 더 급해서 알림에 따로 표시한다).
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.llm.gemini_client import GeminiClient
from app.integrations.markets.coupang import CoupangWingClient
from app.integrations.messaging.telegram_admin import TelegramAdminNotifier
from app.models.customer_inquiry import CustomerInquiry
from app.models.enums import InquiryStatus, InquiryType, MarketType, SourcePlatform
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping

_TYPE_LABELS = {
    InquiryType.PRODUCT: "상품 문의",
    InquiryType.CALL_CENTER: "쿠팡 고객센터 이관 문의(급함)",
}


async def collect_new_inquiries(session: Session, use_mock: bool | None = None) -> dict:
    """아직 답변 안 된 쿠팡 문의를 가져와, 처음 보는 문의만 저장하고 AI 초안을 붙인다."""
    settings = get_settings()
    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    vendor_id = settings.coupang_vendor_id

    fetched: list[tuple[InquiryType, dict]] = []
    errors: list[str] = []
    for inquiry_type, fetch in (
        (InquiryType.PRODUCT, client.fetch_unanswered_product_inquiries),
        (InquiryType.CALL_CENTER, client.fetch_unanswered_call_center_inquiries),
    ):
        # 한 종류 API가 실패해도(권한 없음 등) 다른 종류 문의는 계속 수집한다.
        try:
            fetched.extend((inquiry_type, raw) for raw in await fetch(vendor_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{inquiry_type.value}: {exc}")

    created_ids: list[int] = []
    for inquiry_type, raw in fetched:
        market_inquiry_id = str(raw.get("inquiryId") or "")
        if not market_inquiry_id:
            continue
        exists = (
            session.query(CustomerInquiry)
            .filter_by(market_type=MarketType.COUPANG, inquiry_type=inquiry_type, market_inquiry_id=market_inquiry_id)
            .first()
        )
        if exists:
            continue

        product = _match_product(session, raw)
        question = _extract_question(raw)
        inquiry = CustomerInquiry(
            market_type=MarketType.COUPANG,
            inquiry_type=inquiry_type,
            market_inquiry_id=market_inquiry_id,
            product_id=product.product_id if product else None,
            product_label=f"{product.brand_name} {product.product_name} ({product.style_code})" if product else None,
            question=question,
            inquired_at=str(raw.get("inquiryAt") or raw.get("createdAt") or "") or None,
            parent_answer_id=_extract_parent_answer_id(raw),
            status=InquiryStatus.PENDING,
            raw_json=raw,
        )
        try:
            inquiry.ai_draft_answer = await GeminiClient(settings=settings).draft_inquiry_answer(
                question, _product_context(session, product)
            )
        except Exception as exc:  # noqa: BLE001 - 초안 실패해도 문의 자체는 저장/알림해야 한다.
            print(f"  AI 답변 초안 생성 실패({exc}) — 초안 없이 저장합니다.")
        session.add(inquiry)
        session.commit()
        created_ids.append(inquiry.inquiry_id)
        await _notify_new_inquiry(inquiry, use_mock)

    return {"fetched": len(fetched), "created_inquiry_ids": created_ids, "errors": errors}


async def submit_answer(
    session: Session, inquiry_id: int, answer: str, use_mock: bool | None = None
) -> CustomerInquiry:
    """대표님이 확인한 답변을 쿠팡에 등록한다."""
    inquiry = session.get(CustomerInquiry, inquiry_id)
    if inquiry is None:
        raise ValueError(f"inquiry_id={inquiry_id} 문의가 없습니다.")
    if inquiry.status == InquiryStatus.ANSWERED:
        raise ValueError("이미 답변을 등록한 문의입니다.")
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("답변 내용이 비어 있습니다.")

    settings = get_settings()
    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    if inquiry.inquiry_type == InquiryType.CALL_CENTER:
        await client.reply_call_center_inquiry(
            settings.coupang_vendor_id, inquiry.market_inquiry_id, answer, inquiry.parent_answer_id
        )
    else:
        await client.reply_product_inquiry(settings.coupang_vendor_id, inquiry.market_inquiry_id, answer)

    inquiry.final_answer = answer
    inquiry.status = InquiryStatus.ANSWERED
    inquiry.answered_at = datetime.now(UTC)
    session.commit()
    return inquiry


def _extract_question(raw: dict) -> str:
    # 실API 미검증 — 필드명이 문의 종류마다 다를 수 있어 후보를 순서대로 본다.
    for key in ("content", "inquiryContent", "question"):
        if raw.get(key):
            return str(raw[key])
    return "(문의 내용을 읽지 못했습니다 — WING에서 직접 확인해주세요)"


def _extract_parent_answer_id(raw: dict) -> str | None:
    replies = raw.get("replies") or raw.get("commentDtoList") or []
    if isinstance(replies, list) and replies:
        answer_id = replies[-1].get("answerId") if isinstance(replies[-1], dict) else None
        return str(answer_id) if answer_id else None
    return None


def _match_product(session: Session, raw: dict) -> MasterProduct | None:
    seller_product_id = raw.get("sellerProductId")
    if seller_product_id:
        listing = (
            session.query(MarketListing)
            .filter_by(market_type=MarketType.COUPANG, market_product_id=str(seller_product_id))
            .first()
        )
        if listing:
            return session.get(MasterProduct, listing.product_id)

    vendor_item_id = raw.get("vendorItemId")
    if vendor_item_id:
        for listing in session.query(MarketListing).filter_by(market_type=MarketType.COUPANG).all():
            if str(vendor_item_id) in {str(v) for v in (listing.vendor_item_ids_json or {}).values()}:
                return session.get(MasterProduct, listing.product_id)
    return None


def _product_context(session: Session, product: MasterProduct | None) -> str:
    if product is None:
        return "상품 정보를 찾지 못했습니다(일반적인 안내만 하세요)."

    listing = (
        session.query(MarketListing)
        .filter_by(product_id=product.product_id, market_type=MarketType.COUPANG)
        .first()
    )
    mapping = (
        session.query(SourceMapping)
        .filter_by(product_id=product.product_id, source_platform=SourcePlatform.ABC_MART)
        .first()
    )
    size_stock = (mapping.size_stock_json if mapping else None) or {}
    available = [size for size, info in size_stock.items() if not info.get("is_sold_out")]
    lines = [
        f"브랜드: {product.brand_name}",
        f"상품명: {product.product_name}",
        f"품번: {product.style_code}",
        f"판매가: {int(listing.selling_price):,}원" if listing else "",
        f"구매 가능 사이즈: {', '.join(available) if available else '확인 불가'}",
    ]
    specs = product.raw_specs_json or {}
    lines += [f"{key}: {value}" for key, value in specs.items() if value and value != "상품 상세 참조"]
    return "\n".join(line for line in lines if line)


async def _notify_new_inquiry(inquiry: CustomerInquiry, use_mock: bool | None) -> None:
    text = (
        f"📩 쿠팡 새 {_TYPE_LABELS[inquiry.inquiry_type]}\n"
        f"상품: {inquiry.product_label or '(연결된 상품 없음)'}\n"
        f"질문: {inquiry.question}\n\n"
        f"AI 답변 초안:\n{inquiry.ai_draft_answer or '(초안 생성 실패 — 직접 작성해주세요)'}\n\n"
        "대시보드 '고객문의' 탭에서 확인·수정 후 [답변 등록]을 눌러주세요."
    )
    try:
        await TelegramAdminNotifier(use_mock=use_mock).send_alert(text)
    except Exception as exc:  # noqa: BLE001 - 알림 실패가 문의 저장을 되돌리면 안 된다.
        print(f"  텔레그램 문의 알림 실패: {exc}")
