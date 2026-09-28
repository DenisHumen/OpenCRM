"""Подписки Web Push — звонок напоминания при закрытой вкладке (docs/bloki/31-web-push.md).

Одна новая таблица, засевать нечего: подписку заводит только браузер сотрудника.

Revision ID: c3f81d5a2e47
Revises: a7e2c95d1f38
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.types import ExactString

revision: str = "c3f81d5a2e47"
down_revision: Union[str, None] = "a7e2c95d1f38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=1000), nullable=False),
        sa.Column("endpoint_hash", ExactString(64), nullable=False),
        sa.Column("p256dh", sa.String(length=128), nullable=False),
        sa.Column("auth", sa.String(length=64), nullable=False),
        sa.Column("nazvanie", sa.String(length=120), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_push_subscriptions_endpoint_hash", "push_subscriptions", ["endpoint_hash"], unique=True)
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])


def downgrade() -> None:
    # Подписки пропадают с таблицей; браузеры перестают получать звонки при закрытой вкладке.
    op.drop_table("push_subscriptions")
