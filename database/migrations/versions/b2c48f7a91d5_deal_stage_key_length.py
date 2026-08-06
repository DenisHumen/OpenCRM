"""deals.stage: длина под ключ этапа

Ключ этапа генерируется из названия (`pipeline_service._free_key`) и обрезается
до 24 символов, плюс суффикс при совпадении — до 27. Хранится он в двух местах:
`pipeline_stages.key` (String(32)) и `deals.stage`, где в миграции осталось
VARCHAR(20).

SQLite длину VARCHAR не проверяет, поэтому расхождение ничем себя не выдавало.
MySQL в строгом режиме откажет, в нестрогом обрежет: «Waiting for customer
approval» даёт ключ `waiting_for_customer_app`, после обрезки —
`waiting_for_customer`, а такого этапа нет. Заявка при этом не ломается
явно — она просто перестаёт попадать в свою колонку и в отчёты. Переезд на
MySQL в проекте запланирован (`scripts/migrate_to_mysql.py`), так что случай
не гипотетический.

Приводим к 32 — как у `pipeline_stages.key`, куда это поле и ссылается по
смыслу.

Revision ID: b2c48f7a91d5
Revises: a9e64c17f235
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c48f7a91d5"
down_revision: Union[str, None] = "a9e64c17f235"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("deals") as batch:
        batch.alter_column(
            "stage",
            existing_type=sa.VARCHAR(length=20),
            type_=sa.String(length=32),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Обратно к 20 символов. Ключи длиннее при этом обрежутся, поэтому откат
    # безопасен только сразу после наката — что для отката и верно.
    with op.batch_alter_table("deals") as batch:
        batch.alter_column(
            "stage",
            existing_type=sa.String(length=32),
            type_=sa.VARCHAR(length=20),
            existing_nullable=False,
        )
