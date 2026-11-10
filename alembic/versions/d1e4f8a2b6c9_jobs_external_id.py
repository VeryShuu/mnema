"""jobs.external_id: idempotentnost' sozdaniya vstrechi vneshney sistemoy (portal)

Revision ID: d1e4f8a2b6c9
Revises: aa3c91f6d2b5
Create Date: 2026-11-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd1e4f8a2b6c9'
down_revision: Union[str, Sequence[str], None] = 'aa3c91f6d2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('external_id', sa.String(128), nullable=True))
    # unique na nullable-kolonke: mnozhestvo NULL razresheno i v SQLite, i v PG
    op.create_index('ix_jobs_external_id', 'jobs', ['external_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_jobs_external_id', table_name='jobs')
    op.drop_column('jobs', 'external_id')
