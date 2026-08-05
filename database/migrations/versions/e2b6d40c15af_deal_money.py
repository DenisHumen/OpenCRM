"""deal money

Revision ID: e2b6d40c15af
Revises: d1f7a2c60b93
Create Date: 2026-08-05 18:40:00.000000

Деньги в сделке: сумма и полученная предоплата.

Целыми числами в минимальных единицах (копейки, центы), а не дробными. На
float округление вылезает всегда, и сумма по колонке канбана начинает
расходиться с суммой карточек на копейку — со стороны это выглядит как ошибка
в расчётах, а не как особенность двоичных дробей.

Остаток не хранится: он всегда amount минус prepaid. Третье поле рядом с
двумя первыми разошлось бы с ними при первой же правке.

amount допускает NULL намеренно: «сумму ещё не назвали» и «работа бесплатная»
— разные состояния, и в отчёте они считаются по-разному. prepaid не допускает:
«не платили» — это ноль.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2b6d40c15af'
down_revision: Union[str, None] = 'd1f7a2c60b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('amount', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('prepaid', sa.Integer(), nullable=False, server_default='0')
        )


def downgrade() -> None:
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.drop_column('prepaid')
        batch_op.drop_column('amount')
