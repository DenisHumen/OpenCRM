"""Раскладка сводки у сотрудника: `users.dashboard_json`.

Колонка nullable: у существующих сотрудников раскладки нет — экран показывает
умолчание, и заполнять строки нечем. `downgrade` снимает колонку; раскладки
теряются, сводка возвращается к умолчанию — данных фирмы это не трогает.

Revision ID: f2a7c9e4b1d6
Revises: e5f2b7c1d9a4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7c9e4b1d6"
down_revision: Union[str, None] = "e5f2b7c1d9a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("dashboard_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("dashboard_json")
