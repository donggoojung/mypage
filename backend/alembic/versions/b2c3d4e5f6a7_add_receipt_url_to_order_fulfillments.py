"""add receipt_url to order_fulfillments

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PRD 9.1: 지식재산권 침해 신고 대응용 정식 구매 증빙(결제완료 화면 스크린샷) URL.
    op.add_column('order_fulfillments', sa.Column('receipt_url', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('order_fulfillments', 'receipt_url')
