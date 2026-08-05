"""tasks

Revision ID: a7d5e1c93b02
Revises: f3c07be9a248
Create Date: 2026-08-05 20:10:00.000000

Напоминания: перезвонить, отправить счёт, забрать технику.

CRM, которая не напоминает перезвонить, — записная книжка.

Срок хранится naive UTC, как и всё остальное время. Момент абсолютный:
«сегодня до 18:00» превращается в UTC ещё на клиенте, потому что 18:00 у
приёмщика в Киеве и у владельца в Варшаве — разные мгновения.

Привязки к клиенту и заявке независимые и обе необязательные: бывает «позвонить
Петрову» без заявки и «заказать деталь» по заявке без разговора с клиентом.
CASCADE на обеих: напоминание по удалённой заявке напоминать не о чем.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7d5e1c93b02'
down_revision: Union[str, None] = 'f3c07be9a248'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('due_at', sa.DateTime(), nullable=True),
        sa.Column('assignee_id', sa.Integer(), nullable=True),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('deal_id', sa.Integer(), nullable=True),
        sa.Column('done_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        for column in ('due_at', 'assignee_id', 'client_id', 'deal_id', 'done_at', 'created_at'):
            batch_op.create_index(batch_op.f(f'ix_tasks_{column}'), [column], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        for column in ('created_at', 'done_at', 'deal_id', 'client_id', 'assignee_id', 'due_at'):
            batch_op.drop_index(batch_op.f(f'ix_tasks_{column}'))
    op.drop_table('tasks')
