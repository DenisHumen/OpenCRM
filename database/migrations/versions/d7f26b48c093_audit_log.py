"""audit log: who did what, and what caused it

Revision ID: d7f26b48c093
Revises: b2c48f7a91d5
Create Date: 2026-08-06 12:00:00.000000

Единый журнал действий. Существующие поля исполнителя (deal_stage_changes,
stock_moves.author_id, client_notes.author_id, module_states.updated_by)
остаются на месте — они рабочие данные своих блоков, а не журнал.

Внешнего ключа на объект действия здесь нет намеренно: журнал обязан пережить
удаление того, о чём он написан, а именно удаление он и записывает. Ссылка
CASCADE снесла бы запись вместе с объектом, RESTRICT запретил бы удалять вовсе.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7f26b48c093"
down_revision: Union[str, None] = "b2c48f7a91d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        # Снимок имени: внешний ключ обнуляется при удалении сотрудника, и без
        # снимка журнал остался бы без имён ровно тогда, когда человек уволился.
        sa.Column("actor_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("entity_label", sa.String(length=200), nullable=False, server_default=""),
        # Было и стало. NULL («значения не было») и пустая строка («значение было
        # пустым») различаются, поэтому nullable и без server_default.
        sa.Column("value_before", sa.Text(), nullable=True),
        sa.Column("value_after", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        # SET NULL, как у всякого авторства в проекте: удаление сотрудника не
        # должно уносить с собой историю. Имя остаётся в actor_name.
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_events_created_at"), ["created_at"], unique=False
        )
        # Журнал растёт быстрее всех остальных таблиц, поэтому индексы стоят
        # ровно под два вопроса, ради которых в него заходят, и не больше.
        # «Что делали с этой заявкой»:
        batch_op.create_index(
            "ix_audit_events_entity", ["entity_type", "entity_id", "created_at"], unique=False
        )
        # «Что делал этот сотрудник» — он же закрывает внешний ключ на users:
        batch_op.create_index("ix_audit_events_actor", ["actor_id", "created_at"], unique=False)


def downgrade() -> None:
    # Индексы отдельно не снимаем — их уносит `drop_table`, а на MySQL снятие
    # индекса под живым внешним ключом отвергается (1553). Разбор — в откате
    # `e4451c527c34`. Здесь на это упирался `ix_audit_events_actor`: он и есть
    # индекс под ключ на `users` — о чём сказано парой строк выше, при создании.
    op.drop_table("audit_events")
