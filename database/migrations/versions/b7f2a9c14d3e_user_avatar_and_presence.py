"""user avatar and presence

Revision ID: b7f2a9c14d3e
Revises: e4451c527c34
Create Date: 2026-07-24 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7f2a9c14d3e'
down_revision: Union[str, None] = 'e4451c527c34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('avatar_path', sa.String(length=255), nullable=False, server_default='')
        )
        batch_op.add_column(sa.Column('last_seen_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('last_seen_at')
        batch_op.drop_column('avatar_path')
