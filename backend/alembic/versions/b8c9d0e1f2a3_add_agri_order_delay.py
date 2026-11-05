"""add delay columns to agri_orders (발송 지연 처리 기록)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-06 09:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agri_orders', sa.Column('delayed_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agri_orders', sa.Column('delay_reason', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('agri_orders', 'delay_reason')
    op.drop_column('agri_orders', 'delayed_until')
