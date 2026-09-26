"""add competitor price info to market_listings

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 등록 시점의 쿠팡 경쟁 상품 현황(정보용 — 가격을 자동으로 맞추지 않고, 사람이
    # 나중에 가격차가 큰 상품을 검토할 수 있도록 보관한다).
    op.add_column('market_listings', sa.Column('competitor_count', sa.Integer(), nullable=True))
    op.add_column('market_listings', sa.Column('competitor_lowest_price', sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('market_listings', 'competitor_lowest_price')
    op.drop_column('market_listings', 'competitor_count')
