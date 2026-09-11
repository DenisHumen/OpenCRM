"""Ссылки на файлы наружу и журнал их открытий.

Две новые таблицы. Проверка «ровно один источник» стоит в схеме, а не в
запросе с замком: правило про одну строку, а не про то, сколько их всего.

Revision ID: c7a2f14b9e05
Revises: a4f1e9c72b30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.types import ExactString

revision: str = "c7a2f14b9e05"
down_revision: Union[str, None] = "a4f1e9c72b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "file_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stored_file_id", sa.Integer(), nullable=True),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("token", ExactString(length=64), nullable=False),
        sa.Column("rezhim", sa.String(length=16), nullable=False, server_default="view"),
        sa.Column("krug", sa.String(length=16), nullable=False, server_default="link"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("pin_hash", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["stored_file_id"], ["stored_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(stored_file_id IS NULL) <> (work_id IS NULL)",
            name="ck_file_links_odin_istochnik",
        ),
    )
    # Уникальность токена — именованным индексом, а не ограничением: модель
    # объявляет `unique=True, index=True`, и автогенератор ждёт именно индекс.
    op.create_index(op.f("ix_file_links_token"), "file_links", ["token"], unique=True)
    op.create_index(op.f("ix_file_links_stored_file_id"), "file_links", ["stored_file_id"])
    op.create_index(op.f("ix_file_links_work_id"), "file_links", ["work_id"])

    op.create_table(
        "file_link_views",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.Integer(), nullable=False),
        sa.Column("viewed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(length=300), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["link_id"], ["file_links.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_file_link_views_link_id"), "file_link_views", ["link_id"])
    op.create_index(op.f("ix_file_link_views_viewed_at"), "file_link_views", ["viewed_at"])


def downgrade() -> None:
    # Только таблицы: индексы уходят вместе с ними, а снять индекс отдельно
    # MySQL не даёт — он нужен внешнему ключу, который на нём же и стоит.
    op.drop_table("file_link_views")
    op.drop_table("file_links")
