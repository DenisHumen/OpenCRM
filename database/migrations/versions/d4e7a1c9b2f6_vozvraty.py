"""Возвраты покупателя: `documents.refund_minor` и таблица `document_files`.

Возврат — новый вид бумаги в той же таблице (`kind = return`), поэтому
таблицы у него нет. Своя колонка одна — сколько денег вернули клиенту; она
nullable и у остальных видов пуста, заполнять населённые строки нечем и
незачем. Вложения (фото и видео к возврату) — новая таблица по образцу
`client_files`.

Порядок тот же, что записан в правилах: колонка nullable сразу, таблица
новая. Ни одного `UPDATE` по населённым строкам.

`downgrade` снимает таблицу и колонку: возвраты, проведённые на новой версии,
после отката останутся бумагами без суммы — но останутся, вместе с движениями
склада и деньгами, которые лежат в своих таблицах.

Revision ID: d4e7a1c9b2f6
Revises: c8d1f0a2b7e5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7a1c9b2f6"
down_revision: Union[str, None] = "c8d1f0a2b7e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("file_uid", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_uid"),
    )
    op.create_index("ix_document_files_document_id", "document_files", ["document_id"])
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("refund_minor", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("refund_minor")
    # Индекс уходит вместе с таблицей: снять его отдельно MySQL не даёт —
    # на нём держится внешний ключ.
    op.drop_table("document_files")
