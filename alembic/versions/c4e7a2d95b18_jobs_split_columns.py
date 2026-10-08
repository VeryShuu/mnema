"""jobs: parent_job_id + split_json (разделение встреч)

Revision ID: c4e7a2d95b18
Revises: b5f1a9d3c7e4
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e7a2d95b18'
down_revision: Union[str, Sequence[str], None] = 'b5f1a9d3c7e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_job_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('split_json', sa.JSON(), nullable=True))
        batch_op.create_index('ix_jobs_parent_job_id', ['parent_job_id'], unique=False)
        batch_op.create_foreign_key('fk_jobs_parent_job_id', 'jobs',
                                    ['parent_job_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_jobs_parent_job_id', type_='foreignkey')
        batch_op.drop_index('ix_jobs_parent_job_id')
        batch_op.drop_column('split_json')
        batch_op.drop_column('parent_job_id')
