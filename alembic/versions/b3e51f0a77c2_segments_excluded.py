"""segments: priznak excluded (fragment isklyuchen iz analiza operatorom)

Revision ID: b3e51f0a77c2
Revises: a1b7c3d94f21
Create Date: 2026-09-23 08:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e51f0a77c2'
down_revision: Union[str, Sequence[str], None] = 'a1b7c3d94f21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('segments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('excluded', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('segments', schema=None) as batch_op:
        batch_op.drop_column('excluded')
