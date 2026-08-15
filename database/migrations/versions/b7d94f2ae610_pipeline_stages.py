"""pipeline stages

Revision ID: b7d94f2ae610
Revises: a3c58e10d7b4
Create Date: 2026-08-05 10:00:00.000000

Этапы воронки переезжают из кода в базу: CRM должна годиться любому малому
бизнесу, а у ремонта техники «диагностика», у салона «клиент пришёл», у
магазина «отправлен» — чужими словами пользоваться нельзя.

Настраивается название, фиксируется тип (`open`/`won`/`lost`): отчёты считаются
по типу, поэтому конверсия и потери работают при любых названиях.

Сделки, созданные до этой миграции, стоят на ключах прежнего жёсткого набора
(lead/quote/in_progress/review/won/lost) — он сохранён как пресет «Студия и
агентство», и при наличии таких сделок кладём именно его, чтобы карточки не
осиротели.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d94f2ae610'
down_revision: Union[str, None] = 'a3c58e10d7b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Дублируем наборы здесь намеренно: миграция обязана работать одинаково через
# год, когда пресеты в коде уже поменяются. Тянуть их импортом — значит
# переписывать историю задним числом.
#
# Названия английские, и это исправление, а не косметика. Интерфейс по
# умолчанию английский, а свежая установка в докере получает воронку ИМЕННО
# отсюда: точка входа гонит `alembic upgrade head` по пустой базе, и
# `pipeline_service.seed_defaults` потом видит этапы уже созданными и молчит.
# Пока здесь стоял русский, английская установка получала русскую воронку — и
# перевод пресетов в коде этого не менял, потому что до кода дело не доходило.
#
# Ключи не тронуты: на них ссылается всё остальное, и менять их значило бы
# осиротить карточки. Уже поднятых баз правка не касается — миграция у них
# отмечена, а названия этапов давно принадлежат владельцу.
AGENCY = [
    ("lead", "Lead", "open"),
    ("quote", "Quote", "open"),
    ("in_progress", "In progress", "open"),
    ("review", "Review", "open"),
    ("won", "Won", "won"),
    ("lost", "Lost", "lost"),
]
UNIVERSAL = [
    ("new", "New", "open"),
    ("in_progress", "In progress", "open"),
    ("ready", "Ready", "open"),
    ("done", "Completed", "won"),
    ("cancelled", "Declined", "lost"),
]


def upgrade() -> None:
    stages = op.create_table(
        'pipeline_stages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False, server_default='open'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('color', sa.String(length=7), nullable=False, server_default=''),
        sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('pipeline_stages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pipeline_stages_key'), ['key'], unique=True)
        batch_op.create_index(batch_op.f('ix_pipeline_stages_kind'), ['kind'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_pipeline_stages_sort_order'), ['sort_order'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_pipeline_stages_is_archived'), ['is_archived'], unique=False
        )

    # Есть ли уже сделки на старых ключах — от этого зависит, какой набор класть
    has_deals = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM deals")).scalar() or 0
    chosen = AGENCY if has_deals else UNIVERSAL
    op.bulk_insert(
        stages,
        [
            {"key": key, "name": name, "kind": kind, "sort_order": (i + 1) * 10,
             "color": "", "is_archived": False}
            for i, (key, name, kind) in enumerate(chosen)
        ],
    )


def downgrade() -> None:
    # Индексы перед `drop_table` не снимаются — таблица уносит их сама. Правило
    # общее на все миграции проекта, и держится оно не на аккуратности: MySQL не
    # даёт снять индекс, по которому проверяется внешний ключ (отказ 1553), и
    # такой порядок ломал откат на тринадцати шагах разом. Здесь внешних ключей
    # на этих индексах нет, поэтому падения не было, — но форма та же, и первый
    # же добавленный ключ превратил бы её в отказ. Разбор — в откате
    # `e4451c527c34`.
    op.drop_table('pipeline_stages')
