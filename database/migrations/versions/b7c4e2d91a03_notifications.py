"""Уведомления сотрудникам: таблица `notifications`.

Новая таблица и только она — населённых колонок правка не трогает, поэтому
опасного места «`NOT NULL` на живых строках» нет. Индексы составные от
`user_id`: колокольчик считает непрочитанные, список читает свежие — обе
выборки идут от человека.

`downgrade` снимает таблицу: уведомления — подсказки, а не учёт, и терять их
при откате можно.

Revision ID: b7c4e2d91a03
Revises: e5a9c3d17b04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e2d91a03"
down_revision: Union[str, None] = "e5a9c3d17b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=200), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_read", "notifications", ["user_id", "read_at"])
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])


def downgrade() -> None:
    # Индексы уходят вместе с таблицей: снять их по одному MySQL не даёт —
    # ведущий столбец `user_id` держит внешний ключ (ошибка 1553).
    op.drop_table("notifications")
