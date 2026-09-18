from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin
from app.models.enums import GenerationStatus


class GeneratedAsset(Base, TimestampMixin):
    """저작권 회피를 위해 AI가 새로 생성한 썸네일·상세페이지 S3 주소 (PRD 3장, 8.2)."""

    __tablename__ = "generated_assets"

    asset_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("master_products.product_id"), nullable=False, index=True)
    ai_thumbnail_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    ai_detail_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exif_cleared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generation_status: Mapped[GenerationStatus] = mapped_column(
        SAEnum(GenerationStatus), default=GenerationStatus.PENDING, nullable=False
    )

    product: Mapped["MasterProduct"] = relationship(back_populates="generated_assets")
