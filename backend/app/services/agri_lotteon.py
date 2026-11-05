"""농산물 상품을 롯데온에 등록하기 위한 점검, 요청 내용 만들기, 등록.

항목 이름은 롯데온 API 센터 설명서(`agri/롯데온_설명서/01_상품등록.md`)를 그대로 썼다.
옵션마다 단품(itmLst)으로 보내고, 단품마다 판매가를 따로 적는다 (스마트스토어와 달리 기본가+추가금 계산이 없다).

주의 (미검증): 실제 키로 호출해 본 적이 없다. 특히 상품정보고시 항목코드(농수축산물 20번)는 설명서 본문에 없어서
설정 화면에 직접 넣어야 하고, 원산지코드·배송가능지역코드도 입점 후 공통코드 조회로 확인해야 한다.
"""

from __future__ import annotations

import html

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.markets.lotteon import BaseLotteonClient, LotteonError
from app.models.agri import AgriListing, AgriProduct
from app.services import agri_pricing as pricing
from app.services.agri_smartstore import AlreadyListed, SmartstoreProblems, option_prices

MARKET = "lotteon"
PAYLOAD_VERIFIED = False
PRODUCT_NAME_MAX_LENGTH = 150
# 일반상품(GNRL)의 발송예정일수 상한 (설명서)
MAX_SEND_DAYS = 3
# 판매종료일: 사실상 계속 판매 (미검증 값)
SELL_END = "20991231235959"

NOTICE_LABELS = {
    "name": "품목·명칭",
    "weight": "중량·수량",
    "producer": "생산자",
    "origin": "원산지",
    "packing": "포장일·생산연도",
    "storage": "보관방법",
    "law": "유전자변형·지리적 표시",
    "livestock": "축산물 표시",
    "import": "수입 농수축산물 문구",
    "phone": "소비자상담 전화번호",
    "composition": "상품구성",
    "caution": "소비자 안전 주의사항",
}


class LotteonProblems(SmartstoreProblems):
    """등록 전에 고쳐야 할 것이 있을 때 (스마트스토어와 같은 모양)."""


def is_domestic(product: AgriProduct) -> bool:
    origin = (product.origin or "").strip()
    return origin.startswith("국산") or origin.startswith("국내")


def storage_code(product: AgriProduct) -> str:
    text = product.storage or ""
    if "냉동" in text:
        return "FRZN"
    if "냉장" in text:
        return "RFRG"
    return "NONE"


def notice_values(product: AgriProduct, settings: dict, rows: list[dict]) -> dict[str, str]:
    labels = ", ".join(r["label"] for r in rows)
    hours = settings.get("claim_hours") or 24
    return {
        "name": product.name,
        "weight": labels or "상세페이지 참고",
        "producer": product.producer or "",
        "origin": product.origin or "",
        "packing": product.packing_info or "주문 후 산지에서 포장하여 발송",
        "law": "해당 없음" if product.category != "rice" else "양곡관리법에 따른 양곡 표시사항은 상세페이지를 참고하세요",
        "livestock": "해당 없음",
        "import": "해당 없음" if is_domestic(product) else "수입식품안전관리 특별법에 따른 수입신고를 필함",
        "composition": labels or "상세페이지 참고",
        "storage": product.storage or "상세페이지 참고",
        "caution": f"신선식품 특성상 받으신 뒤 {hours}시간 안에 상태를 확인해 주시고, 안내된 보관방법대로 보관해 주세요.",
        "phone": settings.get("cs_phone", ""),
    }


def collect_problems(product: AgriProduct, settings: dict) -> list[str]:
    """등록 전에 막아야 하는 문제. 비어 있으면 등록해도 된다."""
    lo = settings.get("lotteon", {})
    problems: list[str] = []
    where = "설정 탭의 '롯데온 등록 설정'"

    if product.status == "stopped":
        problems.append("판매 중지로 표시된 상품입니다. 상태를 '판매 준비'로 바꿔 주세요.")
    if len(product.name) > PRODUCT_NAME_MAX_LENGTH:
        problems.append(f"상품명이 {PRODUCT_NAME_MAX_LENGTH}자를 넘습니다. 줄여 주세요.")
    if not product.origin:
        problems.append("원산지를 입력해 주세요. 농산물은 원산지 표시가 법적 의무입니다.")
    elif not is_domestic(product) and not lo.get("oplc_import"):
        problems.append(f"수입 농산물입니다. 롯데온 수입산 원산지코드를 {where}에 넣어 주세요.")
    if not product.producer:
        problems.append("생산자를 입력해 주세요. 상품정보고시에 들어가는 항목입니다.")
    if not product.image_url:
        problems.append("대표 사진 주소를 입력해 주세요. (롯데온은 인터넷에 공개된 사진 주소가 필요합니다)")
    if not settings.get("cs_phone"):
        problems.append("고객센터 전화번호가 비어 있습니다. 설정 탭에서 넣어 주세요.")
    if product.category == "rice":
        for label, value in (("품종", product.variety), ("생산연도", product.harvest_year), ("등급", product.grade)):
            if not value:
                problems.append(f"쌀은 양곡 표시사항이 필요합니다. {label}을(를) 입력해 주세요.")

    if not lo.get("tr_no"):
        problems.append(f"롯데온 거래처번호가 비어 있습니다. {where}에 넣어 주세요. (연결 확인을 누르면 알 수 있습니다)")
    if not (lo.get("standard_category_ids") or {}).get(product.category):
        problems.append(f"롯데온 표준카테고리 번호가 비어 있습니다. {where}에 넣어 주세요.")
    if not (lo.get("display_category_ids") or {}).get(product.category):
        problems.append(f"롯데온 전시카테고리 번호가 비어 있습니다. {where}에 넣어 주세요.")
    for key, label in (
        ("owhp_no", "출고지번호"),
        ("rtrp_no", "회수(반품)지번호"),
        ("dv_cst_pol_no", "배송비정책번호"),
        ("dv_rgspr_grp_cd", "배송가능지역코드"),
    ):
        if not lo.get(key):
            problems.append(f"롯데온 {label}가 비어 있습니다. {where}에 넣어 주세요.")
    if not any((lo.get("notice_codes") or {}).values()):
        problems.append(f"롯데온 상품정보고시(농수축산물) 항목코드가 비어 있습니다. {where}에 넣어 주세요.")
    if int(lo.get("send_days", 2)) > MAX_SEND_DAYS:
        problems.append(f"발송예정일수는 일반상품 최대 {MAX_SEND_DAYS}일입니다.")

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
        '<div style="max-width:860px;margin:0 auto;line-height:1.7;font-size:15px">'
        f"<h2>{esc(product.name)}</h2>"
        f"<ul>{options}</ul>"
        f'<pre style="white-space:pre-wrap;font-family:inherit">{notice}</pre>'
        "</div>"
    )


def epd_no(product: AgriProduct) -> str:
    return f"agri-{product.product_id}"


def eitm_no(option) -> str:
    return f"agri-{option.option_id}"


def build_payload(product: AgriProduct, settings: dict) -> dict:
    """롯데온 상품 등록 요청 한 건 (spdLst 안에 들어가는 내용)."""
    lo = settings["lotteon"]
    rows = option_prices(product, settings, MARKET)
    stock = int(lo.get("stock_quantity", 999))
    has_options = len(rows) > 1
    values = notice_values(product, settings, rows)
    notice = [
        {"pdArtlCd": code, "pdArtlCnts": values[key]}
        for key, code in (lo.get("notice_codes") or {}).items()
        if code and key in values
    ]
    hours = settings.get("claim_hours") or 24

    items = []
    for index, row in enumerate(rows, start=1):
        item: dict = {
            "eitmNo": eitm_no(row["option"]),
            "rprtSitmYn": "Y" if index == 1 else "N",
            "dpYn": "Y",
            "sortSeq": index,
            "itmImgLst": [
                {
                    "epsrTypCd": "IMG",
                    "epsrTypDtlCd": "IMG_SQRE",
                    "origImgFileNm": product.image_url,
                    "rprtImgYn": "Y",
                }
            ],
            "slPrc": row["price"],
            "stkQty": stock,
        }
        if has_options:
            item["itmOptLst"] = [{"optNm": "옵션", "optVal": row["label"]}]
        items.append(item)

    payload: dict = {
        "trGrpCd": lo.get("tr_grp_cd", "SR"),
        "trNo": lo["tr_no"],
        "scatNo": lo["standard_category_ids"][product.category],
        "dcatLst": [{"mallCd": "LTON", "lfDcatNo": lo["display_category_ids"][product.category]}],
        "epdNo": epd_no(product),
        "slTypCd": "GNRL",
        "pdTypCd": "GNRL_GNRL",
        "spdNm": product.name,
        "mfcrNm": product.producer or "",
        "oplcCd": lo["oplc_domestic"] if is_domestic(product) else lo["oplc_import"],
        "tdfDvsCd": "02",  # 면세 (가공하지 않은 농산물)
        "slStrtDttm": _now_stamp(),
        "slEndDttm": SELL_END,
        "ctrtTypCd": "A",
        "stkMgtYn": "Y",
        "sitmYn": "Y" if has_options else "N",
        "pdStatCd": "NEW",
        # 처음에는 전시하지 않고 올린다. 판매자센터에서 내용을 확인한 뒤 전시한다 (스마트스토어와 같은 안전장치).
        "dpYn": "Y" if lo.get("display_on_register") else "N",
        "scKwdLst": [product.name][:5],
        "pdItmsInfo": {"pdItmsCd": "20", "pdItmsArtlLst": notice},  # 20 = 농수축산물
        "purPsbQtyInfo": {"itmByMinPurYn": "N", "itmByMaxPurPsbQtyYn": "N"},
        "ageLmtCd": "0",
        "prstPsbYn": "N",
        "prstPckPsbYn": "N",
        "prstMsgPsbYn": "N",
        "prcCmprEpsrYn": "Y",
        "epnLst": [
            {"pdEpnTypCd": "DSCRP", "cnts": _detail_html(product, settings, rows)},
            {
                "pdEpnTypCd": "PRCTN",
                "cnts": (
                    "신선식품 특성상 단순 변심 교환·반품은 어렵습니다. "
                    f"상품에 문제가 있으면 받으신 뒤 {hours}시간 안에 사진과 함께 문의해 주세요. 문의: {settings.get('cs_phone', '')}"
                ),
            },
        ],
        "cnclPsbYn": "Y",
        "dmstOvsDvDvsCd": "DMST",
        "pstkYn": "N",
        "dvProcTypCd": "LO_ENTP",  # 업체배송
        "dvPdTypCd": "GNRL",
        "sndBgtNday": int(lo.get("send_days", 2)),
        "sndBgtDdInfo": {"nldySndCloseTm": "1400", "satSndPsbYn": "N"},
        "dvRgsprGrpCd": lo["dv_rgspr_grp_cd"],
        "dvMnsCd": "DPCL",
        "owhpNo": lo["owhp_no"],
        "hdcCd": lo.get("hdc_cd", "0002"),
        "dvCstPolNo": lo["dv_cst_pol_no"],
        "cmbnDvPsbYn": "Y",
        "saveTypCd": storage_code(product),
        "rtngPsbYn": "Y",
        "xchgPsbYn": "Y",
        "rtngHdcCd": lo.get("rtng_hdc_cd", "0002"),
        "rtngRtrvPsbYn": "Y",
        "rtrpNo": lo["rtrp_no"],
        "itmLst": items,
    }
    if lo.get("adtn_dv_cst_pol_no"):
        payload["adtnDvCstPolNo"] = lo["adtn_dv_cst_pol_no"]
    if has_options:
        payload["optSrtLst"] = [
            {
                "optSeq": 1,
                "optNm": "옵션",
                "optValSrtLst": [{"optValSeq": i, "optVal": r["label"]} for i, r in enumerate(rows, start=1)],
            }
        ]
    return payload


def _now_stamp() -> str:
    from app.integrations.markets.lotteon import kst_stamp

    return kst_stamp()


def preview(product: AgriProduct, settings: dict, listing: AgriListing | None) -> dict:
    """롯데온에 아무것도 보내지 않고, 점검 결과와 보낼 내용을 미리 보여준다."""
    problems = collect_problems(product, settings)
    rows = option_prices(product, settings, MARKET)
    return {
        "problems": problems,
        "prices": [{"label": r["label"], "price": r["price"], "profit": r["profit"]} for r in rows],
        "payload": None if problems else build_payload(product, settings),
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


async def register_product(session: Session, product: AgriProduct, settings: dict, client: BaseLotteonClient) -> AgriListing:
    problems = collect_problems(product, settings)
    if problems:
        raise LotteonProblems(problems)
    existing = get_listing(session, product.product_id)
    # 같은 방식(가짜/진짜)으로 이미 올렸으면 막는다. 가짜로 올린 기록 위에 진짜로 올리는 것은 허용한다.
    if existing and existing.is_mock == client.is_mock:
        raise AlreadyListed("이미 롯데온에 등록된 상품입니다.")

    [result] = await client.register_products([build_payload(product, settings)])
    if not result.ok or not result.spd_no:
        raise LotteonError(f"롯데온이 상품 등록을 거절했습니다: {result.message or '이유를 알 수 없음'}")

    listing = existing or AgriListing(product_id=product.product_id, market=MARKET)
    listing.market_product_no = result.spd_no
    listing.channel_product_no = None
    listing.status = "registered"
    listing.is_mock = client.is_mock
    listing.registered_prices = {r["label"]: r["price"] for r in option_prices(product, settings, MARKET)}
    session.add(listing)
    if not client.is_mock and settings["lotteon"].get("display_on_register") and product.status == "ready":
        product.status = "selling"
    session.commit()
    session.refresh(listing)
    return listing
