from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import InspectionStatus


class ReturnRequest(Base, TimestampMixin):
    """고객 반품/교환 접수, 거점 입고 검수 상태 및 소싱처 환불 연동 내역 (PRD 7장, 8.2)."""

    __tablename__ = "return_requests"

    return_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("customer_orders.order_id"), nullable=False, index=True)
    market_claim_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_reason: Mapped[str] = mapped_column(String(512), nullable=False)
    inspection_status: Mapped[InspectionStatus] = mapped_column(
        SAEnum(InspectionStatus), default=InspectionStatus.RECEIVED_AT_WAREHOUSE, nullable=False
    )
    source_refund_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    order: Mapped["CustomerOrder"] = relationship(back_populates="return_requests")
