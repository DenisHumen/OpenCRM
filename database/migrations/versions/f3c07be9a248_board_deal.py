"""board deal

Revision ID: f3c07be9a248
Revises: e2b6d40c15af
Create Date: 2026-08-05 19:20:00.000000

Доска привязывается к заявке.

Раньше доска знала только клиента, а у клиента за год бывает пять заказов —
все его доски лежали одной кучей, и какая к чему относится, приходилось помнить
самому.

Колонка допускает NULL и останется такой. У всех существующих досок заявки нет:
они делались до того, как заявки появились. Проставлять им что-нибудь задним
числом нельзя — это засорило бы воронку записями, которых в жизни не было.
Привязка к клиенту остаётся отдельным полем по той же причине: доска без заявки
обязана продолжать работать.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3c07be9a248'
down_revision: Union[str, None] = 'e2b6d40c15af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('boards', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deal_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_boards_deal_id'), ['deal_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_boards_deal_id', 'deals', ['deal_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('boards', schema=None) as batch_op:
        batch_op.drop_constraint('fk_boards_deal_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_boards_deal_id'))
        batch_op.drop_column('deal_id')
