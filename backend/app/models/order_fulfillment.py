from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import SourcePlatform


class OrderFulfillment(Base, TimestampMixin):
    """소싱처 무인 결제 내역 및 수집된 운송장 번호 (PRD 5.2, 6.1, 8.2)."""

    __tablename__ = "order_fulfillments"

    fulfillment_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("customer_orders.order_id"), nullable=False, index=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(SAEnum(SourcePlatform), nullable=False)
    source_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cost_paid: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    courier_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)

    order: Mapped["CustomerOrder"] = relationship(back_populates="fulfillments")
