"""confirm_end_requests: ручной вопрос «завершать ли запись» (слепой детектор тишины)

Revision ID: d9a3f6b18c42
Revises: c4e7a2d95b18
Create Date: 2026-10-09 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a3f6b18c42'
down_revision: Union[str, Sequence[str], None] = 'c4e7a2d95b18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'confirm_end_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column('screenshot_path', sa.String(length=512), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('responded_ts', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_confirm_end_requests_job_id', 'confirm_end_requests', ['job_id'], unique=False)
    op.create_index('ix_confirm_end_requests_status', 'confirm_end_requests', ['status'], unique=False)
    op.create_index('ix_confirm_end_requests_token', 'confirm_end_requests', ['token'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_confirm_end_requests_token', table_name='confirm_end_requests')
    op.drop_index('ix_confirm_end_requests_status', table_name='confirm_end_requests')
    op.drop_index('ix_confirm_end_requests_job_id', table_name='confirm_end_requests')
    op.drop_table('confirm_end_requests')
