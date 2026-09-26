from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
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
    # {"250": "1234567890", "260": "1234567891", ...} — 사이즈별 쿠팡 옵션ID(vendorItemId).
    # 승인(판매중) 전에는 쿠팡이 옵션ID를 아직 발급하지 않아 비어있다 — 재고/가격 동기화
    # (stop_selling_item 등)는 이 값이 채워진 뒤에만 가능하다.
    vendor_item_ids_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # 등록 시점에 확인한 쿠팡 경쟁 상품 현황(정보용 — 이 값으로 selling_price를 자동
    # 조정하지 않는다. 가격 차이가 큰 상품을 나중에 사람이 검토할 수 있게 남겨둔다).
    competitor_count: Mapped[int | None] = mapped_column(nullable=True)
    competitor_lowest_price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    product: Mapped["MasterProduct"] = relationship(back_populates="market_listings")
