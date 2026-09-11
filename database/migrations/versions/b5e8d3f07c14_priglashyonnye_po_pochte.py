"""Приглашённые по почте: список у ссылки и почта в журнале открытий.

Колонка `guest_email` добавляется nullable-с-умолчанием и заполняется пустой
строкой: у всех существующих открытий гостя не было и взяться ему неоткуда —
круг «по ссылке» и «по коду» анонимен по устройству.

Revision ID: b5e8d3f07c14
Revises: c7a2f14b9e05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5e8d3f07c14"
down_revision: Union[str, None] = "c7a2f14b9e05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "file_link_guests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["link_id"], ["file_links.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("link_id", "email", name="uq_file_link_guests_link_email"),
    )
    op.create_index(op.f("ix_file_link_guests_link_id"), "file_link_guests", ["link_id"])

    # Порядок для населённой базы: сначала nullable, потом заполнить одним
    # UPDATE, потом NOT NULL. На живом сервере открытий у ссылок уже много.
    with op.batch_alter_table("file_link_views") as batch:
        batch.add_column(sa.Column("guest_email", sa.String(length=200), nullable=True))
    op.execute("UPDATE file_link_views SET guest_email = '' WHERE guest_email IS NULL")
    with op.batch_alter_table("file_link_views") as batch:
        batch.alter_column(
            "guest_email",
            existing_type=sa.String(length=200),
            nullable=False,
            server_default="",
        )


def downgrade() -> None:
    with op.batch_alter_table("file_link_views") as batch:
        batch.drop_column("guest_email")
    # Только таблица: индекс уходит вместе с ней, а снять его отдельно MySQL не
    # даёт — он нужен внешнему ключу, который на нём же и стоит.
    op.drop_table("file_link_guests")
