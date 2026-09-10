"""Модуль «Файлы»: свои папки и свои файлы.

Две новые таблицы, ничего существующего не трогаем. Порядок — по ссылкам:
сначала папки, потом файлы в них.

Revision ID: a4f1e9c72b30
Revises: e1c74a90b352
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4f1e9c72b30"
down_revision: Union[str, None] = "e1c74a90b352"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "file_folders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["file_folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_file_folders_parent_id"), "file_folders", ["parent_id"])

    op.create_table(
        "stored_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("folder_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("file_uid", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["folder_id"], ["file_folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_uid"),
    )
    op.create_index(op.f("ix_stored_files_folder_id"), "stored_files", ["folder_id"])


def downgrade() -> None:
    # Только таблицы: индексы уходят вместе с ними, а снять индекс отдельно
    # MySQL не даёт — он нужен внешнему ключу, который на нём же и стоит.
    op.drop_table("stored_files")
    op.drop_table("file_folders")
