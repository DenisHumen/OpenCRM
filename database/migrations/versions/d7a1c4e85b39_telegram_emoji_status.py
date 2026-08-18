"""Телеграм: именной эмодзи собеседника.

Одна колонка: `telegram_chats.emoji_status_path` — путь к забранной картинке
именного эмодзи (того самого значка, который премиум-пользователи ставят рядом
со своим именем, включая значки из платных наборов).

**Почему картинка, а не идентификатор.** Телеграм отдаёт `custom_emoji_id`, за
которым стоит стикер в формате TGS (сжатый Lottie) или WEBM. Ни то, ни другое
браузер сам не рисует — понадобилась бы библиотека проигрывания во фронтенде,
то есть новая зависимость ради украшения.

У стикера при этом есть СТАТИЧНАЯ миниатюра, и её браузер рисует обычной
картинкой. Значок виден, набор — тот самый, зависимости нет. Анимация остаётся
отдельным решением владельца и ничего в этой схеме не ломает: путь к анимации
ляжет рядом, когда (и если) её захотят.

Порядок для населённой таблицы соблюдён: `NOT NULL server_default ""`, значение
существующим строкам подставляет сама база.

Revision ID: d7a1c4e85b39
Revises: c5b7e93a1d64
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7a1c4e85b39"
down_revision: Union[str, None] = "c5b7e93a1d64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_chats",
        sa.Column("emoji_status_path", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("telegram_chats", "emoji_status_path")
