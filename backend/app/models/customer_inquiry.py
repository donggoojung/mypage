from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import InquiryStatus, InquiryType, MarketType


class CustomerInquiry(Base, TimestampMixin):
    """쿠팡 고객문의 + AI 답변 초안 + 실제 등록한 답변."""

    __tablename__ = "customer_inquiries"
    __table_args__ = (UniqueConstraint("market_type", "inquiry_type", "market_inquiry_id"),)

    inquiry_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_type: Mapped[MarketType] = mapped_column(SAEnum(MarketType), nullable=False)
    inquiry_type: Mapped[InquiryType] = mapped_column(SAEnum(InquiryType), nullable=False)
    market_inquiry_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 우리 상품과 연결되지 않는 문의(삭제된 상품 등)도 있을 수 있어 nullable.
    product_id: Mapped[int | None] = mapped_column(ForeignKey("master_products.product_id"), nullable=True)
    product_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    inquired_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 쿠팡 고객센터 이관 문의에 답할 때 필요한 상위 답변 ID.
    parent_answer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_draft_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[InquiryStatus] = mapped_column(SAEnum(InquiryStatus), default=InquiryStatus.PENDING, nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
