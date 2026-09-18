from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import ListingStatus, MarketType


class MarketListing(Base, TimestampMixin):
    """스마트스토어/쿠팡/G마켓 등에 등록된 고유 상품 ID 및 판매 상태 (PRD 4장, 8.2)."""

    __tablename__ = "market_listings"

    listing_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("master_products.product_id"), nullable=False, index=True)
    market_type: Mapped[MarketType] = mapped_column(SAEnum(MarketType), nullable=False)
    market_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selling_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[ListingStatus] = mapped_column(SAEnum(ListingStatus), default=ListingStatus.DRAFT, nullable=False)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped["MasterProduct"] = relationship(back_populates="market_listings")
