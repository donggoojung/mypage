from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import SourcePlatform


class SourceMapping(Base, TimestampMixin):
    """ABC마트, 무신사 등 각 공급처별 실시간 가격·사이즈 재고 매핑 (PRD 2.1, 8.2)."""

    __tablename__ = "source_mappings"

    source_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("master_products.product_id"), nullable=False, index=True)
    source_platform: Mapped[SourcePlatform] = mapped_column(SAEnum(SourcePlatform), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # 예: {"250": {"stock": 3, "is_sold_out": false}, "260": {"stock": 0, "is_sold_out": true}}
    size_stock_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped["MasterProduct"] = relationship(back_populates="source_mappings")
