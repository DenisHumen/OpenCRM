"""message templates

Revision ID: c2a58f31d9e0
Revises: b7f41a2c9e35
Create Date: 2026-08-07 11:00:00.000000

Шаблоны сообщений: типовой ответ на заявку, ссылка на доску, напоминание об
оплате.

Миграция добавочная и состоит из одной новой таблицы: ни одна существующая не
меняется, поэтому порядка «колонка nullable → UPDATE → NOT NULL» здесь не
требуется. Заполнять таблицу нечем и не нужно — шаблон пишет человек, и
подсунутый ему «пример» пришлось бы либо переводить, либо стирать.

Уникальность названия объявляется сразу вместе с таблицей: она пуста по
построению, и правило «индексы последними, по заполненным данным» относится к
населённым таблицам. Обычных индексов здесь нет — шаблонов у бизнеса десяток, а
индекс по трёхзначному `channel` планировщик всё равно не возьмёт.

`downgrade` сносит таблицу целиком. Данных, на которые ссылается кто-то ещё, в
ней нет: применённый шаблон превращается в обычное письмо или запись ленты и
связи с шаблоном не хранит, поэтому откат не оставляет висящих ссылок.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2a58f31d9e0"
down_revision: Union[str, None] = "b7f41a2c9e35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        # Без `server_default`: MySQL запрещает обычный DEFAULT у TEXT (ошибка
        # 1101), а выражение здесь ни к чему — тело пишут всегда.
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # Уникальность названия — не косметика: шаблон выбирают из списка
        # глазами, и два одинаковых имени означают выбор наугад. Она же
        # закрывает двойное нажатие из второй вкладки, куда засов на кнопке
        # (`lib/guard.ts`) не достаёт.
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("message_templates")
