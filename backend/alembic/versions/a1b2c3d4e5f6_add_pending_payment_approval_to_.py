"""add pending_payment_approval to orderstatus enum

Revision ID: a1b2c3d4e5f6
Revises: f3a9c1d4e5b6
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f3a9c1d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 반자동 결제 승인: RPA가 배송지 입력까지 마치고 실제 결제 버튼 직전에서 멈추면
    # 에러(HOLD)가 아니라 이 상태로 전환해, 관리자가 대시보드에서 [결제 승인]을
    # 눌러야만 실제 결제가 이어서 완료되도록 한다.
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'PENDING_PAYMENT_APPROVAL'")


def downgrade() -> None:
    # PostgreSQL은 ENUM 타입에서 값 1개만 제거하는 기능을 제공하지 않는다(타입을
    # 통째로 재생성해야 함) — 이미 이 상태로 저장된 행이 있을 수 있어 안전하게 자동으로
    # 되돌릴 방법이 없으므로 downgrade는 의도적으로 아무 것도 하지 않는다.
    pass
