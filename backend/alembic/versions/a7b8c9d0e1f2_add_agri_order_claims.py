"""add claim columns to agri_orders (고객의 취소·반품·교환 요청 표시)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-05 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agri_orders', sa.Column('claim_type', sa.String(length=16), nullable=True))
    op.add_column('agri_orders', sa.Column('claim_status', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('agri_orders', 'claim_status')
    op.drop_column('agri_orders', 'claim_type')
