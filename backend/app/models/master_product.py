from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin


class MasterProduct(Base, TimestampMixin):
    """브랜드 고유 품번(Style Code) 기준 단일 정규화 상품 원부 (PRD 2.1, 8.2)."""

    __tablename__ = "master_products"

    product_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    style_code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    brand_name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_name: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_specs_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    source_mappings: Mapped[list["SourceMapping"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    generated_assets: Mapped[list["GeneratedAsset"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    market_listings: Mapped[list["MarketListing"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
