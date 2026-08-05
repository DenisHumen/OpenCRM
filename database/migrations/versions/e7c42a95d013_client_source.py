"""client source

Revision ID: e7c42a95d013
Revises: b81f6a2d40c7
Create Date: 2026-08-06 10:00:00.000000

Источник клиента — то, без чего отчёт «откуда приходят деньги» построить не из
чего.

Колонка со стабильным строковым ключом, а не ссылка на строку справочника:
у источника, в отличие от этапа воронки, нет ни вида, ни порядка, ни
архивации — платить за одно редактируемое название внешним ключом и join'ом в
каждом отчёте не за что. Понадобится справочник — рядом появится
`client_sources` с тем же ключом, и клиентов переносить не придётся.

Колонка nullable, и это главное в этой миграции: NULL означает «не спросили,
откуда», а ключ `other` — «спросили, ответ — ниоткуда конкретно». Поставить
здесь server_default='other' значило бы задним числом объявить, что у всех
заведённых до сегодня клиентов источник выяснен.

Индексы: по `source` считается группировка отчёта, по `created_at` —
отбор клиентов за период. Без них отчёт за год читает таблицу клиентов целиком.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7c42a95d013'
down_revision: Union[str, None] = 'b81f6a2d40c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_clients_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_clients_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_clients_created_at'))
        batch_op.drop_index(batch_op.f('ix_clients_source'))
        batch_op.drop_column('source')
