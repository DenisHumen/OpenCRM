"""Токены сотрудников к /api/v1 — для программ и агентов (docs/bloki/30-tokeny-i-mcp.md).

Одна новая таблица, засевать нечего: система не выдаёт токенов сама — обновление,
само открывшее вход, было бы утечкой.

Revision ID: a7e2c95d1f38
Revises: d4c19e7a5b62
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.types import ExactString

revision: str = "a7e2c95d1f38"
down_revision: Union[str, None] = "d4c19e7a5b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", ExactString(64), nullable=False),
        sa.Column("tolko_chtenie", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_tokens_token_hash", "user_tokens", ["token_hash"], unique=True)
    op.create_index("ix_user_tokens_user_id", "user_tokens", ["user_id"])
    op.create_index("ix_user_tokens_created_by", "user_tokens", ["created_by"])


def downgrade() -> None:
    # Токены пропадают вместе с таблицей; выданные строки перестают пускать сразу.
    op.drop_table("user_tokens")
