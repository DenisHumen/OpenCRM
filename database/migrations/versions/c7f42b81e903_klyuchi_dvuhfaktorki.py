"""Ключи: хранилище кодов двухфакторной авторизации.

Четыре новые таблицы, ни одной правки существующих: блок приходит целиком и
сбоку. Порядок создания — от того, на что ссылаются, к тому, что ссылается:
категории, ключи, два списка доступа.

Секрет и запасные коды лежат `MEDIUMTEXT`: обычного TEXT хватило бы с запасом,
но шифротекст `secretbox` вчетверо длиннее исходного, а запасных кодов сервисы
дают до сотни — и обрезание молчаливое (`database/types.py:LongText`).

`downgrade` сносит таблицы целиком, и это значит потерю ключей: восстановить их
из данных нечем. Откат сюда — только вместе с копией базы.

Revision ID: c7f42b81e903
Revises: b6e1f38c92d7
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.types import LongText, text_default

revision: str = "c7f42b81e903"
down_revision: Union[str, None] = "b6e1f38c92d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "key_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("zakrytaya", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_key_categories_created_by", "key_categories", ["created_by"])

    op.create_table(
        "two_factor_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("issuer", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("account", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("secret_encrypted", LongText, nullable=False),
        sa.Column("backup_codes_encrypted", LongText, nullable=True),
        sa.Column("digits", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("period", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("algorithm", sa.String(length=8), nullable=False, server_default="SHA1"),
        sa.Column("vazhnost", sa.String(length=8), nullable=False, server_default="normal"),
        sa.Column("note", LongText, nullable=False, server_default=text_default()),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["key_categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_two_factor_keys_category_id", "two_factor_keys", ["category_id"])
    op.create_index("ix_two_factor_keys_task_id", "two_factor_keys", ["task_id"])
    op.create_index("ix_two_factor_keys_created_by", "two_factor_keys", ["created_by"])
    op.create_index("ix_two_factor_keys_deleted_at", "two_factor_keys", ["deleted_at"])

    op.create_table(
        "two_factor_key_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["key_id"], ["two_factor_keys.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_id", "user_id", name="uq_key_access"),
    )
    op.create_index("ix_two_factor_key_access_key_id", "two_factor_key_access", ["key_id"])
    op.create_index("ix_two_factor_key_access_user_id", "two_factor_key_access", ["user_id"])

    op.create_table(
        "key_category_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["key_categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category_id", "user_id", name="uq_key_category_access"),
    )
    op.create_index("ix_key_category_access_category_id", "key_category_access", ["category_id"])
    op.create_index("ix_key_category_access_user_id", "key_category_access", ["user_id"])


def downgrade() -> None:
    # Индексы уходят вместе с таблицами: снять их отдельно MySQL не даёт — на
    # них держатся внешние ключи. Порядок обратный созданию.
    op.drop_table("key_category_access")
    op.drop_table("two_factor_key_access")
    op.drop_table("two_factor_keys")
    op.drop_table("key_categories")
