"""농산물 위탁판매용 테이블 — 신발 구매대행 테이블과 분리해서 관리한다.

상태·구분 값은 PostgreSQL ENUM 대신 문자열로 저장한다. 값이 자주 늘어날 수 있어
(마켓 추가, 주문 단계 추가) 매번 ENUM 변경 마이그레이션을 만들지 않기 위해서다.
허용 값 검사는 app/services/agri_pricing.py 상수와 API 입력 검사에서 한다.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin


class AgriSupplier(Base, TimestampMixin):
    __tablename__ = "agri_suppliers"

    supplier_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    site_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # excel(엑셀 발주) / auto(자동발주) / kakao(카카오톡) / etc
    order_method: Mapped[str] = mapped_column(String(32), default="excel", nullable=False)
    # 공급가에 택배비가 이미 포함돼 있으면 0, 아니면 상자당 택배비.
    default_shipping_fee: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    remote_area_fee: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cutoff_time: Mapped[str | None] = mapped_column(String(32), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgriProduct(Base, TimestampMixin):
    __tablename__ = "agri_products"

    product_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("agri_suppliers.supplier_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # fruit(과일) / vegetable(채소) / rice(쌀) / grain(잡곡) / etc
    category: Mapped[str] = mapped_column(String(32), default="fruit", nullable=False)
    origin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    producer: Mapped[str | None] = mapped_column(String(256), nullable=True)
    storage: Mapped[str | None] = mapped_column(String(256), nullable=True)
    packing_info: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # 쌀 양곡표시용 (쌀이 아니면 비워둔다)
    variety: Mapped[str | None] = mapped_column(String(128), nullable=True)
    harvest_year: Mapped[str | None] = mapped_column(String(32), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # ready(판매 준비) / selling(판매중) / soldout(품절) / stopped(판매 중지)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)

    supplier: Mapped[AgriSupplier] = relationship(lazy="joined")
    options: Mapped[list["AgriOption"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="AgriOption.sort_order",
        lazy="selectin",
    )


class AgriOption(Base, TimestampMixin):
    """판매 단위 하나 (예: "5kg 중과 15~18과"). 가격은 옵션마다 따로 계산한다."""

    __tablename__ = "agri_options"

    option_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("agri_products.product_id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    weight: Mapped[str | None] = mapped_column(String(64), nullable=True)
    count: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supply_price: Mapped[int] = mapped_column(Integer, nullable=False)
    shipping_fee: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 비워두면 마켓별 추천 판매가를 쓴다. 대표님이 직접 정한 가격이 있으면 모든 마켓에 이 가격.
    sale_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_soldout: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped[AgriProduct] = relationship(back_populates="options")


class AgriOrder(Base, TimestampMixin):
    __tablename__ = "agri_orders"
    __table_args__ = (UniqueConstraint("market", "market_order_no"),)

    order_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    market_order_no: Mapped[str] = mapped_column(String(128), nullable=False)
    option_id: Mapped[int] = mapped_column(ForeignKey("agri_options.option_id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receiver_name: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_phone: Mapped[str] = mapped_column(String(64), nullable=False)
    zipcode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    delivery_memo: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 고객이 실제로 결제한 금액 (수량 포함 합계)
    sale_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # 주문 시점의 원가·수수료를 따로 적어둔다. 나중에 공급가가 바뀌어도 지난 주문의 이익이 바뀌지 않게.
    unit_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    fee_rate_percent: Mapped[str] = mapped_column(String(16), nullable=False)
    # new(발주 대기) / ordered(발주 완료) / invoiced(송장 받음) / shipped(마켓 송장 입력 끝) / cancelled(취소)
    status: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    courier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purchase_ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 고객이 마켓에서 낸 취소·반품·교환 요청. claim_type: cancel / return / exchange, claim_status: requested / done / rejected
    claim_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    claim_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 출고가 늦어서 마켓에 "발송 지연"을 알린 기록 (가짜 송장 대신 이것을 쓴다). delay_reason: stock / customer
    delayed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delay_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    option: Mapped[AgriOption] = relationship(lazy="joined")


class AgriListing(Base, TimestampMixin):
    """상품 하나가 어느 마켓에 어떤 번호로 올라가 있는지 (같은 마켓에 두 번 등록하는 것을 막는다)."""

    __tablename__ = "agri_listings"
    __table_args__ = (UniqueConstraint("product_id", "market"),)

    listing_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("agri_products.product_id", ondelete="CASCADE"), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    # 스마트스토어: 원상품번호(originProductNo) / 채널상품번호
    market_product_no: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_product_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # registered(등록됨) / stopped(판매 중지)
    status: Mapped[str] = mapped_column(String(32), default="registered", nullable=False)
    # Mock(가짜)으로 올린 기록인지. 진짜 마켓에 올린 것과 섞이지 않게 구분한다.
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 등록 당시 옵션별 판매가 {"옵션명": 가격} — 나중에 가격이 바뀌었는지 비교하는 용도
    registered_prices: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgriSetting(Base, TimestampMixin):
    """마켓 수수료, 품목별 최소 이익 같은 설정값 (키 하나에 값 묶음)."""

    __tablename__ = "agri_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
