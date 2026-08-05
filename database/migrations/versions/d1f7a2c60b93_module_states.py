"""module states

Revision ID: d1f7a2c60b93
Revises: c9e14b83f27a
Create Date: 2026-08-05 16:20:00.000000

Какие блоки системы нужны этому бизнесу.

Дизайн-студии не нужен склад, мастерской — доски с макетами. Выключенный блок
исчезает из меню и из API, но данные его остаются на месте: выключение — это
«убрать с глаз», а не «стереть». Включил обратно — всё на месте.

В таблице только состояние. Перечень существующих блоков живёт в коде
(core/modules.py), поэтому строка от снесённого блока ничего не включает, а
новый блок появляется со своим значением по умолчанию без правки базы.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1f7a2c60b93'
down_revision: Union[str, None] = 'c9e14b83f27a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'module_states',
        sa.Column('key', sa.String(length=32), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('module_states')
