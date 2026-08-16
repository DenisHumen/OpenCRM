"""Телеграм: `file_id` у сообщения и снятие задвоенного индекса.

**Почему отдельной ревизией, а не правкой соседней.** Обе эти вещи были
написаны прямо в `c7f2a91d4e08` — уже применённой на боевом сервере. Alembic
применённую ревизию заново не гоняет: колонка в базе не появилась, модель её
требовала, сверка схемы отказалась поднимать приложение, и обновление
откатилось. Проверка сработала ровно так, как задумана, — но стоило это
одиннадцати минут простоя и отката базы.

Урок записан правилом номер один и стоит его повторить: **выгруженную миграцию
не правят никогда**. Она может быть уже накатана где угодно, и там правка
означает не «исправлено», а «расхождение, о котором никто не узнает до
следующего запроса».

Что делает эта ревизия:

1. добавляет `telegram_messages.file_id` — чем забрать файл у телеграма позже.
   Нужен для видео: сразу его не тянем (переписка с видео съест диск за
   недели), но и терять возможность посмотреть нельзя. Ссылка телеграма живёт
   около часа, а `file_id` постоянен;
2. снимает `ix_telegram_chats_last_message` — он повторяет
   `ix_telegram_chats_last_message_at`, созданный из `index=True` на самой
   колонке. Две одинаковые записи на одной колонке — лишний вес при каждой
   вставке и лишний выбор планировщику.

Порядок соблюдён: колонка добавляется **nullable-совместимо** (`server_default`
пустой строкой), поэтому на населённой таблице она ложится без единого
`UPDATE`. Индекс снимается последним.

Revision ID: d5a3f81c9e27
Revises: c7f2a91d4e08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5a3f81c9e27"
down_revision: Union[str, None] = "c7f2a91d4e08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `server_default=""` и `nullable=False` вместе — единственный способ
    # добавить непустую колонку на населённой таблице одним шагом: значение
    # для существующих строк берёт сама база, отдельного `UPDATE` не нужно.
    op.add_column(
        "telegram_messages",
        sa.Column("file_id", sa.String(length=200), nullable=False, server_default=""),
    )

    # Индекс мог не существовать: на установках, где `c7f2a91d4e08` накатывалась
    # из ПРАВЛЕНОЙ редакции (между двумя выгрузками), его не создавали. Снимать
    # несуществующий индекс MySQL отказывается ошибкой 1091, и она уронила бы
    # обновление на ровном месте — поэтому сначала спрашиваем, есть ли он.
    bind = op.get_bind()
    est = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'telegram_chats' "
            "AND index_name = 'ix_telegram_chats_last_message'"
        )
    ).scalar()
    if est:
        op.drop_index("ix_telegram_chats_last_message", table_name="telegram_chats")


def downgrade() -> None:
    # Порядок обратный: сначала вернуть индекс, потом снять колонку.
    #
    # Индекс возвращаем тоже с оглядкой: откат могли запустить на установке, где
    # его никогда не было, — тогда создание упало бы на «duplicate key name»
    # ровно так же, как снятие падало бы на его отсутствии.
    bind = op.get_bind()
    est = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'telegram_chats' "
            "AND index_name = 'ix_telegram_chats_last_message'"
        )
    ).scalar()
    if not est:
        op.create_index(
            "ix_telegram_chats_last_message",
            "telegram_chats",
            ["last_message_at"],
            unique=False,
        )

    op.drop_column("telegram_messages", "file_id")
