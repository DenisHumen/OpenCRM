"""Напоминания: важность, подробности и вложения.

Две колонки с умолчанием и новая таблица под вложения. Существующие
напоминания получают важность «normal» и пустые подробности — ровно то, чем
они и были: без важности и без разбора. `UPDATE` по населённым строкам нет:
обе колонки приходят со `server_default`.

У TEXT умолчание задаётся формой-выражением `DEFAULT ('')` — обычное MySQL
запрещает, разбор в `database/types.py:text_default`. Сами подробности —
`MEDIUMTEXT`: 65 535 БАЙТ обычного TEXT это шестнадцать тысяч эмодзи, и разбор
с картинками из мессенджера обрезался бы молча (`database/types.py:LongText`).

`downgrade` снимает и колонки, и таблицу: сами напоминания при этом целы,
теряются только важность и приложенные снимки.

Revision ID: b6e1f38c92d7
Revises: a4d9c6e2f107
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.types import LongText, text_default

revision: str = "b6e1f38c92d7"
down_revision: Union[str, None] = "a4d9c6e2f107"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("file_uid", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_uid"),
    )
    op.create_index("ix_task_files_task_id", "task_files", ["task_id"])

    with op.batch_alter_table("tasks") as batch:
        batch.add_column(
            sa.Column("vazhnost", sa.String(length=8), nullable=False, server_default="normal")
        )
        batch.add_column(
            sa.Column("note", LongText, nullable=False, server_default=text_default())
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("note")
        batch.drop_column("vazhnost")
    # Индекс уходит вместе с таблицей: снять его отдельно MySQL не даёт —
    # на нём держится внешний ключ.
    op.drop_table("task_files")
