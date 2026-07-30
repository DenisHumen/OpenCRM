"""work preview crop

Revision ID: d5e93b71a8c6
Revises: c3d81a5e9f42
Create Date: 2026-07-30 09:00:00.000000

Какой фрагмент длинной работы попадает на витрину. Раньше лонгрид резался
всегда с самого верха — какой именно кусок увидит клиент, менеджер не выбирал.
Теперь выбирает: preview_focus задаёт положение окна обрезки по вертикали
(0 — верх картинки, 1 — низ).

Форму места при этом задаёт композиция, а не работа (см. web/public/layout.py),
поэтому высота обрезки в базе не хранится.

NULL = прежнее поведение, поэтому уже загруженные работы выглядят так же, как
до миграции.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e93b71a8c6'
down_revision: Union[str, None] = 'c3d81a5e9f42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('works', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preview_focus', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('works', schema=None) as batch_op:
        batch_op.drop_column('preview_focus')
