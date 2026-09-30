"""voice quality: diar_turns + ref_embeddings

Revision ID: e2c8f4a6b913
Revises: d7f0b21c58a9
Create Date: 2026-09-29 17:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2c8f4a6b913'
down_revision: Union[str, Sequence[str], None] = 'd7f0b21c58a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('diar_turns', sa.JSON(), nullable=True))

    with op.batch_alter_table('voice_profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ref_embeddings', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('voice_profiles', schema=None) as batch_op:
        batch_op.drop_column('ref_embeddings')

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('diar_turns')
