"""Телеграм: ответ на конкретное сообщение.

Одна колонка: `telegram_messages.reply_to_id` — ссылка на СВОЮ же таблицу, на ту
запись, которой отвечают.

**Почему на свою запись, а не на `external_id` телеграма.** Номер сообщения в
телеграме уникален внутри чата, а не вообще; ссылка на него означала бы пару
«чат + номер» и поиск по ней при каждом показе ленты. Своя ссылка — обычный
внешний ключ, и она работает одинаково для входящих (номер у нас есть) и для
исходящих, у которых номер появляется только ПОСЛЕ успешной отправки, а
показать цитату надо сразу.

`ondelete="SET NULL"`, а не `CASCADE`: удаление сообщения, на которое ссылались,
не должно уносить ответ на него. Ответ при этом теряет цитату и остаётся
обычным сообщением — это ровно то поведение, которое человек ожидает.

Порядок для населённой таблицы соблюдён: колонка добавляется nullable, значений
существующим строкам не нужно вовсе («ответом ни на что» они и были). Внешний
ключ создаётся вместе с колонкой — MySQL заводит под него индекс сам, поэтому
отдельного `create_index` здесь нет.

Revision ID: c5b7e93a1d64
Revises: a4e8d21f56b0
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5b7e93a1d64"
down_revision: Union[str, None] = "a4e8d21f56b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_messages",
        sa.Column("reply_to_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_telegram_messages_reply_to",
        "telegram_messages",
        "telegram_messages",
        ["reply_to_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Ключ снимается ПЕРВЫМ: MySQL не даст убрать колонку, на которой он висит,
    # и откат встал бы на ровном месте — а откат здесь главный способ починки.
    op.drop_constraint("fk_telegram_messages_reply_to", "telegram_messages", type_="foreignkey")
    op.drop_column("telegram_messages", "reply_to_id")
