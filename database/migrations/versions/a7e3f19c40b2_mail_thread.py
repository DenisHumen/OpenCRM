"""Цепочка переписки: `in_reply_to` и `references` у письма.

Две колонки и только они — населённых данных эта правка не трогает, поэтому
опасного места «добавить `NOT NULL` на живых строках» здесь нет. Порядок из
CLAUDE.md соблюдён тем, что шаг ровно один: добавить.

**Почему не `NOT NULL` с умолчанием, а `server_default`.** Строки в
`mail_messages` уже есть — вся синхронизированная переписка, — и добавить
колонку без умолчания на населённой таблице нельзя. Пустая строка здесь
означает «письмо ни на что не отвечает», и это честное значение для всего, что
приехало до этой правки: у старых писем цепочка не сохранялась, и выдумывать её
задним числом неоткуда.

Индексов нет намеренно. По этим колонкам не ищут: они уезжают в заголовки
исходящего письма, и читают их ровно по одному разу — когда человек нажимает
«ответить». Индекс на колонке, по которой не ищут, стоит места и времени записи
и не даёт ничего.

`downgrade` снимает обе колонки. Данные при этом теряются, и это неизбежно: их
негде хранить, а сама переписка остаётся на месте — цепочка лишь подсказка
почтовому клиенту собеседника.

Revision ID: a7e3f19c40b2
Revises: f3c72a19d5e8
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import text

#: Умолчание для TEXT: MySQL запрещает обычный DEFAULT у TEXT/BLOB (ошибка 1101),
#: а форму DEFAULT ('') принимает. Скобки понимают оба движка.
#: Подробности — database/types.text_default.
PUSTO = text("('')")

revision: str = "a7e3f19c40b2"
down_revision: Union[str, None] = "f3c72a19d5e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mail_messages",
        sa.Column("in_reply_to", sa.String(length=320), nullable=False, server_default=""),
    )
    op.add_column(
        "mail_messages",
        sa.Column("references", sa.Text(), nullable=False, server_default=PUSTO),
    )


def downgrade() -> None:
    op.drop_column("mail_messages", "references")
    op.drop_column("mail_messages", "in_reply_to")
