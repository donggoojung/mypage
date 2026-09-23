from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import MarketType, OrderStatus


class CustomerOrder(Base, TimestampMixin):
    """마켓에서 결제 완료되어 유입된 실 고객 주문 및 배송지 정보 (PRD 5.1, 8.2)."""

    __tablename__ = "customer_orders"

    order_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_type: Mapped[MarketType] = mapped_column(SAEnum(MarketType), nullable=False)
    market_order_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    # 주문서에서 추출한 제조사 품번(마스터 상품) + 사이즈 옵션 (PRD 5.1 "제조사 품번과 사이즈 옵션을 추출").
    product_id: Mapped[int] = mapped_column(ForeignKey("master_products.product_id"), nullable=False, index=True)
    ordered_size: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    shipping_addr: Mapped[str] = mapped_column(String(512), nullable=False)
    paid_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # PRD 5.2 "상태값을 ORDER_PURCHASED로 전환" 등 주문 파이프라인 전이를 추적하기 위한 컬럼.
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.RECEIVED, nullable=False)
    # PRD 6.1 발송처리(confirm_shipping) 시 쿠팡 송장업로드 API에 그대로 넘겨야 하는
    # 식별자 — 발주서 조회(detect_new_orders) 시점에 orderItems에서 같이 받아 저장해둔다.
    market_shipment_box_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_vendor_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    product: Mapped["MasterProduct"] = relationship()

    fulfillments: Mapped[list["OrderFulfillment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    return_requests: Mapped[list["ReturnRequest"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
