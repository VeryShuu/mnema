"""voice_profiles: position (dolzhnost' dlya LLM-konteksta)

Revision ID: a4d6e8c0f2b5
Revises: f3a9c1b84d07
Create Date: 2026-10-02 07:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4d6e8c0f2b5'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1b84d07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('voice_profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('position', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('voice_profiles', schema=None) as batch_op:
        batch_op.drop_column('position')
