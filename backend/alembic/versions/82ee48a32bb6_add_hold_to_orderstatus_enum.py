"""add hold to orderstatus enum

Revision ID: 82ee48a32bb6
Revises: eff421c900ba
Create Date: 2026-09-23 02:06:02.586675

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82ee48a32bb6'
down_revision: Union[str, None] = 'eff421c900ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PRD 보완: 30분 재고 동기화 주기 사이의 순간 품절/발주 실패를 조용히 재시도만
    # 반복하지 않고, 사람이 확인해야 하는 상태로 명확히 멈추기 위한 새 상태값.
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'HOLD'")


def downgrade() -> None:
    # PostgreSQL은 ENUM 타입에서 값 1개만 제거하는 기능을 제공하지 않는다(타입을
    # 통째로 재생성해야 함) — 이미 HOLD 상태로 저장된 행이 있을 수 있어 안전하게
    # 자동으로 되돌릴 방법이 없으므로 downgrade는 의도적으로 아무 것도 하지 않는다.
    pass
