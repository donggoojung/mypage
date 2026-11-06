"""농산물 상품을 토스쇼핑에 등록하기 위한 점검, 요청 내용 만들기, 등록.

항목 이름은 토스쇼핑 개발 문서의 "상품 등록 v3" 명세를 그대로 썼다 (`agri/토스쇼핑_연동_메모.md`).
옵션마다 판매가를 따로 적는다. 등록하면 토스쇼핑 검수를 거친 뒤에 노출된다.

주의 (미검증): 실제 키로 호출해 본 적이 없다. 카테고리마다 정해진 구매옵션 이름·값에 맞아야 해서,
처음 등록할 때 거절 사유(`error.reason`)를 보고 설정을 고쳐야 할 수 있다.
"""

from __future__ import annotations

import html
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.toss import BaseTossClient, TossError
from app.models.agri import AgriListing, AgriProduct
from app.services import agri_pricing as pricing
from app.services.agri_lotteon import is_domestic, notice_values
from app.services.agri_smartstore import AlreadyListed, SmartstoreProblems, option_prices

MARKET = "toss"
PAYLOAD_VERIFIED = False
NAME_PATTERN = re.compile(r"^[0-9a-zA-Z가-힣 ()\-·\[\]/&+,~.*_#]{1,100}$")
TAG_PATTERN = re.compile(r"[0-9a-zA-Z가-힣]{1,10}")
MAX_PREP_DAYS = 14

# 고시 항목 제목에 이 글자가 있으면 해당 값을 넣는다 (위에서부터 먼저 맞는 것). 못 맞추면 "상세페이지 참고".
NOTICE_RULES = [
    ("phone", ("상담", "전화")),
    ("caution", ("소비자 안전", "주의사항")),
    ("storage", ("보관", "취급방법")),
    ("composition", ("상품구성", "구성품")),
    ("packing", ("제조연월일", "소비기한", "유통기한", "품질유지", "포장일")),
    ("origin", ("원산지",)),
    ("producer", ("생산자", "수입자", "제조자")),
    ("weight", ("포장단위", "용량", "중량", "수량", "크기")),
    ("name", ("품목", "명칭", "제품명")),
    ("law", ("유전자", "지리적", "관련법", "표시사항", "등급")),
]


class TossProblems(SmartstoreProblems):
    """등록 전에 고쳐야 할 것이 있을 때 (스마트스토어·롯데온과 같은 모양)."""


def _digits(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def search_tags(product: AgriProduct) -> list[str]:
    words = re.split(r"[^0-9a-zA-Z가-힣]+", f"{product.name} {pricing.CATEGORIES.get(product.category, '')}")
    tags: list[str] = []
    for word in words:
        word = word[:10]
        if word and TAG_PATTERN.fullmatch(word) and word not in tags:
            tags.append(word)
    return tags[:10]


def match_notice(items: list[dict], values: dict[str, str]) -> list[dict]:
    """토스쇼핑이 알려 준 고시 항목(id, title)마다 우리 정보를 제목 글자로 찾아 넣는다."""
    result = []
    for item in items:
        title = str(item.get("title") or "")
        content = "상세페이지 참고"
        for key, words in NOTICE_RULES:
            if any(w in title for w in words) and values.get(key):
                content = values[key]
                break
        result.append({"id": item["id"], "content": content})
    return result


def collect_problems(product: AgriProduct, settings: dict) -> list[str]:
    """등록 전에 막아야 하는 문제. 비어 있으면 등록해도 된다."""
    box = settings.get("toss", {})
    where = "설정 탭의 '토스쇼핑 등록 설정'"
    problems: list[str] = []

    if product.status == "stopped":
        problems.append("판매 중지로 표시된 상품입니다. 상태를 '판매 준비'로 바꿔 주세요.")
    if not NAME_PATTERN.fullmatch(product.name or ""):
        problems.append("상품명은 한글·영문·숫자와 ( ) - · [ ] / & + , ~ . * _ # 만 쓸 수 있고 100자까지입니다. 다른 기호는 지워 주세요.")
    if not product.origin:
        problems.append("원산지를 입력해 주세요. 농산물은 원산지 표시가 법적 의무입니다.")
    if not product.producer:
        problems.append("생산자를 입력해 주세요. 상품정보고시에 들어가는 항목입니다.")
    if not product.image_url:
        problems.append("대표 사진 주소를 입력해 주세요. (토스쇼핑은 2MB 이하 jpg·png, 1:1 비율, 600×600 이상)")
    if not settings.get("cs_phone"):
        problems.append("고객센터 전화번호가 비어 있습니다. 설정 탭에서 넣어 주세요.")
    if product.category == "rice":
        for label, value in (("품종", product.variety), ("생산연도", product.harvest_year), ("등급", product.grade)):
            if not value:
                problems.append(f"쌀은 양곡 표시사항이 필요합니다. {label}을(를) 입력해 주세요.")

    if _digits((box.get("leaf_category_ids") or {}).get(product.category)) is None:
        problems.append(f"토스쇼핑 카테고리 번호가 비어 있습니다 (최하위 카테고리). {where}의 '값 찾기'로 확인해 넣어 주세요.")
    if not (box.get("sales_option_keys") or {}).get(product.category):
        problems.append(f"토스쇼핑 구매옵션 이름이 비어 있습니다. 카테고리 템플릿에서 옵션(예: 중량)의 이름을 확인해 {where}에 넣어 주세요.")
    if not box.get("notice_category_code"):
        problems.append(f"토스쇼핑 정보제공 고시 카테고리 코드가 비어 있습니다. {where}의 '값 찾기'로 확인해 넣어 주세요.")
    if _digits(box.get("exchange_refund_location_id")) is None:
        problems.append(f"토스쇼핑 교환반품지 ID가 비어 있습니다. 파트너스에서 교환·반품지를 등록한 뒤 {where}에 넣어 주세요.")
    if int(box.get("prep_days", 3)) > MAX_PREP_DAYS:
        problems.append(f"상품 준비 기간은 최대 {MAX_PREP_DAYS}일입니다.")

    rows = option_prices(product, settings, MARKET)
    if not rows:
        problems.append("판매할 수 있는 옵션이 없습니다. (모든 옵션이 품절이거나 옵션이 없습니다)")
        return problems
    for row in rows:
        if row["profit"] < row["min_profit"]:
            problems.append(
                f"옵션 '{row['label']}'의 판매가 {row['price']:,}원은 예상 이익이 {row['profit']:,}원으로 "
                f"최소 이익 {row['min_profit']:,}원보다 낮습니다. 판매가를 올려 주세요."
            )
    return problems


def _detail_html(product: AgriProduct, settings: dict, rows: list[dict]) -> str:
    esc = html.escape
    options = "".join(f"<li>{esc(r['label'])}</li>" for r in rows)
    notice = esc(pricing.product_notice_text(product, settings))
    return (
        f"<div><h2>{esc(product.name)}</h2><ul>{options}</ul>"
        f'<pre style="white-space:pre-wrap">{notice}</pre></div>'
    )


def product_code(product: AgriProduct) -> str:
    return f"agri-{product.product_id}"


def item_code(option) -> str:
    return f"agri-{option.option_id}"


def build_payload(product: AgriProduct, settings: dict, notice_items: list[dict]) -> dict:
    """토스쇼핑 상품 등록(v3) 요청 내용."""
    box = settings["toss"]
    rows = option_prices(product, settings, MARKET)
    stock = int(box.get("stock_quantity", 999))
    hours = settings.get("claim_hours") or 24
    key = box["sales_option_keys"][product.category]
    values = notice_values(product, settings, rows)

    items = []
    for index, row in enumerate(rows):
        items.append(
            {
                "quantity": stock,
                # 정상가는 판매가와 같게 둔다 (정상가 ≥ 판매가여야 한다). 별도 정상가가 필요하면 나중에 옵션에 추가한다.
                "originPrice": row["price"],
                "regularSalePrice": row["price"],
                "salesOptions": [{"key": key, "value": row["label"]}],
                "itemManagementCode": item_code(row["option"]),
                "itemThumbnails": [{"itemThumbnailUrl": product.image_url}],
                "isMainOption": index == 0,
            }
        )

    delivery: dict = {
        "deliveryType": "NORMAL",
        "deliveryMethod": "NORMAL",
        "preparationDays": int(box.get("prep_days", 3)),
        # 택배비는 판매가에 이미 넣어서 계산했으므로 고객에게는 무료배송으로 보인다.
        "deliveryFeeType": "FREE",
        "deliveryFee": 0,
        "minimumPurchasePrice": 0,
        "jejuDeliveryFee": int(box.get("jeju_fee", 3000)),
        "islandsMountainsDeliveryFee": int(box.get("islands_fee", 3000)),
        "deliveryBlockedRegionPolicyId": int(box.get("blocked_region_policy_id", 1)),
    }
    if _digits(box.get("delivery_location_id")) is not None:
        delivery["deliveryLocationId"] = int(box["delivery_location_id"])

    return {
        "productName": product.name,
        "productManagementCode": product_code(product),
        "isTaxFree": True,  # 가공하지 않은 농산물은 면세
        "searchTags": search_tags(product),
        "leafCategoryId": int(box["leaf_category_ids"][product.category]),
        "items": items,
        "productThumbnails": [{"productThumbnailUrl": product.image_url}],
        "productDescriptionHtml": {"html": _detail_html(product, settings, rows)},
        "deliveryPolicy": delivery,
        "exchangeReturnPolicy": {
            "exchangeRefundLocationId": int(box["exchange_refund_location_id"]),
            "refundOneWayDeliveryFee": int(box.get("refund_one_way_fee", 3000)),
            "exchangeRoundTripDeliveryFee": int(box.get("exchange_round_trip_fee", 6000)),
            "applicationTermDescription": (
                f"신선식품 특성상 단순 변심 교환·반품은 어렵습니다. 상품에 문제가 있으면 받으신 뒤 {hours}시간 안에 "
                f"사진과 함께 문의해 주세요. 문의: {settings.get('cs_phone', '')}"
            ),
            "isReturnRequired": True,
        },
        "productMandatoryNotice": {
            "mandatoryNoticeCode": box["notice_category_code"],
            "mandatoryNoticeItems": match_notice(notice_items, values),
        },
    }


def listing_dict(listing: AgriListing | None) -> dict | None:
    if listing is None:
        return None
    return {
        "market": listing.market,
        "market_product_no": listing.market_product_no,
        "channel_product_no": listing.channel_product_no,
        "status": listing.status,
        "is_mock": listing.is_mock,
        "registered_prices": listing.registered_prices,
        "registered_at": listing.registered_at.isoformat() if listing.registered_at else None,
    }


def get_listing(session: Session, product_id: int) -> AgriListing | None:
    return session.execute(
        select(AgriListing).where(AgriListing.product_id == product_id, AgriListing.market == MARKET)
    ).scalar_one_or_none()


async def preview(product: AgriProduct, settings: dict, listing: AgriListing | None, client: BaseTossClient) -> dict:
    """토스쇼핑에 아무것도 올리지 않고, 점검 결과와 보낼 내용을 미리 보여준다 (고시 항목 조회만 한다)."""
    problems = collect_problems(product, settings)
    rows = option_prices(product, settings, MARKET)
    payload = None
    if not problems:
        try:
            items = await client.notice_items(settings["toss"]["notice_category_code"])
            payload = build_payload(product, settings, items)
        except TossError as exc:
            problems.append(f"토스쇼핑 고시 항목을 불러오지 못했습니다: {exc}")
    return {
        "problems": problems,
        "prices": [{"label": r["label"], "price": r["price"], "profit": r["profit"]} for r in rows],
        "payload": payload,
        "payload_verified": PAYLOAD_VERIFIED,
        "listing": listing_dict(listing),
    }


async def register_product(session: Session, product: AgriProduct, settings: dict, client: BaseTossClient) -> AgriListing:
    problems = collect_problems(product, settings)
    if problems:
        raise TossProblems(problems)
    existing = get_listing(session, product.product_id)
    if existing and existing.is_mock == client.is_mock:
        raise AlreadyListed("이미 토스쇼핑에 등록된 상품입니다.")

    items = await client.notice_items(settings["toss"]["notice_category_code"])
    product_id = await client.register_product(build_payload(product, settings, items))

    listing = existing or AgriListing(product_id=product.product_id, market=MARKET)
    listing.market_product_no = str(product_id)
    listing.channel_product_no = None
    listing.status = "registered"  # 토스쇼핑 검수 후 노출된다
    listing.is_mock = client.is_mock
    listing.registered_prices = {r["label"]: r["price"] for r in option_prices(product, settings, MARKET)}
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing
