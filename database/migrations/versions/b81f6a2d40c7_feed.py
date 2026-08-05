"""feed entries

Revision ID: b81f6a2d40c7
Revises: a7d5e1c93b02
Create Date: 2026-08-05 21:00:00.000000

Заметка становится записью ленты.

Архитектурное решение, которое принимается ДО почты и телефонии: письма,
звонки и заметки падают в один поток, а не в три отдельных журнала. Склеивать
их потом больно — придётся сводить три источника с разными полями и временем.

Заготовка была: у client_notes с самого начала есть `kind`
(note/call/meeting/email), он просто не использовался. Добавляем два поля,
которых не хватало: направление (входящее/исходящее) и привязку к заявке.

Направление пустое по умолчанию: у заметки его нет, и «нет направления» — не
то же самое, что «входящее». Привязка к заявке необязательна: «клиент звонил
спросить про цены» бывает и без заявки, а клиент есть всегда.

Существующие заметки переживают это без изменений: оба поля со значениями по
умолчанию, ничего не переписывается задним числом.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b81f6a2d40c7'
down_revision: Union[str, None] = 'a7d5e1c93b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('client_notes', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('direction', sa.String(length=3), nullable=False, server_default='')
        )
        batch_op.add_column(sa.Column('deal_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_client_notes_deal_id'), ['deal_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_client_notes_deal_id', 'deals', ['deal_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('client_notes', schema=None) as batch_op:
        batch_op.drop_constraint('fk_client_notes_deal_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_client_notes_deal_id'))
        batch_op.drop_column('deal_id')
        batch_op.drop_column('direction')
