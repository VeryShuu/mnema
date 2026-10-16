"""segments: asr_json — тайминги ASR-сегментов внутри VAD-строки

Revision ID: aa3c91f6d2b5
Revises: d9a3f6b18c42
Create Date: 2026-10-15 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa3c91f6d2b5'
down_revision: Union[str, Sequence[str], None] = 'd9a3f6b18c42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('segments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('asr_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('segments', schema=None) as batch_op:
        batch_op.drop_column('asr_json')
