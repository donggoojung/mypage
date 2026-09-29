"""add customer_inquiries

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-29 13:00:01.787069

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('customer_inquiries',
    sa.Column('inquiry_id', sa.Integer(), autoincrement=True, nullable=False),
    # markettype 타입은 이미 market_listings 등에서 만들어져 있어 다시 생성하지 않는다.
    sa.Column('market_type', postgresql.ENUM(name='markettype', create_type=False), nullable=False),
    sa.Column('inquiry_type', sa.Enum('PRODUCT', 'CALL_CENTER', name='inquirytype'), nullable=False),
    sa.Column('market_inquiry_id', sa.String(length=128), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('product_label', sa.String(length=512), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('inquired_at', sa.String(length=64), nullable=True),
    sa.Column('parent_answer_id', sa.String(length=128), nullable=True),
    sa.Column('ai_draft_answer', sa.Text(), nullable=True),
    sa.Column('final_answer', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('PENDING', 'ANSWERED', name='inquirystatus'), nullable=False),
    sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('raw_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['master_products.product_id'], ),
    sa.PrimaryKeyConstraint('inquiry_id'),
    sa.UniqueConstraint('market_type', 'inquiry_type', 'market_inquiry_id')
    )


def downgrade() -> None:
    op.drop_table('customer_inquiries')
    sa.Enum(name='inquirystatus').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='inquirytype').drop(op.get_bind(), checkfirst=True)
