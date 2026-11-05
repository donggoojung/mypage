"""농산물 상품을 스마트스토어에 등록하기 위한 점검, 요청 내용 만들기, 등록.

가격은 옵션마다 따로 계산한 스마트스토어 판매가를 쓴다 (옵션에 직접 정한 판매가가 있으면 그 값).
네이버는 "기본 판매가 + 옵션 추가금" 구조라서, 가장 싼 옵션을 기본 판매가로 두고 나머지는 차액을 추가금으로 보낸다.

주의 (미검증): build_payload 가 만드는 요청 내용은 네이버 커머스 API 공개 자료를 보고 작성했지만,
실제 키로 호출해 본 적이 없다. 상품정보고시 유형·원산지 코드·세금 구분 값은 특히 확인이 필요하다.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.naver_commerce import BaseSmartstoreClient
from app.models.agri import AgriListing, AgriOption, AgriProduct
from app.services import agri_pricing as pricing

MARKET = "smartstore"
# 요청 내용의 세부 항목이 실제 API로 확인되었는지. 확인하면 True 로 바꾼다.
PAYLOAD_VERIFIED = False
# 네이버는 옵션 추가금을 기본 판매가의 일정 비율 안으로 제한한다. 정확한 한도는 확인이 필요하다 (미검증).
OPTION_PRICE_MAX_RATIO = 0.5
PRODUCT_NAME_MAX_LENGTH = 100


class SmartstoreProblems(Exception):
    """등록 전에 고쳐야 할 것이 있을 때."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class AlreadyListed(Exception):
    """이미 이 마켓에 등록된 상품일 때."""


def sellable_options(product: AgriProduct) -> list[AgriOption]:
    return [o for o in product.options if not o.is_soldout]


def option_prices(product: AgriProduct, settings: dict) -> list[dict]:
    """판매 가능한 옵션마다 스마트스토어 판매가와 예상 이익."""
    rate = float(settings["fee_rates"].get(MARKET, 0))
    min_profit = int(settings["min_profit"].get(product.category, settings["min_profit"].get("etc", 0)))
    unit = settings.get("price_unit", 100)
    rows = []
    for option in sellable_options(product):
        cost = pricing.unit_cost(option.supply_price, option.shipping_fee)
        price = int(option.sale_price) if option.sale_price else pricing.recommended_price(cost, min_profit, rate, unit)
        rows.append(
            {
                "option": option,
                "label": option.label,
                "price": price,
                "cost": cost,
                "profit": pricing.profit_of(price, cost, rate)["profit"],
                "min_profit": min_profit,
            }
        )
    return rows


def collect_problems(product: AgriProduct, settings: dict) -> list[str]:
    """등록 전에 막아야 하는 문제. 비어 있으면 등록해도 된다."""
    problems: list[str] = []
    ss = settings.get("smartstore", {})

    if product.status == "stopped":
        problems.append("판매 중지로 표시된 상품입니다. 상태를 '판매 준비'로 바꿔 주세요.")
    if len(product.name) > PRODUCT_NAME_MAX_LENGTH:
        problems.append(f"상품명이 {PRODUCT_NAME_MAX_LENGTH}자를 넘습니다. 줄여 주세요.")
    if not product.origin:
        problems.append("원산지를 입력해 주세요. 농산물은 원산지 표시가 법적 의무입니다.")
    if not product.producer:
        problems.append("생산자를 입력해 주세요. 상품정보고시에 들어가는 항목입니다.")
    if not product.image_url:
        problems.append("대표 사진 주소를 입력해 주세요.")
    if not (ss.get("leaf_category_ids") or {}).get(product.category):
        problems.append("스마트스토어 카테고리 번호가 비어 있습니다. 설정 탭의 '스마트스토어 등록 설정'에 넣어 주세요.")
    if not settings.get("cs_phone"):
        problems.append("고객센터 전화번호가 비어 있습니다. 설정 탭에서 넣어 주세요.")
    if product.category == "rice":
        for label, value in (("품종", product.variety), ("생산연도", product.harvest_year), ("등급", product.grade)):
            if not value:
                problems.append(f"쌀은 양곡 표시사항이 필요합니다. {label}을(를) 입력해 주세요.")

    rows = option_prices(product, settings)
    if not rows:
        problems.append("판매할 수 있는 옵션이 없습니다. (모든 옵션이 품절이거나 옵션이 없습니다)")
        return problems

    for row in rows:
        if row["profit"] < row["min_profit"]:
            problems.append(
                f"옵션 '{row['label']}'의 판매가 {row['price']:,}원은 예상 이익이 {row['profit']:,}원으로 "
                f"최소 이익 {row['min_profit']:,}원보다 낮습니다. 판매가를 올려 주세요."
            )
    base = min(r["price"] for r in rows)
    for row in rows:
        if row["price"] - base > base * OPTION_PRICE_MAX_RATIO:
            problems.append(
                f"옵션 '{row['label']}'의 가격이 가장 싼 옵션보다 너무 비쌉니다 "
                f"(차액 {row['price'] - base:,}원). 네이버는 옵션 가격 차이를 제한하므로 상품을 나누어 등록해 주세요."
            )
    return problems


def _detail_html(product: AgriProduct, settings: dict, rows: list[dict]) -> str:
    esc = html.escape
    options = "".join(f"<li>{esc(r['label'])}</li>" for r in rows)
    notice = esc(pricing.product_notice_text(product, settings))
    return (
        '<div style="max-width:860px;margin:0 auto;line-height:1.7;font-size:15px">'
        f"<h2>{esc(product.name)}</h2>"
        f"<ul>{options}</ul>"
        f'<pre style="white-space:pre-wrap;font-family:inherit">{notice}</pre>'
        "</div>"
    )


def build_payload(product: AgriProduct, settings: dict, image_urls: list[str]) -> dict:
    """스마트스토어 상품 등록 요청 내용 (미검증 항목 있음 — 파일 위 설명 참고)."""
    ss = settings["smartstore"]
    rows = option_prices(product, settings)
    base_price = min(r["price"] for r in rows)
    stock = int(ss.get("stock_quantity", 999))
    hours = settings.get("claim_hours") or 24
    cs_phone = settings.get("cs_phone", "")

    detail_attribute: dict = {
        "naverShoppingSearchInfo": {"manufacturerName": product.producer or "", "brandName": ""},
        "minorPurchasable": True,
        "originAreaInfo": {
            "originAreaCode": ss.get("origin_area_code", "00"),
            "content": product.origin or "",
            "plural": False,
        },
        "afterServiceInfo": {
            "afterServiceTelephoneNumber": cs_phone,
            "afterServiceGuideContent": (
                f"신선식품 특성상 단순 변심 교환·반품은 어렵습니다. 상품에 문제가 있으면 받으신 뒤 {hours}시간 안에 "
                "사진과 함께 문의해 주세요."
            ),
        },
        "sellerCodeInfo": {"sellerManagementCode": f"agri-{product.product_id}"},
        # 가공하지 않은 농산물은 부가세 면세
        "taxType": "DUTYFREE",
        "productInfoProvidedNotice": {
            "productInfoProvidedNoticeType": "FOOD",
            "food": {
                "foodItem": product.name,
                "weight": ", ".join(r["label"] for r in rows),
                "amount": "상세페이지 참고",
                "size": "상세페이지 참고",
                "packDateText": product.packing_info or "주문 후 산지에서 포장하여 발송",
                "storageMethod": product.storage or "상세페이지 참고",
                "producer": product.producer or "",
                "relevantLawContent": "해당 없음" if product.category != "rice" else "양곡관리법에 따른 양곡 표시사항 참고",
                "customerServicePhoneNumber": cs_phone,
            },
        },
    }
    if len(rows) > 1:
        detail_attribute["optionInfo"] = {
            "optionCombinationSortType": "CREATE",
            "optionCombinationGroupNames": {"optionGroupName1": "옵션"},
            "optionCombinations": [
                {
                    "optionName1": r["label"],
                    "stockQuantity": stock,
                    "price": r["price"] - base_price,
                    "sellerManagerCode": r["option"].supplier_code or "",
                    "usable": True,
                }
                for r in rows
            ],
        }

    return {
        "originProduct": {
            "statusType": "SALE",
            "saleType": "NEW",
            "leafCategoryId": ss["leaf_category_ids"][product.category],
            "name": product.name,
            "detailContent": _detail_html(product, settings, rows),
            "images": {
                "representativeImage": {"url": image_urls[0]},
                "optionalImages": [{"url": u} for u in image_urls[1:]],
            },
            "salePrice": base_price,
            "stockQuantity": stock,
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryCompany": ss.get("delivery_company", "CJGLS"),
                # 택배비는 판매가에 이미 포함해서 계산했으므로 고객에게 무료배송으로 보인다.
                "deliveryFee": {"deliveryFeeType": "FREE", "baseFee": 0},
                "claimDeliveryInfo": {
                    "returnDeliveryFee": int(ss.get("return_fee", 3000)),
                    "exchangeDeliveryFee": int(ss.get("exchange_fee", 6000)),
                },
            },
            "detailAttribute": detail_attribute,
        },
        "smartstoreChannelProduct": {
            "channelProductName": product.name,
            "naverShoppingRegistration": True,
            "channelProductDisplayStatusType": "ON" if ss.get("display_on_register") else "SUSPENSION",
        },
    }


def preview(product: AgriProduct, settings: dict, listing: AgriListing | None) -> dict:
    """마켓에 아무것도 보내지 않고, 점검 결과와 보낼 내용을 미리 보여준다."""
    problems = collect_problems(product, settings)
    rows = option_prices(product, settings)
    payload = None
    if not problems:
        placeholder = "(등록할 때 네이버에 올린 주소로 바뀝니다)"
        payload = build_payload(product, settings, [placeholder])
    return {
        "problems": problems,
        "prices": [{"label": r["label"], "price": r["price"], "profit": r["profit"]} for r in rows],
        "payload": payload,
        "payload_verified": PAYLOAD_VERIFIED,
        "listing": listing_dict(listing),
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


async def register_product(
    session: Session, product: AgriProduct, settings: dict, client: BaseSmartstoreClient
) -> AgriListing:
    problems = collect_problems(product, settings)
    if problems:
        raise SmartstoreProblems(problems)

    existing = get_listing(session, product.product_id)
    # 같은 방식(가짜/진짜)으로 이미 올렸으면 막는다. 가짜로 올린 기록 위에 진짜로 올리는 것은 허용한다.
    if existing and existing.is_mock == client.is_mock:
        raise AlreadyListed("이미 스마트스토어에 등록된 상품입니다.")

    image_url = await client.upload_image(product.image_url)
    payload = build_payload(product, settings, [image_url])
    result = await client.register_product(payload)

    listing = existing or AgriListing(product_id=product.product_id, market=MARKET)
    listing.market_product_no = result.origin_product_no
    listing.channel_product_no = result.channel_product_no
    listing.status = "registered"
    listing.is_mock = client.is_mock
    listing.registered_prices = {r["label"]: r["price"] for r in option_prices(product, settings)}
    session.add(listing)
    # 진짜로, 전시를 켠 채 올렸을 때만 '판매중'으로 바꾼다 (가짜 등록이나 전시 중지 상태는 아직 파는 게 아니다).
    if not client.is_mock and settings["smartstore"].get("display_on_register") and product.status == "ready":
        product.status = "selling"
    session.commit()
    session.refresh(listing)
    return listing
