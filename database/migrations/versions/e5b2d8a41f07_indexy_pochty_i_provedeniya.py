"""индексы: клиент по почте и бумаги по дате проведения

Revision ID: e5b2d8a41f07
Revises: c3f81d5a2e47

Разбор 29.09.2026, оба места — полный проход таблицы на частом пути:

- `mail.klient_po_pochte` искал клиента `lower(email) = ?`, а индекса на почте не
  было вовсе: каждое входящее письмо, каждое исходящее и каждая заявка с формы
  сайта читали `clients` целиком. Сортировка базы (`utf8mb4_0900_ai_ci`) регистр
  и так не различает, поэтому запрос стал простым равенством и идёт по индексу.
- сводка и статистика возвратов отбирают проведённые бумаги по `updated_at`, а
  `ix_documents_kind_status_created` сужал только до «все закрытые заказы» — на
  каждую строку поиск по первичному ключу ради даты.

Добавочная миграция: только индексы, данные не трогаются, откат их снимает.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e5b2d8a41f07"
down_revision: Union[str, None] = "c3f81d5a2e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEKSY = (
    ("ix_clients_email", "clients", ["email"]),
    ("ix_documents_kind_status_updated", "documents", ["kind", "status", "updated_at"]),
)


def upgrade() -> None:
    for name, table, columns in INDEKSY:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(INDEKSY):
        op.drop_index(name, table_name=table)
