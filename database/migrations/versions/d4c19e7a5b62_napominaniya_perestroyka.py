"""Перестройка «Напоминаний»: люди, повторы, звонки, история, шаги, ссылки, привязки.

Владелец 28.09.2026 (docs/bloki/29-napominaniya.md). Населённая база, поэтому
порядок из CONTRIBUTING.md §1: таблицы → перенос людей → колонки nullable → одним
UPDATE → NOT NULL и ключи → индексы.

Единственный исполнитель (`assignee_id`) уходит в `task_members`: у поставившего
строка владельца, у исполнителя — получателя. Напоминания без автора (заявка с
сайта, пропущенный звонок) встают на общую полку — их видели все и видят дальше.
Остальные становятся личными: так просил владелец.

Revision ID: d4c19e7a5b62
Revises: b5e8d3f07c14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d4c19e7a5b62"
down_revision: Union[str, None] = "b5e8d3f07c14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POYAS = "Europe/Kyiv"
NOVYE_KLYUCHI = (
    ("document_id", "documents", "fk_tasks_document_id"),
    ("product_id", "products", "fk_tasks_product_id"),
    ("board_id", "boards", "fk_tasks_board_id"),
)


def _klyuch_na(tablitsa: str, kolonka: str) -> str | None:
    """Имя внешнего ключа по колонке: у ключей из первой миграции оно автоматическое."""
    for kluch in inspect(op.get_bind()).get_foreign_keys(tablitsa):
        if kluch["constrained_columns"] == [kolonka]:
            return kluch["name"]
    return None


def upgrade() -> None:
    # 1. Таблицы.
    op.create_table(
        "task_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("vladelets", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("poluchaet", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("otlozheno_do", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "user_id", name="uq_task_members_task_user"),
    )
    op.create_index(op.f("ix_task_members_user_id"), "task_members", ["user_id"])

    op.create_table(
        "task_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("moment", sa.DateTime(), nullable=False),
        sa.Column("srok", sa.DateTime(), nullable=False),
        sa.Column("vid", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("prinyato", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "user_id", "moment", name="uq_task_signals_task_user_moment"),
    )
    op.create_index("ix_task_signals_user_prinyato", "task_signals", ["user_id", "prinyato"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("vid", sa.String(length=16), nullable=False),
        sa.Column("srok", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_events_task_created", "task_events", ["task_id", "created_at"])

    op.create_table(
        "task_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("sdelan_at", sa.DateTime(), nullable=True),
        sa.Column("poryadok", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_steps_task_id"), "task_steps", ["task_id"])

    op.create_table(
        "task_urls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("title", sa.String(length=200), server_default="", nullable=False),
        sa.Column("poryadok", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_urls_task_id"), "task_urls", ["task_id"])

    # 2. Люди из единственного исполнителя: владелец — автор, получатель — исполнитель.
    op.execute(
        "INSERT INTO task_members (task_id, user_id, vladelets, poluchaet) "
        "SELECT id, created_by, 1, "
        "CASE WHEN assignee_id IS NULL OR assignee_id = created_by THEN 1 ELSE 0 END "
        "FROM tasks WHERE created_by IS NOT NULL"
    )
    op.execute(
        "INSERT INTO task_members (task_id, user_id, vladelets, poluchaet) "
        "SELECT id, assignee_id, 0, 1 FROM tasks "
        "WHERE assignee_id IS NOT NULL AND (created_by IS NULL OR assignee_id <> created_by)"
    )
    # Право «ставить другим» — тем, кто уже умел заводить напоминания: вчера у них
    # было поле «исполнитель», и завтра оно не должно пропасть.
    op.execute(
        "INSERT INTO role_permissions (role_id, area, action) "
        "SELECT role_id, 'tasks', 'assign' FROM role_permissions "
        "WHERE area = 'tasks' AND action = 'create'"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, area, action) "
        "SELECT id, 'tasks', 'view_others' FROM roles WHERE preset = 'director'"
    )

    # 3. Колонки — nullable.
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("ves_den", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("poyas", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("povtor", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("povtor_nachalo", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("povtor_posle", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("sdelano_raz", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("opovesheniya", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("nastoychivo", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("obshchee", sa.Boolean(), nullable=True))
        for kolonka, _tablitsa, _imya in NOVYE_KLYUCHI:
            batch.add_column(sa.Column(kolonka, sa.Integer(), nullable=True))

    # 4. Заполнить одним UPDATE. Звонок «в срок» получают и старые: для будущих
    #    сроков это и есть новое, а прошедшие не зазвонят — планировщик смотрит
    #    только последние минуты.
    op.execute(
        f"UPDATE tasks SET ves_den = 0, poyas = '{POYAS}', povtor_posle = 0, sdelano_raz = 0, "
        "opovesheniya = '0', obshchee = CASE WHEN created_by IS NULL THEN 1 ELSE 0 END"
    )

    # 5. NOT NULL, ключи; единственный исполнитель уходит.
    kluch_ispolnitelya = _klyuch_na("tasks", "assignee_id")
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("ves_den", existing_type=sa.Boolean(), nullable=False, server_default="0")
        batch.alter_column("poyas", existing_type=sa.String(length=64), nullable=False, server_default=POYAS)
        batch.alter_column("povtor_posle", existing_type=sa.Boolean(), nullable=False, server_default="0")
        batch.alter_column("sdelano_raz", existing_type=sa.Integer(), nullable=False, server_default="0")
        batch.alter_column("opovesheniya", existing_type=sa.String(length=64), nullable=False, server_default="0")
        batch.alter_column("obshchee", existing_type=sa.Boolean(), nullable=False, server_default="0")
        for kolonka, tablitsa, imya in NOVYE_KLYUCHI:
            batch.create_foreign_key(imya, tablitsa, [kolonka], ["id"], ondelete="SET NULL")
        if kluch_ispolnitelya:
            batch.drop_constraint(kluch_ispolnitelya, type_="foreignkey")
        batch.drop_index("ix_tasks_assignee_id")
        batch.drop_column("assignee_id")

    # 6. Индексы — последними, по заполненным данным.
    for kolonka, _tablitsa, _imya in NOVYE_KLYUCHI:
        op.create_index(op.f(f"ix_tasks_{kolonka}"), "tasks", [kolonka])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("assignee_id", sa.Integer(), nullable=True))
    # Исполнитель — чужой получатель, иначе сам владелец, если звонило ему: прежний
    # `create` без исполнителя ставил им автора, и откат обязан это вернуть.
    op.execute(
        "UPDATE tasks SET assignee_id = COALESCE("
        "(SELECT MIN(m.user_id) FROM task_members m "
        "WHERE m.task_id = tasks.id AND m.poluchaet = 1 AND m.vladelets = 0), "
        "(SELECT MIN(m.user_id) FROM task_members m "
        "WHERE m.task_id = tasks.id AND m.poluchaet = 1))"
    )
    with op.batch_alter_table("tasks") as batch:
        batch.create_foreign_key(
            "fk_tasks_assignee_id", "users", ["assignee_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_tasks_assignee_id", ["assignee_id"])
        for kolonka, _tablitsa, imya in NOVYE_KLYUCHI:
            batch.drop_constraint(imya, type_="foreignkey")
        # Индексы под ключами уходят вместе с колонками: MySQL не даёт снять индекс,
        # пока на нём стоит ключ, а ключ уже снят строкой выше.
        for kolonka, _tablitsa, _imya in NOVYE_KLYUCHI:
            batch.drop_index(f"ix_tasks_{kolonka}")
            batch.drop_column(kolonka)
        for kolonka in (
            "obshchee", "nastoychivo", "opovesheniya", "sdelano_raz", "povtor_posle",
            "povtor_nachalo", "povtor", "poyas", "ves_den",
        ):
            batch.drop_column(kolonka)

    op.execute("DELETE FROM role_permissions WHERE area = 'tasks' AND action IN ('assign', 'view_others')")
    for tablitsa in ("task_urls", "task_steps", "task_events", "task_signals", "task_members"):
        op.drop_table(tablitsa)
