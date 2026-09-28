"""preset_links table

Revision ID: d7f0b21c58a9
Revises: c9d4e2a71b58
Create Date: 2026-09-28 16:20:11.402317

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f0b21c58a9'
down_revision: Union[str, Sequence[str], None] = 'c9d4e2a71b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'preset_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('meeting_link', sa.String(length=1024), nullable=False),
        sa.Column('pwd_ref', sa.String(length=255), nullable=True),
        sa.Column('created_ts', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('preset_links')
