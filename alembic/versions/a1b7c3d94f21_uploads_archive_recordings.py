"""uploads: arhivnye zapisi (video/audio) + Job.source

Revision ID: a1b7c3d94f21
Revises: fe94ae55777e
Create Date: 2026-09-20 15:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b7c3d94f21'
down_revision: Union[str, Sequence[str], None] = 'fe94ae55777e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=16),
                                      nullable=False, server_default='zoom'))
        batch_op.alter_column('zoom_meeting_id', existing_type=sa.String(length=64),
                              nullable=True)

    # bayty faylov mogut prevyshat' 2 GB (arhivnye zapisi) -> BigInteger
    with op.batch_alter_table('audio_files', schema=None) as batch_op:
        batch_op.alter_column('bytes', existing_type=sa.Integer(),
                              type_=sa.BigInteger(), existing_nullable=True)

    op.create_table(
        'uploads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=512), nullable=False),
        sa.Column('path', sa.String(length=1024), nullable=False),
        sa.Column('bytes_total', sa.BigInteger(), nullable=True),
        sa.Column('bytes_received', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('duration_s', sa.Float(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_ts', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_ts', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_uploads_job_id'), 'uploads', ['job_id'], unique=False)
    op.create_index(op.f('ix_uploads_status'), 'uploads', ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_uploads_status'), table_name='uploads')
    op.drop_index(op.f('ix_uploads_job_id'), table_name='uploads')
    op.drop_table('uploads')
    with op.batch_alter_table('audio_files', schema=None) as batch_op:
        batch_op.alter_column('bytes', existing_type=sa.BigInteger(),
                              type_=sa.Integer(), existing_nullable=True)
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('source')
