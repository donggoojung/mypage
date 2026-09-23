"""add cancel_requested/return_requested to orderstatus enum

Revision ID: f3a9c1d4e5b6
Revises: 82ee48a32bb6
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d4e5b6'
down_revision: Union[str, None] = '82ee48a32bb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PRD 7장 반품/교환(역물류) 자동화: 소싱처(ABC마트) 매입 완료 후 취소가 감지된 경우와
    # 배송완료 후 반품이 접수된 경우를 구분해 사람이 확인해야 하는 상태로 명확히 멈추기
    # 위한 새 상태값 (HOLD와 같은 목적, 다른 트리거).
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'CANCEL_REQUESTED'")
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'RETURN_REQUESTED'")


def downgrade() -> None:
    # PostgreSQL은 ENUM 타입에서 값 1개만 제거하는 기능을 제공하지 않는다(타입을
    # 통째로 재생성해야 함) — 이미 이 상태로 저장된 행이 있을 수 있어 안전하게 자동으로
    # 되돌릴 방법이 없으므로 downgrade는 의도적으로 아무 것도 하지 않는다.
    pass
