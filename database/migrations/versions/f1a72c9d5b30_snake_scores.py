"""snake scores

Revision ID: f1a72c9d5b30
Revises: d5e93b71a8c6
Create Date: 2026-08-03 12:00:00.000000

Таблица лидеров змейки со страницы обслуживания. Живёт в основной базе, а не в
отдельном хранилище: своё потребовало бы постоянно работающего сервиса рядом —
лишний контейнер в продакшене навсегда ради игры на минуту в месяц. Пока сайт
обновляется, счёт лежит в localStorage у игрока и уходит на сервер, как только
приложение вернулось.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a72c9d5b30'
down_revision: Union[str, None] = 'd5e93b71a8c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'snake_scores',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=24), nullable=False, server_default=''),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('played_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('ip_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('snake_scores', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_snake_scores_score'), ['score'], unique=False)
        batch_op.create_index(batch_op.f('ix_snake_scores_ip_hash'), ['ip_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_snake_scores_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    # Индексы перед `drop_table` не снимаются — таблица уносит их сама. Правило
    # общее на все миграции проекта, и держится оно не на аккуратности: MySQL не
    # даёт снять индекс, по которому проверяется внешний ключ (отказ 1553), и
    # такой порядок ломал откат на тринадцати шагах разом. Здесь внешних ключей
    # на этих индексах нет, поэтому падения не было, — но форма та же, и первый
    # же добавленный ключ превратил бы её в отказ. Разбор — в откате
    # `e4451c527c34`.
    op.drop_table('snake_scores')
