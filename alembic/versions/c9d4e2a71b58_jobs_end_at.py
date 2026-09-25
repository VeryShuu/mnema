"""jobs.end_at: predpolagaemoe okonchanie vstrechi (aktivaciya proverki tishiny)

Revision ID: c9d4e2a71b58
Revises: b3e51f0a77c2
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9d4e2a71b58'
down_revision: Union[str, Sequence[str], None] = 'b3e51f0a77c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('end_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'end_at')
