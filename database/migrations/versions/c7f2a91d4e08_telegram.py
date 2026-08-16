"""Переписка с клиентами через бота фирмы: диалоги и сообщения.

Только новые таблицы — населённых колонок эта ревизия не трогает, поэтому
опасного места «добавить NOT NULL на живых строках» здесь нет вовсе. Порядок
всё равно соблюдён: таблицы, потом внешние ключи внутри них, индексы —
последними.

Откат снимает обе таблицы. Данные при этом теряются, и это тот случай, когда
иначе нельзя: колонок, куда их сложить, на прежней схеме не существует. Зато
откат РАБОТАЕТ, а это по правилам проекта главный способ починки на боевом
сервере. Внешние ключи снимаются вместе с таблицами (`drop_table`), отдельно их
не трогаем: на MySQL снятие индекса под живым внешним ключом отвергается
ошибкой 1553 — этот урок в проекте уже оплачен в откате `e4451c527c34`.

Revision ID: c7f2a91d4e08
Revises: b3f18d5a2e47
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7f2a91d4e08"
down_revision: Union[str, None] = "b3f18d5a2e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_chats",
        sa.Column("id", sa.Integer(), nullable=False),
        # BigInteger, а не Integer: телеграм давно выдаёт идентификаторы больше
        # двух миллиардов. Ошибка ровно того класса, что уже ловилась в
        # `tests/test_potolki.py`, только здесь она стоила бы приёма сообщений.
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("phone", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("phone_norm", sa.String(length=32), nullable=False, server_default=""),
        # Пусто — законное состояние: привязка по неточному совпадению запрещена.
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_telegram_chats_chat_id"), "telegram_chats", ["chat_id"], unique=True
    )
    op.create_index(
        op.f("ix_telegram_chats_phone_norm"), "telegram_chats", ["phone_norm"], unique=False
    )
    op.create_index(
        op.f("ix_telegram_chats_client_id"), "telegram_chats", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_telegram_chats_last_message_at"),
        "telegram_chats",
        ["last_message_at"],
        unique=False,
    )

    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        # Пусто у исходящего до успешной отправки: строка заводится ДО обращения
        # к телеграму, чтобы двойное нажатие не отправило дважды.
        sa.Column("external_id", sa.BigInteger(), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="text"),
        # Text, а не String: предел телеграма 4096 знаков, но подпись к файлу
        # считается отдельно, и упереться в потолок колонки значило бы потерять
        # хвост чужого сообщения.
        sa.Column("body", sa.Text(), nullable=False),
        # `file_id` — чем забрать файл у телеграма позже. Нужен для видео:
        # сразу его не тянем, но и терять возможность посмотреть нельзя.
        sa.Column("file_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("file_path", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("file_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("send_state", sa.String(length=16), nullable=False, server_default="sent"),
        sa.Column("send_error", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("happened_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["telegram_chats.id"], ondelete="CASCADE"),
        # SET NULL, а не каскад: уволенный менеджер не уносит с собой переписку.
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_telegram_messages_chat_id"), "telegram_messages", ["chat_id"], unique=False
    )
    op.create_index(
        op.f("ix_telegram_messages_external_id"),
        "telegram_messages",
        ["external_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_messages_direction"),
        "telegram_messages",
        ["direction"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_messages_happened_at"),
        "telegram_messages",
        ["happened_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_messages_chat_time",
        "telegram_messages",
        ["chat_id", "happened_at"],
        unique=False,
    )


def downgrade() -> None:
    # Порядок обратный: сначала сообщения (они ссылаются на диалоги), потом
    # диалоги. Индексы уносит `drop_table` — снимать их отдельно на MySQL
    # нельзя, пока по ним проверяется внешний ключ (ошибка 1553).
    op.drop_table("telegram_messages")
    op.drop_table("telegram_chats")
