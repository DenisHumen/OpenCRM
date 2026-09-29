"""индексы уборки: уведомления и звонки по дате

Revision ID: f1c4a7e9d253
Revises: e5b2d8a41f07

Разбор 29.09.2026: уборка старого идёт пачками `DELETE … WHERE created_at < ?
LIMIT n`, а индекса на одной дате у `notifications` и `task_signals` не было —
каждая пачка читала таблицу с первой строки, а при пустом итоге целиком.

Добавочная миграция: только индексы, данные не трогаются, откат их снимает.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f1c4a7e9d253"
down_revision: Union[str, None] = "e5b2d8a41f07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEKSY = (
    ("ix_notifications_created_at", "notifications", ["created_at"]),
    ("ix_task_signals_created_at", "task_signals", ["created_at"]),
)


def upgrade() -> None:
    for name, table, columns in INDEKSY:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(INDEKSY):
        op.drop_index(name, table_name=table)
