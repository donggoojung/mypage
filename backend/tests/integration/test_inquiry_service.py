"""쿠팡 고객문의 수집 → AI 답변 초안 → 관리자 확인 후 답변 등록 흐름 테스트."""

from decimal import Decimal

import httpx
import pytest

from app.main import app
from app.models.customer_inquiry import CustomerInquiry
from app.models.enums import InquiryStatus, InquiryType, ListingStatus, MarketType, SourcePlatform
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services import inquiry_service


class _FakeCoupangClient:
    product_inquiries: list[dict] = []
    call_center_inquiries: list[dict] = []
    fail_call_center = False
    replies: list[tuple] = []

    def __init__(self, *args, **kwargs):
        pass

    async def fetch_unanswered_product_inquiries(self, vendor_id):
        return list(self.product_inquiries)

    async def fetch_unanswered_call_center_inquiries(self, vendor_id):
        if self.fail_call_center:
            raise RuntimeError("권한 없음(테스트)")
        return list(self.call_center_inquiries)

    async def reply_product_inquiry(self, vendor_id, inquiry_id, content):
        _FakeCoupangClient.replies.append(("product", inquiry_id, content, None))
        return {"code": 200}

    async def reply_call_center_inquiry(self, vendor_id, inquiry_id, content, parent_answer_id):
        _FakeCoupangClient.replies.append(("call_center", inquiry_id, content, parent_answer_id))
        return {"code": 200}


@pytest.fixture
def fake_client(monkeypatch):
    _FakeCoupangClient.product_inquiries = []
    _FakeCoupangClient.call_center_inquiries = []
    _FakeCoupangClient.fail_call_center = False
    _FakeCoupangClient.replies = []
    monkeypatch.setattr(inquiry_service, "CoupangWingClient", _FakeCoupangClient)
    return _FakeCoupangClient


@pytest.fixture
def listed_product(db_session):
    product = MasterProduct(style_code="HJ9198", brand_name="나이키", product_name="레볼루션 8")
    db_session.add(product)
    db_session.flush()
    db_session.add(
        MarketListing(
            product_id=product.product_id,
            market_type=MarketType.COUPANG,
            market_product_id="16395678159",
            selling_price=Decimal("106000"),
            status=ListingStatus.ACTIVE,
            vendor_item_ids_json={"260": "90000001"},
        )
    )
    db_session.add(
        SourceMapping(
            product_id=product.product_id,
            source_platform=SourcePlatform.ABC_MART,
            source_url="https://abcmart.a-rt.com/product/new?prdtNo=1010125354",
            source_price=71000,
            size_stock_json={"260": {"stock": 3, "is_sold_out": False}, "270": {"stock": 0, "is_sold_out": True}},
        )
    )
    db_session.commit()
    return product


@pytest.mark.asyncio
async def test_collect_saves_new_inquiries_with_ai_draft_and_skips_duplicates(db_session, fake_client, listed_product):
    fake_client.product_inquiries = [
        {"inquiryId": 111, "content": "270 사이즈 있나요?", "sellerProductId": 16395678159, "inquiryAt": "2026-09-29"}
    ]
    fake_client.call_center_inquiries = [
        {"inquiryId": 222, "content": "배송 언제 오나요?", "vendorItemId": 90000001, "replies": [{"answerId": 777}]}
    ]

    result = await inquiry_service.collect_new_inquiries(db_session, use_mock=True)
    again = await inquiry_service.collect_new_inquiries(db_session, use_mock=True)

    assert len(result["created_inquiry_ids"]) == 2
    assert again["created_inquiry_ids"] == []  # 같은 문의는 다시 저장/알림하지 않는다
    rows = {r.market_inquiry_id: r for r in db_session.query(CustomerInquiry).all()}
    assert rows["111"].inquiry_type == InquiryType.PRODUCT
    assert rows["111"].product_id == listed_product.product_id
    assert rows["111"].ai_draft_answer
    assert rows["111"].status == InquiryStatus.PENDING
    # 고객센터 이관 문의는 vendorItemId로 상품을 찾고, 답변에 필요한 상위 답변ID를 저장한다.
    assert rows["222"].product_id == listed_product.product_id
    assert rows["222"].parent_answer_id == "777"


@pytest.mark.asyncio
async def test_collect_continues_when_one_inquiry_api_fails(db_session, fake_client):
    fake_client.fail_call_center = True
    fake_client.product_inquiries = [{"inquiryId": 333, "content": "정품인가요?"}]

    result = await inquiry_service.collect_new_inquiries(db_session, use_mock=True)

    assert len(result["created_inquiry_ids"]) == 1
    assert result["errors"] and "call_center" in result["errors"][0]


def test_product_context_lists_only_available_sizes(db_session, listed_product):
    context = inquiry_service._product_context(db_session, listed_product)

    assert "구매 가능 사이즈: 260" in context
    assert "270" not in context
    assert "106,000원" in context


@pytest.mark.asyncio
async def test_submit_answer_routes_to_correct_api_and_marks_answered(db_session, fake_client):
    product_q = CustomerInquiry(
        market_type=MarketType.COUPANG, inquiry_type=InquiryType.PRODUCT, market_inquiry_id="111", question="?"
    )
    call_q = CustomerInquiry(
        market_type=MarketType.COUPANG,
        inquiry_type=InquiryType.CALL_CENTER,
        market_inquiry_id="222",
        question="?",
        parent_answer_id="777",
    )
    db_session.add_all([product_q, call_q])
    db_session.commit()

    await inquiry_service.submit_answer(db_session, product_q.inquiry_id, "  안녕하세요, 있습니다.  ", use_mock=True)
    await inquiry_service.submit_answer(db_session, call_q.inquiry_id, "내일 출고됩니다.", use_mock=True)

    assert fake_client.replies == [
        ("product", "111", "안녕하세요, 있습니다.", None),
        ("call_center", "222", "내일 출고됩니다.", "777"),
    ]
    assert product_q.status == InquiryStatus.ANSWERED
    assert product_q.final_answer == "안녕하세요, 있습니다."
    assert product_q.answered_at is not None


@pytest.mark.asyncio
async def test_submit_answer_rejects_empty_and_already_answered(db_session, fake_client):
    q = CustomerInquiry(
        market_type=MarketType.COUPANG, inquiry_type=InquiryType.PRODUCT, market_inquiry_id="111", question="?"
    )
    db_session.add(q)
    db_session.commit()

    with pytest.raises(ValueError):
        await inquiry_service.submit_answer(db_session, q.inquiry_id, "   ", use_mock=True)
    await inquiry_service.submit_answer(db_session, q.inquiry_id, "답변", use_mock=True)
    with pytest.raises(ValueError):
        await inquiry_service.submit_answer(db_session, q.inquiry_id, "또 답변", use_mock=True)
    assert len(fake_client.replies) == 1


@pytest.mark.asyncio
async def test_inquiries_api_lists_pending_first_and_escapes_nothing_server_side(db_session):
    db_session.add_all(
        [
            CustomerInquiry(
                market_type=MarketType.COUPANG,
                inquiry_type=InquiryType.PRODUCT,
                market_inquiry_id="1",
                question="답변된 문의",
                status=InquiryStatus.ANSWERED,
            ),
            CustomerInquiry(
                market_type=MarketType.COUPANG,
                inquiry_type=InquiryType.CALL_CENTER,
                market_inquiry_id="2",
                question="<b>미답변</b>",
            ),
        ]
    )
    db_session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/inquiries")
        empty_answer = await client.post("/api/inquiries/999999/answer", json={"answer": "x"})

    body = response.json()
    assert [q["status"] for q in body] == ["pending", "answered"]
    assert body[0]["question"] == "<b>미답변</b>"  # 이스케이프는 대시보드(escapeHtml)가 담당
    assert empty_answer.status_code == 400
