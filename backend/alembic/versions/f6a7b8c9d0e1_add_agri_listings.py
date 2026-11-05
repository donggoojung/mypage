"""add agri_listings (농산물 상품의 마켓 등록 기록)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-05 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agri_listings',
        sa.Column('listing_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('market', sa.String(length=32), nullable=False),
        sa.Column('market_product_no', sa.String(length=64), nullable=False),
        sa.Column('channel_product_no', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('is_mock', sa.Boolean(), nullable=False),
        sa.Column('registered_prices', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('registered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['agri_products.product_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('listing_id'),
        sa.UniqueConstraint('product_id', 'market'),
    )


def downgrade() -> None:
    op.drop_table('agri_listings')
