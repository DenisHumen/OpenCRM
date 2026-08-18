"""Телеграм: индекс под счёт непрочитанного вместо одиночного по направлению.

**Замерено, а не предположено.** На таблице в 20 000 сообщений (50 диалогов по
400) счёт непрочитанного для страницы списка шёл так:

    было:  type=ref   key=ix_telegram_messages_direction  rows=10000
           Extra: Using where; Using temporary
    стало: type=range key=ix_telegram_messages_napravlenie rows=10025
           Extra: Using where; Using index

Разница не в оценке строк — MySQL грубо считает её для полусотни диапазонов
`OR`, — а в способе. Было: чтение ВСЕХ входящих строк таблицы плюс временная
таблица под группировку. Стало: покрывающий проход по индексу (`Using index`),
то есть к самой таблице обращений нет вовсе, и временная таблица не нужна.

Запрос этот идёт на КАЖДОЕ обновление списка диалогов у КАЖДОГО открытого
экрана, то есть несколько раз в минуту постоянно. На переписке в миллион строк
прежний план читал бы полмиллиона.

**Одиночный индекс по `direction` при этом снимается, и это не потеря.** У
колонки два значения («in» и «out»), то есть он делит таблицу пополам и почти
никогда не полезен — а MySQL, как показал замер, именно на него и сваливался,
предпочитая его составному. Всё, что он мог обслужить, обслуживает новый: он
начинается с той же колонки, и префикс работает так же.

Порядок для населённой таблицы: индекс создаётся ПЕРВЫМ, снимается старый
вторым. Обратный порядок оставил бы окно, в котором запросы по направлению не
опираются ни на что.

Revision ID: e2f6b81d3c47
Revises: d7a1c4e85b39
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2f6b81d3c47"
down_revision: Union[str, None] = "d7a1c4e85b39"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _est_indeks(imya: str) -> bool:
    """Есть ли такой индекс. Спрашиваем, а не надеемся.

    MySQL отвечает ошибкой и на создание существующего (1061), и на снятие
    отсутствующего (1091), а обе беды роняют обновление на ровном месте. На
    установках, куда правка приезжала из промежуточной редакции, состояние
    бывает любым.
    """
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'telegram_messages' "
                "AND index_name = :imya LIMIT 1"
            ),
            {"imya": imya},
        ).first()
    )


def upgrade() -> None:
    if not _est_indeks("ix_telegram_messages_napravlenie"):
        op.create_index(
            "ix_telegram_messages_napravlenie",
            "telegram_messages",
            ["direction", "chat_id", "id"],
        )
    if _est_indeks("ix_telegram_messages_direction"):
        op.drop_index("ix_telegram_messages_direction", table_name="telegram_messages")


def downgrade() -> None:
    if not _est_indeks("ix_telegram_messages_direction"):
        op.create_index(
            "ix_telegram_messages_direction", "telegram_messages", ["direction"]
        )
    if _est_indeks("ix_telegram_messages_napravlenie"):
        op.drop_index("ix_telegram_messages_napravlenie", table_name="telegram_messages")
