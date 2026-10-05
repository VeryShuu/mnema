"""notifications: data_json + screenshot_path

Revision ID: b5f1a9d3c7e4
Revises: a4d6e8c0f2b5
Create Date: 2026-10-02 13:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5f1a9d3c7e4'
down_revision: Union[str, Sequence[str], None] = 'a4d6e8c0f2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data_json', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('screenshot_path', sa.String(length=512),
                                      nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('screenshot_path')
        batch_op.drop_column('data_json')
