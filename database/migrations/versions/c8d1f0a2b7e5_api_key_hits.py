"""Обращения по ключам сайта: таблица `api_key_hits`.

Строка на (ключ, час, область): счётчик обращений и отказов по потолку.
Поштучно обращения не хранятся — графики строятся по дням и часам, а
таблица росла бы на сотню тысяч строк в сутки у одного сайта.

Новая таблица и только она: населённых колонок правка не трогает. Уникальный
индекс ведёт `api_key_id`, он же обслуживает внешний ключ; индекс по
`bucket_at` — под ночную уборку старого.

`downgrade` снимает таблицу: статистика — не учёт, терять её при откате можно.

Revision ID: c8d1f0a2b7e5
Revises: b7c4e2d91a03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8d1f0a2b7e5"
down_revision: Union[str, None] = "b7c4e2d91a03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_key_hits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("bucket_at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rejected", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", "bucket_at", "category", name="uq_api_key_hit"),
    )
    op.create_index("ix_api_key_hits_bucket_at", "api_key_hits", ["bucket_at"])


def downgrade() -> None:
    op.drop_table("api_key_hits")
