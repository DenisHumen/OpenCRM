"""Телеграм: аватар собеседника и признак премиума.

Две колонки, обе на `telegram_chats`:

1. `avatar_path` — где лежит забранная фотография профиля. Само изображение на
   диске рядом с остальными файлами канала (`storage/telegram/<chat_id>/`), в
   базе только путь. Тот же приём, что у вложений: гонять картинки через строки
   базы — верный способ раздуть её и замедлить всё разом.
2. `avatar_checked_at` — когда в последний раз СПРАШИВАЛИ у телеграма. Именно
   спрашивали, а не получили: у половины людей аватара нет вовсе (не поставили
   или закрыли настройками приватности), и без отметки «спросили и не нашли» мы
   ходили бы за ним снова и снова на каждый показ списка. Телеграм за такое
   ограничивает.

Порядок для населённой таблицы соблюдён: `avatar_path` ложится
`NOT NULL server_default ""` — значение существующим строкам подставляет сама
база, отдельного `UPDATE` не нужно. `avatar_checked_at` пустая: «никогда не
спрашивали» — это законное состояние, и подставлять вместо него дату значило бы
соврать, что аватар уже искали.

Индекса нет намеренно: колонки читаются вместе со строкой диалога, отбора по
ним не бывает.

Revision ID: a4e8d21f56b0
Revises: d5a3f81c9e27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4e8d21f56b0"
down_revision: Union[str, None] = "d5a3f81c9e27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_chats",
        sa.Column("avatar_path", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "telegram_chats",
        sa.Column("avatar_checked_at", sa.DateTime(), nullable=True),
    )
    # Премиум приходит вместе с сообщением и ничего не стоит. `server_default`
    # «0» — то же правило населённой таблицы: значение существующим строкам
    # подставляет база, отдельного `UPDATE` не нужно.
    op.add_column(
        "telegram_chats",
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # Откат — главный способ починки на боевом сервере, поэтому он обязан
    # работать. Снятые колонки означают потерю только пути к файлу: сами
    # картинки остаются на диске и найдутся заново при первом же обращении.
    op.drop_column("telegram_chats", "is_premium")
    op.drop_column("telegram_chats", "avatar_checked_at")
    op.drop_column("telegram_chats", "avatar_path")
