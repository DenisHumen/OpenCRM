"""deals

Revision ID: a3c58e10d7b4
Revises: f1a72c9d5b30
Create Date: 2026-08-03 16:00:00.000000

Сделка — работа для клиента от заявки до закрытия. Того, что между «клиент
появился» и «вот доска с результатом», в системе не было вовсе: отчёты не из
чего считать, письма и звонки некуда привязывать, кроме клиента вообще.

Журнал смены этапов отдельной таблицей: из него считается, сколько сделка
простояла в каждом этапе, — единственный отчёт, показывающий, где затык.
"""
from typing import Sequence, Union

from alembic import op
# У TEXT значение по умолчанию записано выражением — `DEFAULT ('')`, не `DEFAULT ''`:
# обычную форму MySQL отвергает (ошибка 1101), и миграция обрывается на середине.
# Скобки понимают оба движка. Подробности — database/types.text_default.
import sqlalchemy as sa


revision: str = 'a3c58e10d7b4'
down_revision: Union[str, None] = 'f1a72c9d5b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('manager_id', sa.Integer(), nullable=True),
        sa.Column('stage', sa.String(length=20), nullable=False, server_default='lead'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('description', sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column('due_at', sa.DateTime(), nullable=True),
        sa.Column('lost_reason', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['manager_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deals_title'), ['title'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_manager_id'), ['manager_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_stage'), ['stage'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_sort_order'), ['sort_order'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_due_at'), ['due_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_closed_at'), ['closed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_deleted_at'), ['deleted_at'], unique=False)

    op.create_table(
        'deal_stage_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('deal_id', sa.Integer(), nullable=False),
        sa.Column('from_stage', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('to_stage', sa.String(length=20), nullable=False),
        sa.Column('changed_by', sa.Integer(), nullable=True),
        sa.Column('changed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('deal_stage_changes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deal_stage_changes_deal_id'), ['deal_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_deal_stage_changes_changed_at'), ['changed_at'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('deal_stage_changes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deal_stage_changes_changed_at'))
        batch_op.drop_index(batch_op.f('ix_deal_stage_changes_deal_id'))
    op.drop_table('deal_stage_changes')

    with op.batch_alter_table('deals', schema=None) as batch_op:
        for name in (
            'ix_deals_deleted_at', 'ix_deals_created_at', 'ix_deals_closed_at',
            'ix_deals_due_at', 'ix_deals_sort_order', 'ix_deals_stage',
            'ix_deals_manager_id', 'ix_deals_client_id', 'ix_deals_title',
        ):
            batch_op.drop_index(batch_op.f(name))
    op.drop_table('deals')
