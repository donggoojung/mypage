"""add agri tables (농산물 위탁판매)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-05 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'agri_suppliers',
        sa.Column('supplier_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('site_url', sa.String(length=512), nullable=True),
        sa.Column('contact', sa.String(length=256), nullable=True),
        sa.Column('order_method', sa.String(length=32), nullable=False),
        sa.Column('default_shipping_fee', sa.Integer(), nullable=False),
        sa.Column('remote_area_fee', sa.Integer(), nullable=False),
        sa.Column('cutoff_time', sa.String(length=32), nullable=True),
        sa.Column('memo', sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint('supplier_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'agri_products',
        sa.Column('product_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('origin', sa.String(length=128), nullable=True),
        sa.Column('producer', sa.String(length=256), nullable=True),
        sa.Column('storage', sa.String(length=256), nullable=True),
        sa.Column('packing_info', sa.String(length=256), nullable=True),
        sa.Column('variety', sa.String(length=128), nullable=True),
        sa.Column('harvest_year', sa.String(length=32), nullable=True),
        sa.Column('grade', sa.String(length=64), nullable=True),
        sa.Column('image_url', sa.String(length=1024), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('memo', sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['supplier_id'], ['agri_suppliers.supplier_id']),
        sa.PrimaryKeyConstraint('product_id'),
    )
    op.create_table(
        'agri_options',
        sa.Column('option_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=256), nullable=False),
        sa.Column('weight', sa.String(length=64), nullable=True),
        sa.Column('count', sa.String(length=64), nullable=True),
        sa.Column('grade', sa.String(length=64), nullable=True),
        sa.Column('supplier_code', sa.String(length=128), nullable=True),
        sa.Column('supply_price', sa.Integer(), nullable=False),
        sa.Column('shipping_fee', sa.Integer(), nullable=False),
        sa.Column('sale_price', sa.Integer(), nullable=True),
        sa.Column('is_soldout', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(['product_id'], ['agri_products.product_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('option_id'),
    )
    op.create_table(
        'agri_orders',
        sa.Column('order_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('market', sa.String(length=32), nullable=False),
        sa.Column('market_order_no', sa.String(length=128), nullable=False),
        sa.Column('option_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('buyer_name', sa.String(length=64), nullable=True),
        sa.Column('receiver_name', sa.String(length=64), nullable=False),
        sa.Column('receiver_phone', sa.String(length=64), nullable=False),
        sa.Column('zipcode', sa.String(length=16), nullable=True),
        sa.Column('address', sa.String(length=512), nullable=False),
        sa.Column('delivery_memo', sa.String(length=512), nullable=True),
        sa.Column('sale_amount', sa.Integer(), nullable=False),
        sa.Column('unit_cost', sa.Integer(), nullable=False),
        sa.Column('fee_rate_percent', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('courier', sa.String(length=64), nullable=True),
        sa.Column('tracking_no', sa.String(length=64), nullable=True),
        sa.Column('ordered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('purchase_ordered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('memo', sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['option_id'], ['agri_options.option_id']),
        sa.PrimaryKeyConstraint('order_id'),
        sa.UniqueConstraint('market', 'market_order_no'),
    )
    op.create_table(
        'agri_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('agri_settings')
    op.drop_table('agri_orders')
    op.drop_table('agri_options')
    op.drop_table('agri_products')
    op.drop_table('agri_suppliers')
