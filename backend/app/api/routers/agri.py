"""농산물 판매 관리 화면(/agri.html)용 API.

공급사·상품·옵션 관리, 가격 계산, 주문 → 발주서 → 송장 흐름을 다룬다.
엑셀 파일 읽기·만들기는 화면(브라우저)에서 하고, 여기서는 표 형태의 데이터만 주고받는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.core.config import get_settings as get_app_settings
from app.core.database import get_db_sync
from app.integrations.markets.lotteon import LotteonError, get_lotteon_client
from app.integrations.markets.naver_commerce import NaverCommerceError, get_smartstore_client
from app.models.agri import AgriListing, AgriOption, AgriOrder, AgriProduct, AgriSetting, AgriSupplier
from app.services import agri_pricing as pricing
from app.services import agri_order_actions as order_actions
from app.services import agri_lotteon as lotteon_products
from app.services import agri_lotteon_orders as lotteon_orders
from app.services import agri_smartstore as smartstore
from app.services import agri_smartstore_orders as orders_sync

router = APIRouter(prefix="/api/agri", tags=["agri"])

MarketCode = Literal["smartstore", "lotteon", "coupang", "toss", "11st", "gmarket"]
CategoryCode = Literal["fruit", "vegetable", "rice", "grain", "etc"]
ProductStatus = Literal["ready", "selling", "soldout", "stopped"]
OrderMethod = Literal["excel", "auto", "kakao", "etc"]
OrderStatus = Literal["new", "ordered", "invoiced", "shipped", "cancelled"]


# ---------------------------------------------------------------- 설정


def load_settings(session: Session) -> dict:
    row = session.get(AgriSetting, pricing.SETTINGS_KEY)
    return pricing.merge_settings(row.value if row else None)


@router.get("/meta")
def meta() -> dict:
    """화면에서 쓰는 이름표 (마켓, 품목, 상태)."""
    return {
        "markets": pricing.MARKETS,
        "categories": pricing.CATEGORIES,
        "order_statuses": pricing.ORDER_STATUSES,
        "product_statuses": pricing.PRODUCT_STATUSES,
        "order_methods": pricing.ORDER_METHODS,
        "smartstore_mode": "mock" if get_app_settings().use_mock_naver_commerce else "real",
        "lotteon_mode": "mock" if get_app_settings().use_mock_lotteon else "real",
    }


@router.get("/settings")
def get_settings(session: Session = Depends(get_db_sync)) -> dict:
    return load_settings(session)


class SmartstoreSettingsIn(BaseModel):
    leaf_category_ids: dict[str, str] = {}
    delivery_company: str = Field("CJGLS", min_length=1)
    return_fee: int = Field(3000, ge=0)
    exchange_fee: int = Field(6000, ge=0)
    stock_quantity: int = Field(999, ge=0)
    origin_area_code: str = Field("00", min_length=1)
    display_on_register: bool = False


class LotteonSettingsIn(BaseModel):
    tr_grp_cd: str = "SR"
    tr_no: str = ""
    standard_category_ids: dict[str, str] = {}
    display_category_ids: dict[str, str] = {}
    owhp_no: str = ""
    rtrp_no: str = ""
    dv_cst_pol_no: str = ""
    adtn_dv_cst_pol_no: str = ""
    dv_rgspr_grp_cd: str = ""
    hdc_cd: str = "0002"
    rtng_hdc_cd: str = "0002"
    oplc_domestic: str = "KR"
    oplc_import: str = ""
    send_days: int = Field(2, ge=0, le=3)
    stock_quantity: int = Field(999, ge=0)
    notice_codes: dict[str, str] = {}
    display_on_register: bool = False


class SettingsIn(BaseModel):
    fee_rates: dict[str, float]
    min_profit: dict[str, int]
    price_unit: int = Field(100, ge=1)
    cs_phone: str = ""
    claim_hours: int = Field(24, ge=1)
    # 보내지 않으면 저장돼 있던 마켓별 등록 설정을 그대로 둔다.
    smartstore: SmartstoreSettingsIn | None = None
    lotteon: LotteonSettingsIn | None = None


@router.put("/settings")
def put_settings(payload: SettingsIn, session: Session = Depends(get_db_sync)) -> dict:
    for market, rate in payload.fee_rates.items():
        if market not in pricing.MARKETS or not 0 <= rate < 100:
            raise HTTPException(400, f"수수료율을 확인해 주세요: {market} {rate}")
    for category, amount in payload.min_profit.items():
        if category not in pricing.CATEGORIES or amount < 0:
            raise HTTPException(400, f"최소 이익을 확인해 주세요: {category} {amount}")
    if payload.smartstore:
        for category, number in payload.smartstore.leaf_category_ids.items():
            if category not in pricing.CATEGORIES or (number and not number.strip().isdigit()):
                raise HTTPException(400, f"스마트스토어 카테고리 번호는 숫자만 넣어 주세요: {category} {number}")
    if payload.lotteon:
        for field in ("standard_category_ids", "display_category_ids"):
            for category, number in getattr(payload.lotteon, field).items():
                if category not in pricing.CATEGORIES or (number and not number.strip().isalnum()):
                    raise HTTPException(400, f"롯데온 카테고리 번호는 글자와 숫자만 넣어 주세요: {category} {number}")
        for key, code in payload.lotteon.notice_codes.items():
            if code and not code.strip().isalnum():
                raise HTTPException(400, f"롯데온 상품정보고시 항목코드는 글자와 숫자만 넣어 주세요: {key} {code}")
    row = session.get(AgriSetting, pricing.SETTINGS_KEY)
    if row is None:
        row = AgriSetting(key=pricing.SETTINGS_KEY, value={})
        session.add(row)
    data = payload.model_dump(exclude={"smartstore", "lotteon"})
    for market in ("smartstore", "lotteon"):
        section = getattr(payload, market)
        if section is not None:
            data[market] = section.model_dump()
        elif row.value.get(market):
            data[market] = row.value[market]
    row.value = data
    session.commit()
    return load_settings(session)


class PricePreviewIn(BaseModel):
    supply_price: int = Field(ge=0)
    shipping_fee: int = Field(0, ge=0)
    category: CategoryCode = "fruit"
    sale_price: int | None = Field(None, ge=0)


@router.post("/price-preview")
def price_preview(payload: PricePreviewIn, session: Session = Depends(get_db_sync)) -> dict:
    settings = load_settings(session)
    return {
        "cost": pricing.unit_cost(payload.supply_price, payload.shipping_fee),
        "min_profit": settings["min_profit"].get(payload.category, 0),
        "rows": pricing.price_table(
            payload.supply_price, payload.shipping_fee, payload.category, settings, payload.sale_price
        ),
    }


# ---------------------------------------------------------------- 공급사


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    site_url: str | None = None
    contact: str | None = None
    order_method: OrderMethod = "excel"
    default_shipping_fee: int = Field(0, ge=0)
    remote_area_fee: int = Field(0, ge=0)
    cutoff_time: str | None = None
    memo: str | None = None


def _supplier_dict(s: AgriSupplier, product_count: int = 0) -> dict:
    return {
        "supplier_id": s.supplier_id,
        "name": s.name,
        "site_url": s.site_url,
        "contact": s.contact,
        "order_method": s.order_method,
        "default_shipping_fee": s.default_shipping_fee,
        "remote_area_fee": s.remote_area_fee,
        "cutoff_time": s.cutoff_time,
        "memo": s.memo,
        "product_count": product_count,
    }


@router.get("/suppliers")
def list_suppliers(session: Session = Depends(get_db_sync)) -> list[dict]:
    counts = dict(
        session.execute(select(AgriProduct.supplier_id, func.count()).group_by(AgriProduct.supplier_id)).all()
    )
    rows = session.execute(select(AgriSupplier).order_by(AgriSupplier.name)).scalars().all()
    return [_supplier_dict(s, counts.get(s.supplier_id, 0)) for s in rows]


def _commit_or_400(session: Session, message: str) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(400, message) from exc


@router.post("/suppliers")
def create_supplier(payload: SupplierIn, session: Session = Depends(get_db_sync)) -> dict:
    supplier = AgriSupplier(**payload.model_dump())
    session.add(supplier)
    _commit_or_400(session, "같은 이름의 공급사가 이미 있습니다.")
    return _supplier_dict(supplier)


@router.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, payload: SupplierIn, session: Session = Depends(get_db_sync)) -> dict:
    supplier = session.get(AgriSupplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "공급사를 찾을 수 없습니다.")
    for key, value in payload.model_dump().items():
        setattr(supplier, key, value)
    _commit_or_400(session, "같은 이름의 공급사가 이미 있습니다.")
    return _supplier_dict(supplier)


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, session: Session = Depends(get_db_sync)) -> dict:
    supplier = session.get(AgriSupplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "공급사를 찾을 수 없습니다.")
    has_products = session.execute(
        select(func.count()).select_from(AgriProduct).where(AgriProduct.supplier_id == supplier_id)
    ).scalar_one()
    if has_products:
        raise HTTPException(400, "이 공급사의 상품이 남아 있어 지울 수 없습니다. 상품을 먼저 지워 주세요.")
    session.delete(supplier)
    session.commit()
    return {"deleted": supplier_id}


# ---------------------------------------------------------------- 상품


class OptionIn(BaseModel):
    option_id: int | None = None
    label: str = Field(min_length=1, max_length=256)
    weight: str | None = None
    count: str | None = None
    grade: str | None = None
    supplier_code: str | None = None
    supply_price: int = Field(ge=0)
    shipping_fee: int = Field(0, ge=0)
    sale_price: int | None = Field(None, ge=0)
    is_soldout: bool = False


class ProductIn(BaseModel):
    supplier_id: int
    name: str = Field(min_length=1, max_length=256)
    category: CategoryCode = "fruit"
    origin: str | None = None
    producer: str | None = None
    storage: str | None = None
    packing_info: str | None = None
    variety: str | None = None
    harvest_year: str | None = None
    grade: str | None = None
    image_url: str | None = None
    status: ProductStatus = "ready"
    memo: str | None = None
    options: list[OptionIn] = Field(min_length=1)


def _option_dict(o: AgriOption, category: str, settings: dict) -> dict:
    return {
        "option_id": o.option_id,
        "label": o.label,
        "weight": o.weight,
        "count": o.count,
        "grade": o.grade,
        "supplier_code": o.supplier_code,
        "supply_price": o.supply_price,
        "shipping_fee": o.shipping_fee,
        "sale_price": o.sale_price,
        "is_soldout": o.is_soldout,
        "cost": pricing.unit_cost(o.supply_price, o.shipping_fee),
        "prices": pricing.price_table(o.supply_price, o.shipping_fee, category, settings, o.sale_price),
    }


LISTING_MARKETS = (smartstore.MARKET, lotteon_products.MARKET)


def _listing_map(session: Session, product_ids: list[int] | None = None) -> dict[int, dict[str, AgriListing]]:
    """상품마다 마켓별 등록 기록 {상품번호: {마켓: 기록}}."""
    query = select(AgriListing).where(AgriListing.market.in_(LISTING_MARKETS))
    if product_ids is not None:
        query = query.where(AgriListing.product_id.in_(product_ids))
    result: dict[int, dict[str, AgriListing]] = {}
    for listing in session.execute(query).scalars():
        result.setdefault(listing.product_id, {})[listing.market] = listing
    return result


def _product_dict(p: AgriProduct, settings: dict, listings: dict[int, dict[str, AgriListing]] | None = None) -> dict:
    if listings is None:
        session = object_session(p)
        listings = _listing_map(session, [p.product_id]) if session else {}
    mine = listings.get(p.product_id, {})
    return {
        "listings": {market: smartstore.listing_dict(mine.get(market)) for market in LISTING_MARKETS},
        "product_id": p.product_id,
        "supplier_id": p.supplier_id,
        "supplier_name": p.supplier.name if p.supplier else None,
        "name": p.name,
        "category": p.category,
        "origin": p.origin,
        "producer": p.producer,
        "storage": p.storage,
        "packing_info": p.packing_info,
        "variety": p.variety,
        "harvest_year": p.harvest_year,
        "grade": p.grade,
        "image_url": p.image_url,
        "status": p.status,
        "memo": p.memo,
        "options": [_option_dict(o, p.category, settings) for o in p.options],
    }


@router.get("/products")
def list_products(session: Session = Depends(get_db_sync)) -> list[dict]:
    settings = load_settings(session)
    rows = session.execute(select(AgriProduct).order_by(AgriProduct.product_id.desc())).unique().scalars().all()
    listings = _listing_map(session)
    return [_product_dict(p, settings, listings) for p in rows]


def _apply_product(session: Session, product: AgriProduct, payload: ProductIn) -> None:
    if session.get(AgriSupplier, payload.supplier_id) is None:
        raise HTTPException(400, "공급사를 먼저 골라 주세요.")
    data = payload.model_dump(exclude={"options"})
    for key, value in data.items():
        setattr(product, key, value)

    # 이미 주문에 쓰인 옵션은 지우면 주문 기록이 깨지므로, 기존 옵션은 option_id로 찾아 고친다.
    existing = {o.option_id: o for o in product.options}
    kept: list[AgriOption] = []
    for index, item in enumerate(payload.options):
        option = existing.pop(item.option_id, None) if item.option_id else None
        if option is None:
            option = AgriOption()
        for key, value in item.model_dump(exclude={"option_id"}).items():
            setattr(option, key, value)
        option.sort_order = index
        kept.append(option)
    for removed in existing.values():
        used = session.execute(
            select(func.count()).select_from(AgriOrder).where(AgriOrder.option_id == removed.option_id)
        ).scalar_one()
        if used:
            raise HTTPException(
                400, f"'{removed.label}' 옵션은 주문 기록이 있어 지울 수 없습니다. 대신 품절로 표시해 주세요."
            )
    product.options = kept


@router.post("/products")
def create_product(payload: ProductIn, session: Session = Depends(get_db_sync)) -> dict:
    product = AgriProduct()
    _apply_product(session, product, payload)
    session.add(product)
    session.commit()
    session.refresh(product)
    return _product_dict(product, load_settings(session))


@router.put("/products/{product_id}")
def update_product(product_id: int, payload: ProductIn, session: Session = Depends(get_db_sync)) -> dict:
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    _apply_product(session, product, payload)
    session.commit()
    session.refresh(product)
    return _product_dict(product, load_settings(session))


@router.delete("/products/{product_id}")
def delete_product(product_id: int, session: Session = Depends(get_db_sync)) -> dict:
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    for market, listing in _listing_map(session, [product_id]).get(product_id, {}).items():
        if not listing.is_mock:
            label = pricing.MARKETS.get(market, market)
            raise HTTPException(400, f"{label}에 등록된 상품입니다. {label}에서 상품을 내린 뒤 지울 수 있습니다.")
    option_ids = [o.option_id for o in product.options]
    if option_ids:
        used = session.execute(
            select(func.count()).select_from(AgriOrder).where(AgriOrder.option_id.in_(option_ids))
        ).scalar_one()
        if used:
            raise HTTPException(400, "주문 기록이 있는 상품은 지울 수 없습니다. 대신 '판매 중지'로 바꿔 주세요.")
    session.delete(product)
    session.commit()
    return {"deleted": product_id}


@router.get("/products/{product_id}/notice")
def product_notice(product_id: int, session: Session = Depends(get_db_sync)) -> dict:
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    return {"text": pricing.product_notice_text(product, load_settings(session))}


@router.get("/products/{product_id}/smartstore/preview")
def smartstore_preview(product_id: int, session: Session = Depends(get_db_sync)) -> dict:
    """스마트스토어에 아무것도 보내지 않고, 등록 전 점검 결과와 보낼 내용을 보여준다."""
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    result = smartstore.preview(product, load_settings(session), smartstore.get_listing(session, product_id))
    result["mode"] = "mock" if get_smartstore_client().is_mock else "real"
    return result


@router.post("/products/{product_id}/smartstore/register")
async def smartstore_register(product_id: int, session: Session = Depends(get_db_sync)) -> dict:
    """점검을 통과한 상품을 스마트스토어에 등록한다 (기본은 Mock이라 실제 스토어에는 올라가지 않는다)."""
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    client = get_smartstore_client()
    try:
        listing = await smartstore.register_product(session, product, load_settings(session), client)
    except smartstore.SmartstoreProblems as exc:
        raise HTTPException(400, "등록 전에 고칠 것이 있습니다: " + " / ".join(exc.problems)) from exc
    except smartstore.AlreadyListed as exc:
        raise HTTPException(400, str(exc)) from exc
    except NaverCommerceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"listing": smartstore.listing_dict(listing), "mode": "mock" if client.is_mock else "real"}


class ImportRow(BaseModel):
    product_name: str = Field(min_length=1)
    category: CategoryCode = "fruit"
    origin: str | None = None
    producer: str | None = None
    storage: str | None = None
    option_label: str = Field(min_length=1)
    weight: str | None = None
    count: str | None = None
    grade: str | None = None
    supplier_code: str | None = None
    supply_price: int = Field(ge=0)
    shipping_fee: int | None = Field(None, ge=0)


class ImportIn(BaseModel):
    supplier_id: int
    rows: list[ImportRow] = Field(min_length=1)


@router.post("/products/import")
def import_products(payload: ImportIn, session: Session = Depends(get_db_sync)) -> dict:
    """엑셀에서 읽은 줄을 상품명별로 묶어 등록한다. 같은 공급사에 같은 이름의 상품이 있으면 옵션만 추가."""
    supplier = session.get(AgriSupplier, payload.supplier_id)
    if supplier is None:
        raise HTTPException(400, "공급사를 먼저 골라 주세요.")
    existing = {
        p.name: p
        for p in session.execute(select(AgriProduct).where(AgriProduct.supplier_id == supplier.supplier_id))
        .unique()
        .scalars()
        .all()
    }
    created = 0
    options_added = 0
    for row in payload.rows:
        product = existing.get(row.product_name)
        if product is None:
            product = AgriProduct(
                supplier_id=supplier.supplier_id,
                name=row.product_name,
                category=row.category,
                origin=row.origin,
                producer=row.producer,
                storage=row.storage,
            )
            product.options = []
            session.add(product)
            existing[row.product_name] = product
            created += 1
        if any(o.label == row.option_label for o in product.options):
            continue
        product.options.append(
            AgriOption(
                label=row.option_label,
                weight=row.weight,
                count=row.count,
                grade=row.grade,
                supplier_code=row.supplier_code,
                supply_price=row.supply_price,
                shipping_fee=supplier.default_shipping_fee if row.shipping_fee is None else row.shipping_fee,
                sort_order=len(product.options),
            )
        )
        options_added += 1
    session.commit()
    return {"products_created": created, "options_added": options_added}


# ---------------------------------------------------------------- 주문


class OrderIn(BaseModel):
    market: MarketCode
    market_order_no: str = Field(min_length=1, max_length=128)
    option_id: int
    quantity: int = Field(1, ge=1)
    buyer_name: str | None = None
    receiver_name: str = Field(min_length=1)
    receiver_phone: str = Field(min_length=1)
    zipcode: str | None = None
    address: str = Field(min_length=1)
    delivery_memo: str | None = None
    sale_amount: int = Field(ge=0)
    memo: str | None = None


def _order_dict(o: AgriOrder, settings: dict | None = None) -> dict:
    option = o.option
    product = option.product if option else None
    return {
        "order_id": o.order_id,
        "market": o.market,
        "market_order_no": o.market_order_no,
        "option_id": o.option_id,
        "product_name": product.name if product else None,
        "option_label": option.label if option else None,
        "supplier_code": option.supplier_code if option else None,
        "supplier_id": product.supplier_id if product else None,
        "supplier_name": product.supplier.name if product and product.supplier else None,
        "quantity": o.quantity,
        "buyer_name": o.buyer_name,
        "receiver_name": o.receiver_name,
        "receiver_phone": o.receiver_phone,
        "zipcode": o.zipcode,
        "address": o.address,
        "delivery_memo": o.delivery_memo,
        "sale_amount": o.sale_amount,
        "unit_cost": o.unit_cost,
        "fee_rate_percent": float(o.fee_rate_percent),
        "profit": pricing.order_profit(o.sale_amount, o.unit_cost, o.quantity, float(o.fee_rate_percent)),
        "status": o.status,
        "courier": o.courier,
        "tracking_no": o.tracking_no,
        "ordered_at": o.ordered_at.isoformat() if o.ordered_at else None,
        "purchase_ordered_at": o.purchase_ordered_at.isoformat() if o.purchase_ordered_at else None,
        "memo": o.memo,
        "claim_type": o.claim_type,
        "claim_status": o.claim_status,
        "delayed_until": o.delayed_until.isoformat() if o.delayed_until else None,
        "delay_reason": o.delay_reason,
        "overdue": order_actions.is_overdue(o, settings) if settings is not None else False,
        "is_practice": o.market_order_no.startswith(orders_sync.MOCK_PREFIX),
    }


@router.get("/orders")
def list_orders(status: OrderStatus | None = None, session: Session = Depends(get_db_sync)) -> list[dict]:
    query = select(AgriOrder).order_by(AgriOrder.order_id.desc())
    if status:
        query = query.where(AgriOrder.status == status)
    settings = load_settings(session)
    return [_order_dict(o, settings) for o in session.execute(query).unique().scalars().all()]


@router.post("/orders")
def create_order(payload: OrderIn, session: Session = Depends(get_db_sync)) -> dict:
    option = session.get(AgriOption, payload.option_id)
    if option is None:
        raise HTTPException(400, "상품 옵션을 골라 주세요.")
    settings = load_settings(session)
    order = AgriOrder(
        **payload.model_dump(),
        unit_cost=pricing.unit_cost(option.supply_price, option.shipping_fee),
        fee_rate_percent=str(settings["fee_rates"].get(payload.market, 0)),
    )
    session.add(order)
    _commit_or_400(session, "같은 마켓에 같은 주문번호가 이미 있습니다.")
    session.refresh(order)
    return _order_dict(order)


class OrderStatusIn(BaseModel):
    status: OrderStatus
    memo: str | None = None


@router.put("/orders/{order_id}/status")
def set_order_status(order_id: int, payload: OrderStatusIn, session: Session = Depends(get_db_sync)) -> dict:
    order = session.get(AgriOrder, order_id)
    if order is None:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    order.status = payload.status
    if payload.memo is not None:
        order.memo = payload.memo
    session.commit()
    session.refresh(order)
    return _order_dict(order)


class OrderIdsIn(BaseModel):
    order_ids: list[int] = Field(min_length=1)


@router.post("/orders/purchase-sheet")
def purchase_sheet(payload: OrderIdsIn, session: Session = Depends(get_db_sync)) -> dict:
    """공급사 발주서용 줄을 공급사별로 묶어 돌려주고, 발주 대기 주문은 '발주 완료'로 바꾼다."""
    orders = session.execute(select(AgriOrder).where(AgriOrder.order_id.in_(payload.order_ids))).unique().scalars().all()
    if not orders:
        raise HTTPException(400, "고른 주문이 없습니다.")
    cancelled = [o.market_order_no for o in orders if o.status == "cancelled"]
    if cancelled:
        raise HTTPException(400, f"취소된 주문은 발주할 수 없습니다: {', '.join(cancelled)}")
    now = datetime.now(timezone.utc)
    for order in orders:
        if order.status == "new":
            order.status = "ordered"
            order.purchase_ordered_at = now
    session.commit()

    groups: dict[str, list[dict]] = {}
    for order in sorted(orders, key=lambda o: o.order_id):
        data = _order_dict(order)
        groups.setdefault(data["supplier_name"] or "공급사 없음", []).append(data)
    return {"groups": groups}


class TrackingRow(BaseModel):
    order_id: int
    courier: str = Field(min_length=1)
    tracking_no: str = Field(min_length=1)


class TrackingIn(BaseModel):
    rows: list[TrackingRow] = Field(min_length=1)


@router.post("/orders/tracking")
def save_tracking(payload: TrackingIn, session: Session = Depends(get_db_sync)) -> dict:
    """공급사가 보낸 송장번호를 주문에 넣는다. 마켓에는 아직 입력 전이라 '송장 받음' 상태."""
    updated = 0
    skipped: list[int] = []
    for row in payload.rows:
        order = session.get(AgriOrder, row.order_id)
        if order is None or order.status == "cancelled":
            skipped.append(row.order_id)
            continue
        order.courier = row.courier.strip()
        order.tracking_no = row.tracking_no.strip()
        if order.status in ("new", "ordered"):
            order.status = "invoiced"
        updated += 1
    session.commit()
    return {"updated": updated, "skipped": skipped}


@router.post("/orders/market-shipped")
def mark_market_shipped(payload: OrderIdsIn, session: Session = Depends(get_db_sync)) -> dict:
    """마켓에 송장 입력을 마친 주문을 '마켓 송장 입력 끝'으로 바꾼다."""
    updated = 0
    for order in session.execute(select(AgriOrder).where(AgriOrder.order_id.in_(payload.order_ids))).unique().scalars():
        if order.tracking_no and order.status != "cancelled":
            order.status = "shipped"
            updated += 1
    session.commit()
    return {"updated": updated}


# ---------------------------------------------------------------- 스마트스토어 주문 연동


def _mode(client) -> str:
    return "mock" if client.is_mock else "real"


class SyncIn(BaseModel):
    hours: int = Field(24, ge=1, le=24 * 14)


@router.post("/smartstore/orders/sync")
async def smartstore_sync_orders(payload: SyncIn | None = None, session: Session = Depends(get_db_sync)) -> dict:
    """스마트스토어에서 최근 바뀐 주문(새 주문, 취소·반품 요청)을 가져온다. 마켓에는 아무것도 바꾸지 않는다."""
    client = get_smartstore_client()
    hours = payload.hours if payload else 24
    try:
        return await orders_sync.sync_orders(session, client, load_settings(session), hours)
    except NaverCommerceError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/smartstore/orders/dispatch")
async def smartstore_dispatch_orders(payload: OrderIdsIn, session: Session = Depends(get_db_sync)) -> dict:
    """송장을 받은 주문의 택배사·송장번호를 스마트스토어에 입력(발송 처리)한다."""
    client = get_smartstore_client()
    try:
        result = await orders_sync.dispatch_orders(session, client, payload.order_ids)
    except NaverCommerceError as exc:
        raise HTTPException(502, str(exc)) from exc
    result["mode"] = _mode(client)
    return result


class ClaimApproveIn(BaseModel):
    confirmed: bool = False


@router.post("/orders/{order_id}/smartstore/claim/approve")
async def smartstore_approve_claim(
    order_id: int, payload: ClaimApproveIn | None = None, session: Session = Depends(get_db_sync)
) -> dict:
    """고객의 취소·반품 요청을 승인한다. 이미 공급사에 발주한 주문은 확인(confirmed)을 받아야 한다."""
    order = session.get(AgriOrder, order_id)
    if order is None:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    client = get_smartstore_client()
    try:
        await orders_sync.approve_claim(session, client, order, bool(payload and payload.confirmed))
    except orders_sync.NeedsConfirmation as exc:
        raise HTTPException(409, str(exc)) from exc
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except NaverCommerceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"order": _order_dict(order), "mode": _mode(client)}


class PracticeOrderIn(BaseModel):
    option_id: int
    quantity: int = Field(1, ge=1, le=99)


@router.post("/smartstore/orders/practice")
def smartstore_practice_order(payload: PracticeOrderIn, session: Session = Depends(get_db_sync)) -> dict:
    """연습 모드에서 고객이 주문한 것처럼 가짜 주문을 만든다. [주문 가져오기]를 누르면 들어온다."""
    client = get_smartstore_client()
    try:
        mo = orders_sync.create_practice_order(session, client, load_settings(session), payload.option_id, payload.quantity)
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"product_order_id": mo.product_order_id, "paid_amount": mo.paid_amount}


class PracticeClaimIn(BaseModel):
    kind: Literal["cancel", "return"]


@router.post("/smartstore/orders/practice/{product_order_id}/claim")
def smartstore_practice_claim(product_order_id: str, payload: PracticeClaimIn) -> dict:
    """연습 모드에서 고객이 취소·반품을 요청한 것처럼 만든다 (승인 연습용)."""
    client = get_smartstore_client()
    if not client.is_mock:
        raise HTTPException(400, "연습 모드에서만 쓸 수 있습니다.")
    for mo in client.orders:  # type: ignore[attr-defined]
        if mo.product_order_id == product_order_id:
            mo.claim_type = payload.kind.upper()
            mo.claim_status = f"{payload.kind.upper()}_REQUEST"
            return {"product_order_id": product_order_id}
    raise HTTPException(404, "연습 주문을 찾을 수 없습니다. 서버를 다시 켰다면 연습 주문을 새로 만들어 주세요.")


# ---------------------------------------------------------------- 발송 지연(2-10), 발송 전 취소 처리(2-9)


class DelayIn(BaseModel):
    order_ids: list[int] = Field(min_length=1)
    due_date: str  # 2026-11-09
    reason: Literal["stock", "customer"] = "stock"


@router.post("/orders/delay")
async def delay_orders(payload: DelayIn, session: Session = Depends(get_db_sync)) -> dict:
    """출고가 늦는 주문의 발송 지연을 마켓에 알린다. 가짜 송장 대신 이것을 쓴다 (가짜 송장은 판매 정지 사유)."""
    try:
        result = await order_actions.delay_orders(
            session, payload.order_ids, payload.due_date, payload.reason, get_lotteon_client(), get_smartstore_client()
        )
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (LotteonError, NaverCommerceError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return result


class ResolveIn(BaseModel):
    decision: Literal["blocked", "shipped"]
    courier: str | None = None
    tracking_no: str | None = None


@router.post("/orders/{order_id}/claim/resolve")
async def resolve_claim(order_id: int, payload: ResolveIn, session: Session = Depends(get_db_sync)) -> dict:
    """발송 전 취소 요청 처리: 공급사가 출고를 막았으면 승인(blocked), 이미 출고했으면 송장 저장 + 거부(shipped)."""
    order = session.get(AgriOrder, order_id)
    if order is None:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    try:
        result = await order_actions.resolve_cancel_request(
            session, order, payload.decision, get_lotteon_client(), get_smartstore_client(), payload.courier, payload.tracking_no
        )
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (LotteonError, NaverCommerceError) as exc:
        raise HTTPException(502, str(exc)) from exc
    session.refresh(order)
    return {**result, "order": _order_dict(order, load_settings(session))}


# ---------------------------------------------------------------- 롯데온 (상품 등록, 주문 연동)


@router.get("/lotteon/connection")
async def lotteon_connection() -> dict:
    """롯데온과 연결이 되는지 확인한다 (읽기만 함). 연습 모드에서는 가짜 거래처가 나온다."""
    client = get_lotteon_client()
    try:
        data = await client.identity()
    except LotteonError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"mode": _mode(client), "identity": data}


@router.get("/products/{product_id}/lotteon/preview")
def lotteon_preview(product_id: int, session: Session = Depends(get_db_sync)) -> dict:
    """롯데온에 아무것도 보내지 않고, 등록 전 점검 결과와 보낼 내용을 보여준다."""
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    result = lotteon_products.preview(product, load_settings(session), lotteon_products.get_listing(session, product_id))
    result["mode"] = "mock" if get_app_settings().use_mock_lotteon else "real"
    return result


@router.post("/products/{product_id}/lotteon/register")
async def lotteon_register(product_id: int, session: Session = Depends(get_db_sync)) -> dict:
    """점검을 통과한 상품을 롯데온에 등록한다 (기본은 연습 모드라 실제 롯데온에는 올라가지 않는다)."""
    product = session.get(AgriProduct, product_id)
    if product is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    client = get_lotteon_client()
    try:
        listing = await lotteon_products.register_product(session, product, load_settings(session), client)
    except lotteon_products.LotteonProblems as exc:
        raise HTTPException(400, "등록 전에 고칠 것이 있습니다: " + " / ".join(exc.problems)) from exc
    except lotteon_products.AlreadyListed as exc:
        raise HTTPException(400, str(exc)) from exc
    except LotteonError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"listing": lotteon_products.listing_dict(listing), "mode": _mode(client)}


@router.post("/lotteon/orders/sync")
async def lotteon_sync_orders(payload: SyncIn | None = None, session: Session = Depends(get_db_sync)) -> dict:
    """롯데온의 새 주문, 반품·교환 회수지시, 고객의 취소 요청을 가져온다.
    새 주문은 가져온 뒤 롯데온에 '연동완료'를 알린다 (롯데온 규칙)."""
    client = get_lotteon_client()
    hours = payload.hours if payload else 24
    try:
        return await lotteon_orders.sync_orders(session, client, load_settings(session), hours)
    except LotteonError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/lotteon/orders/dispatch")
async def lotteon_dispatch_orders(payload: OrderIdsIn, session: Session = Depends(get_db_sync)) -> dict:
    """송장을 받은 주문의 택배사·송장번호를 롯데온에 입력(발송완료 통보)한다."""
    client = get_lotteon_client()
    try:
        result = await lotteon_orders.dispatch_orders(session, client, payload.order_ids)
    except LotteonError as exc:
        raise HTTPException(502, str(exc)) from exc
    result["mode"] = _mode(client)
    return result


@router.post("/orders/{order_id}/lotteon/claim/approve")
async def lotteon_approve_claim(
    order_id: int, payload: ClaimApproveIn | None = None, session: Session = Depends(get_db_sync)
) -> dict:
    order = session.get(AgriOrder, order_id)
    if order is None:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    client = get_lotteon_client()
    try:
        await lotteon_orders.approve_cancel(session, client, order, bool(payload and payload.confirmed))
    except orders_sync.NeedsConfirmation as exc:
        raise HTTPException(409, str(exc)) from exc
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LotteonError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"order": _order_dict(order), "mode": _mode(client)}


@router.post("/orders/{order_id}/lotteon/claim/hold")
async def lotteon_hold_claim(order_id: int, session: Session = Depends(get_db_sync)) -> dict:
    """고객의 취소 요청을 거부한다 (이미 발송한 주문 등)."""
    order = session.get(AgriOrder, order_id)
    if order is None:
        raise HTTPException(404, "주문을 찾을 수 없습니다.")
    client = get_lotteon_client()
    try:
        await lotteon_orders.hold_cancel(session, client, order)
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LotteonError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"order": _order_dict(order), "mode": _mode(client)}


@router.post("/lotteon/orders/practice")
def lotteon_practice_order(payload: PracticeOrderIn, session: Session = Depends(get_db_sync)) -> dict:
    """연습 모드에서 고객이 주문한 것처럼 가짜 주문을 만든다. [주문 가져오기]를 누르면 들어온다."""
    client = get_lotteon_client()
    try:
        lo = lotteon_orders.create_practice_order(session, client, load_settings(session), payload.option_id, payload.quantity)
    except orders_sync.OrderActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"product_order_id": lo.key, "paid_amount": lo.amount}


@router.post("/lotteon/orders/practice/{key}/claim")
def lotteon_practice_claim(key: str, payload: PracticeClaimIn) -> dict:
    """연습 모드에서 고객이 취소를 요청한 것처럼 만든다 (승인 연습용)."""
    client = get_lotteon_client()
    if not client.is_mock:
        raise HTTPException(400, "연습 모드에서만 쓸 수 있습니다.")
    if payload.kind != "cancel":
        raise HTTPException(400, "연습용으로는 취소 요청만 만들 수 있습니다.")
    for order in client.orders:  # type: ignore[attr-defined]
        if order.key == key:
            client.add_cancel_request(order)  # type: ignore[attr-defined]
            return {"product_order_id": key}
    raise HTTPException(404, "연습 주문을 찾을 수 없습니다. 서버를 다시 켰다면 연습 주문을 새로 만들어 주세요.")


@router.get("/summary")
def summary(session: Session = Depends(get_db_sync)) -> dict:
    orders = session.execute(select(AgriOrder)).unique().scalars().all()
    counts = {key: 0 for key in pricing.ORDER_STATUSES}
    now = datetime.now(timezone.utc)
    month_sales = 0
    month_profit = 0
    month_orders = 0
    for order in orders:
        counts[order.status] = counts.get(order.status, 0) + 1
        if order.status == "cancelled" or order.ordered_at is None:
            continue
        if order.ordered_at.year == now.year and order.ordered_at.month == now.month:
            month_orders += 1
            month_sales += order.sale_amount
            month_profit += pricing.order_profit(
                order.sale_amount, order.unit_cost, order.quantity, float(order.fee_rate_percent)
            )
    product_count = session.execute(select(func.count()).select_from(AgriProduct)).scalar_one()
    supplier_count = session.execute(select(func.count()).select_from(AgriSupplier)).scalar_one()
    return {
        "status_counts": counts,
        "month_orders": month_orders,
        "month_sales": month_sales,
        "month_profit": month_profit,
        "product_count": product_count,
        "supplier_count": supplier_count,
    }
