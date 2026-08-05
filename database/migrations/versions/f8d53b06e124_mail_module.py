"""mail: accounts and messages

Revision ID: f8d53b06e124
Revises: b81f6a2d40c7
Create Date: 2026-08-06 10:00:00.000000

Блок «Почта»: ящики фирмы и зеркало переписки.

Своей истории общения блок не заводит: письмо, попав в систему, порождает запись
в общей ленте (`client_notes`, `kind='email'`) — ровно то, подо что ревизия
b81f6a2d40c7 ленту и обобщила. Поэтому здесь только две новые таблицы и ни одной
колонки в чужих.

Пароль ящика хранится зашифрованным (core/security/secretbox.py), поэтому колонка
называется `password_encrypted`, а не `password`: имя должно мешать положить туда
открытый текст.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8d53b06e124"
down_revision: Union[str, None] = "e7c42a95d013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("imap_host", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("smtp_host", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="465"),
        sa.Column("smtp_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("login", sa.String(length=320), nullable=False, server_default=""),
        # NULL — пароль не задан. Пустая строка значила бы «пароль есть и он пустой».
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("mail_accounts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mail_accounts_address"), ["address"], unique=False)

    op.create_table(
        "mail_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        # NULL у исходящих: их UID на сервере входящей почты не существует.
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.String(length=320), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_html", sa.Text(), nullable=False, server_default=""),
        sa.Column("from_addr", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("to_addrs", sa.Text(), nullable=False, server_default=""),
        # Момент отправки письма (UTC), а не синхронизации — по нему письмо встаёт
        # в ленту рядом со звонками и встречами того же дня.
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("deal_id", sa.Integer(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # Ящик удалили — его зеркало уезжает следом: обновлять его больше нечем.
        # История общения при этом остаётся, она записана в client_notes.
        sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="CASCADE"),
        # Клиента или заявку вычистили — письмо остаётся, но теряет привязку.
        # Переписка это документ фирмы, исчезать вместе с карточкой она не должна.
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="SET NULL"),
    )
    with op.batch_alter_table("mail_messages", schema=None) as batch_op:
        # Уникальный — на нём держится идемпотентность синхронизации.
        batch_op.create_index(
            batch_op.f("ix_mail_messages_message_id"), ["message_id"], unique=True
        )
        batch_op.create_index(batch_op.f("ix_mail_messages_account_id"), ["account_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_mail_messages_client_id"), ["client_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_mail_messages_deal_id"), ["deal_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_mail_messages_sent_at"), ["sent_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_mail_messages_from_addr"), ["from_addr"], unique=False)
        batch_op.create_index("ix_mail_messages_account_uid", ["account_id", "uid"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("mail_messages", schema=None) as batch_op:
        batch_op.drop_index("ix_mail_messages_account_uid")
        batch_op.drop_index(batch_op.f("ix_mail_messages_from_addr"))
        batch_op.drop_index(batch_op.f("ix_mail_messages_sent_at"))
        batch_op.drop_index(batch_op.f("ix_mail_messages_deal_id"))
        batch_op.drop_index(batch_op.f("ix_mail_messages_client_id"))
        batch_op.drop_index(batch_op.f("ix_mail_messages_account_id"))
        batch_op.drop_index(batch_op.f("ix_mail_messages_message_id"))
    op.drop_table("mail_messages")

    with op.batch_alter_table("mail_accounts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_mail_accounts_address"))
    op.drop_table("mail_accounts")

    # Состояние блока чистим тоже: иначе после отката и повторного наката почта
    # молча осталась бы «включённой» при пустых таблицах.
    op.execute("DELETE FROM module_states WHERE key = 'mail'")

    # Записи ленты, порождённые письмами, НЕ трогаем. Лента — общая история
    # общения, а не собственность блока: откатывая почту, мы убираем зеркало
    # переписки, а не сам факт, что клиенту тогда написали.
