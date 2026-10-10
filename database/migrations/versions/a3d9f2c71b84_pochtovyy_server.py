"""Свой почтовый сервер: серверы, домены, работы мастера, псевдонимы, доступ к ящикам.

Задание владельца от 09.10.2026 (docs/bloki/32-pochtovyy-server.md). Таблицы
новые, а у `mail_accounts` и `user_tokens` только добавляются колонки с
умолчаниями: старые ящики остаются общими (их и раньше видели все с
`mail.view`), старые токены — на все ящики сотрудника.

Право `mail.manage` получают роли, которые уже правили ящики через
`settings.manage`: вчера они это умели, и завтра умение не должно пропасть.

Архив писем в CRM (`mail_archive`, `mail_archive_folders`) — таблицы новые;
`mail_accounts.archive` с умолчанием 0: прежние ящики все внешние, а их копию
заводят только по выбору.

Revision ID: a3d9f2c71b84
Revises: f1c4a7e9d253
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.types import ExactString, LongText

revision: str = "a3d9f2c71b84"
down_revision: Union[str, None] = "f1c4a7e9d253"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PUSTO = sa.text("('')")


def upgrade() -> None:
    op.create_table(
        "mail_servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("public_ip", sa.String(length=45), nullable=False),
        sa.Column("ssh_host", sa.String(length=255), nullable=False),
        sa.Column("ssh_port", sa.Integer(), nullable=False),
        sa.Column("ssh_user", sa.String(length=64), nullable=False),
        sa.Column("ssh_key_encrypted", sa.Text(), nullable=True),
        sa.Column("ssh_public_key", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("ssh_host_key", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("sudo_password_encrypted", sa.Text(), nullable=True),
        sa.Column("docker_cmd", sa.String(length=32), nullable=False),
        sa.Column("base_dir", sa.String(length=255), nullable=False),
        sa.Column("container", sa.String(length=64), nullable=False),
        sa.Column("image", sa.String(length=200), nullable=False),
        sa.Column("imap_host", sa.String(length=255), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("tls_pin", sa.String(length=64), nullable=False),
        sa.Column("master_user", sa.String(length=64), nullable=False),
        sa.Column("master_password_encrypted", sa.Text(), nullable=True),
        sa.Column("agent_token_encrypted", sa.Text(), nullable=True),
        sa.Column("relay_host", sa.String(length=255), nullable=False),
        sa.Column("relay_port", sa.Integer(), nullable=False),
        sa.Column("relay_user", sa.String(length=320), nullable=False),
        sa.Column("relay_password_encrypted", sa.Text(), nullable=True),
        sa.Column("installed_at", sa.DateTime(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mail_domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("dkim_selector", sa.String(length=63), nullable=False),
        sa.Column("dkim_txt", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("dns_report", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("dns_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mail_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain"),
    )
    op.create_index(op.f("ix_mail_domains_server_id"), "mail_domains", ["server_id"])
    op.create_table(
        "mail_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("log", LongText, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("error_detail", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["mail_servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mail_jobs_server_id"), "mail_jobs", ["server_id"])
    op.create_index(op.f("ix_mail_jobs_actor_id"), "mail_jobs", ["actor_id"])
    op.create_table(
        "mail_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=320), nullable=False),
        sa.Column("destination", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mail_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "source", "destination", name="uq_mail_aliases_pair"),
    )
    op.create_index(op.f("ix_mail_aliases_server_id"), "mail_aliases", ["server_id"])

    with op.batch_alter_table("mail_accounts") as batch:
        batch.add_column(sa.Column("server_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("is_shared", sa.Boolean(), server_default="1", nullable=False))
        batch.add_column(sa.Column("quota_mb", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("inbox_uidvalidity", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("inbox_uidnext", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("unseen_count", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("status_checked_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_mail_accounts_server_id", "mail_servers", ["server_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index(op.f("ix_mail_accounts_server_id"), "mail_accounts", ["server_id"])

    op.create_table(
        "mail_box_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("can_send", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "user_id", name="uq_mail_box_access"),
    )
    op.create_index(op.f("ix_mail_box_access_user_id"), "mail_box_access", ["user_id"])
    op.create_index(op.f("ix_mail_box_access_granted_by"), "mail_box_access", ["granted_by"])

    with op.batch_alter_table("user_tokens") as batch:
        batch.add_column(
            sa.Column("mail_scope", sa.String(length=8), server_default="all", nullable=False)
        )
    op.create_table(
        "user_token_mail_boxes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["token_id"], ["user_tokens.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_id", "account_id", name="uq_user_token_mail_box"),
    )
    op.create_index(
        op.f("ix_user_token_mail_boxes_account_id"), "user_token_mail_boxes", ["account_id"]
    )

    with op.batch_alter_table("mail_accounts") as batch:
        batch.add_column(sa.Column("archive", sa.Boolean(), server_default="0", nullable=False))
    op.create_table(
        "mail_archive_folders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("folder", ExactString(500), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=False),
        sa.Column("last_uid", sa.Integer(), nullable=False),
        sa.Column("uidnext", sa.Integer(), server_default="0", nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "folder", name="uq_mail_archive_folders_box_folder"),
    )
    op.create_table(
        "mail_archive",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("folder", ExactString(500), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("message_id", ExactString(320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("from_addr", sa.String(length=320), nullable=False),
        sa.Column("to_addrs", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("cc_addrs", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("flags", sa.String(length=200), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), nullable=False),
        sa.Column("attachments", sa.Text(), server_default=PUSTO, nullable=False),
        sa.Column("body_text", LongText, nullable=False),
        sa.Column("body_html", LongText, nullable=False),
        sa.Column("raw_path", sa.String(length=500), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "folder", "uidvalidity", "uid", name="uq_mail_archive_box_folder_uid"
        ),
    )
    op.create_index(op.f("ix_mail_archive_from_addr"), "mail_archive", ["from_addr"])
    op.create_index(
        "ix_mail_archive_box_folder_received", "mail_archive", ["account_id", "folder", "received_at"]
    )
    op.create_index("ix_mail_archive_box_sent", "mail_archive", ["account_id", "sent_at"])
    op.create_index("ix_mail_archive_message_id", "mail_archive", ["message_id"])

    # Кто правил ящики через `settings.manage`, тот управляет почтой и дальше.
    op.execute(
        "INSERT INTO role_permissions (role_id, area, action) "
        "SELECT role_id, 'mail', 'manage' FROM role_permissions "
        "WHERE area = 'settings' AND action = 'manage'"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE area = 'mail' AND action = 'manage'")
    op.drop_table("mail_archive")
    op.drop_table("mail_archive_folders")
    with op.batch_alter_table("mail_accounts") as batch:
        batch.drop_column("archive")
    op.drop_table("user_token_mail_boxes")
    with op.batch_alter_table("user_tokens") as batch:
        batch.drop_column("mail_scope")
    op.drop_table("mail_box_access")
    with op.batch_alter_table("mail_accounts") as batch:
        batch.drop_constraint("fk_mail_accounts_server_id", type_="foreignkey")
    op.drop_index(op.f("ix_mail_accounts_server_id"), table_name="mail_accounts")
    with op.batch_alter_table("mail_accounts") as batch:
        batch.drop_column("status_checked_at")
        batch.drop_column("unseen_count")
        batch.drop_column("inbox_uidnext")
        batch.drop_column("inbox_uidvalidity")
        batch.drop_column("quota_mb")
        batch.drop_column("is_shared")
        batch.drop_column("server_id")
    op.drop_table("mail_aliases")
    op.drop_table("mail_jobs")
    op.drop_table("mail_domains")
    op.drop_table("mail_servers")
