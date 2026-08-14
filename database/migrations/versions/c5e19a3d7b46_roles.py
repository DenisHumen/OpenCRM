"""роли и права

Revision ID: c5e19a3d7b46
Revises: b2c48f7a91d5

Ролей было две, и обе жили в коде: root и менеджер. Бухгалтеру нужны финансы и
не нужны доски; проджекту — заявки и склад, но не суммы. Двумя ролями это не
описывается, а дописывать третью в код на каждую должность — не выход.

Здесь появляются сами роли (`roles`), их права (`role_permissions`) и ссылка на
роль у сотрудника (`users.role_id`). Перечень существующих прав остаётся в коде
(`core/permissions.py`) — в базе только то, кому они выданы.

**Никто не должен потерять доступ.** Поэтому миграция не просто заводит
таблицы: она кладёт роль «Менеджер» с набором прав, ровно повторяющим то, что
умел `require_staff` до этой правки, и назначает её всем существующим
сотрудникам. Root не получает роли вовсе — у него все права всегда.

Набор прав здесь выписан списком, а не взят из `permissions_service.PRESETS`.
Миграция — снимок прошлого: пресет завтра поправят под новый блок, и накат на
старой базе выдал бы людям не то, что у них было. Совпадение поведения (а не
списков) проверяет `tests/test_roles.py`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e19a3d7b46"
down_revision: Union[str, None] = "d7f26b48c093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Права менеджера на момент этой миграции — то же, что открывал `require_staff`.
MANAGER_PERMISSIONS: tuple[tuple[str, str], ...] = tuple(
    [
        (area, action)
        for area in ("clients", "deals", "boards", "tasks", "documents", "warehouse")
        for action in ("view", "create", "edit", "delete")
    ]
    + [(area, "view") for area in ("companies", "reports", "mail", "telephony")]
    + [
        ("deals", "move_stage"),
        ("deals", "view_others"),
        ("deals", "view_amounts"),
        ("documents", "issue"),
        ("warehouse", "restore"),
        ("warehouse", "view_amounts"),
        ("reports", "view_amounts"),
        ("mail", "create"),
        ("telephony", "create"),
        ("telephony", "edit"),
    ]
)


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("preset", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("area", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_id", "area", "action", name="uq_role_permission"),
    )
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"])
    op.create_index("ix_role_permissions_area", "role_permissions", ["area"])

    # batch_alter_table: SQLite не умеет ADD COLUMN с внешним ключом иначе как
    # пересозданием таблицы, и alembic делает это за нас.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_users_role_id", "roles", ["role_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_users_role_id", "users", ["role_id"])

    _seed_manager_role()


def _seed_manager_role() -> None:
    """Роль «Менеджер» и переезд на неё всех, кто не root.

    Через `op.get_bind()`, а не через модели: модель завтра обрастёт полями,
    которых в этой ревизии ещё нет, и миграция сломается на базе, которую как
    раз и должна была починить.
    """
    bind = op.get_bind()

    roles = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("preset", sa.String),
        sa.column("is_default", sa.Boolean),
    )
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("area", sa.String),
        sa.column("action", sa.String),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("role", sa.String),
        sa.column("role_id", sa.Integer),
    )

    bind.execute(
        roles.insert().values(name="Менеджер", preset="manager", is_default=True)
    )
    role_id = bind.execute(
        sa.select(roles.c.id).where(roles.c.name == "Менеджер")
    ).scalar_one()

    bind.execute(
        role_permissions.insert(),
        [
            {"role_id": role_id, "area": area, "action": action}
            for area, action in MANAGER_PERMISSIONS
        ],
    )

    # Все, кроме root: у него роли не бывает, права и так все.
    bind.execute(users.update().where(users.c.role != "root").values(role_id=role_id))


def downgrade() -> None:
    """Откат обязан быть рабочим, а не формальным.

    Роли исчезают целиком, `users.role` при этом не трогался ни разу — значит
    после отката система возвращается ровно к двум ролям в коде, и менеджеры
    остаются менеджерами. Ничего восстанавливать не нужно, потому что ничего и
    не разрушалось: роль добавлялась рядом со старым признаком, а не вместо
    него.
    """
    # Ссылка снимается ДО индекса: на MySQL индекс, по которому проверяется
    # внешний ключ, снять нельзя (отказ 1553), и откат вставал ровно здесь, на
    # `ix_users_role_id`. Разбор — в откате `e4451c527c34`.
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_role_id", type_="foreignkey")
    op.drop_index("ix_users_role_id", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("role_id")

    # Индексы прав отдельно не снимаем — их уносит сама таблица.
    op.drop_table("role_permissions")
    op.drop_table("roles")
