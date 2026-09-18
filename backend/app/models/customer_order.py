from sqlalchemy import Numeric, String
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
    recipient_name: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    shipping_addr: Mapped[str] = mapped_column(String(512), nullable=False)
    paid_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # PRD 5.2 "상태값을 ORDER_PURCHASED로 전환" 등 주문 파이프라인 전이를 추적하기 위한 컬럼.
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.RECEIVED, nullable=False)

    fulfillments: Mapped[list["OrderFulfillment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    return_requests: Mapped[list["ReturnRequest"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
