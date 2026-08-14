"""finance: categories, operations, budgets

Revision ID: d4e1a83c2f60
Revises: c3d9f2a71b58
Create Date: 2026-08-07 14:00:00.000000

Блок «Финансы»: статьи доходов и расходов, операции по ним и план на период.

**Миграция чисто добавочная.** Ни одной существующей строки она не переписывает
и ни одной существующей таблицы не трогает: три новые таблицы и посев
справочника. Поэтому шагов «добавить колонку nullable → заполнить одним UPDATE →
ужесточить через batch_alter_table» здесь нет вовсе — ужесточать нечего, все
колонки рождаются сразу NOT NULL на пустых таблицах. Простоя миграция не
добавляет.

**Статьи засеваются прямо здесь, а не при старте приложения** — по той же
причине, что склад «Основной» в `b2c8e4f1a396`: операцию без статьи завести
нельзя, а пустой справочник на первом экране блока — это конструктор вместо
работы, ровно то, чего в проекте избегают (см. пресеты воронки и наборы блоков).
Набор при этом обычный, а не обязательный: закрыть лишнее и завести своё можно в
первый же день.

Названия статей по-русски — как у склада «Основной»: интерфейс продукта
английский, а посев идёт в базу конкретного бизнеса, где статьи потом
переименовывают под себя.

Откат снимает индексы и все три таблицы. Данных за пределами блока он не
касается: ссылки на заявки, клиентов и фирмы жили внутри `finance_operations` и
уходят вместе с ней — сами заявки, клиенты и фирмы остаются на месте.
"""
from typing import Sequence, Union

from alembic import op
# У TEXT значение по умолчанию записано выражением — `DEFAULT ('')`, не
# `DEFAULT ''`: обычную форму MySQL отвергает (ошибка 1101), и миграция
# обрывается на середине. Скобки понимают оба движка. Подробности —
# database/types.text_default.
import sqlalchemy as sa


revision: str = "d4e1a83c2f60"
down_revision: Union[str, None] = "c3d9f2a71b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Начальный справочник статей.
#:
#: Идентификаторы проставлены явно, а не отданы автоинкременту. Так набор
#: воспроизводим: следующая миграция, которой понадобится сослаться на
#: «Зарплату», найдёт её по числу, а не по названию, которое к тому времени
#: успеют переименовать.
SEED = (
    (1, "Выручка", "income", "general"),
    (2, "Прочие доходы", "income", "general"),
    (3, "Закупка товара", "expense", "general"),
    (4, "Аренда", "expense", "general"),
    # Зарплата и налоги — не просто расходы: их спрашивают отдельной строкой
    # («сколько ушло на людей», «сколько отдали государству»), и разложить
    # расходы по этому признаку нельзя, если он не записан.
    (5, "Зарплата", "expense", "salary"),
    (6, "Налоги", "expense", "tax"),
    (7, "Реклама", "expense", "general"),
    (8, "Прочие расходы", "expense", "general"),
)


def upgrade() -> None:
    # 1. Справочник статей — первым: на него ссылаются обе следующие таблицы.
    op.create_table(
        "finance_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column(
            "purpose", sa.String(length=16), server_default="general", nullable=False
        ),
        sa.Column("note", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # 2. Операции и планы.
    op.create_table(
        "finance_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        # Знаковое целое в минорных единицах: доход +, расход −. Прибыль равна
        # SUM(amount_minor) и нигде не хранится.
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("happened_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("deal_id", sa.Integer(), nullable=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["category_id"], ["finance_categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "finance_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("planned_amount_minor", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"], ["finance_categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        # «Один план на статью в периоде» держит СУБД, а не запрос: частичность
        # здесь не нужна (мягкого удаления у бюджета нет), значит обычный UNIQUE
        # работает и в MySQL.
        sa.UniqueConstraint(
            "category_id", "period_start", "period_end", name="uq_finance_budget_period"
        ),
    )

    # 3. Посев справочника. Одной вставкой, а не восемью: посев в цикле по
    #    строке — восемь обращений к базе ради восьми строк.
    categories = sa.table(
        "finance_categories",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("direction", sa.String),
        sa.column("purpose", sa.String),
    )
    op.bulk_insert(
        categories,
        [
            {"id": row_id, "name": name, "direction": direction, "purpose": purpose}
            for row_id, name, direction, purpose in SEED
        ],
    )

    # 4. Индексы — последними, по уже засеянным данным.
    op.create_index(
        "ix_finance_categories_deleted_at", "finance_categories", ["deleted_at"]
    )
    op.create_index(
        "ix_finance_categories_direction_name",
        "finance_categories",
        ["direction", "name"],
    )

    op.create_index("ix_finance_operations_deal_id", "finance_operations", ["deal_id"])
    op.create_index("ix_finance_operations_client_id", "finance_operations", ["client_id"])
    op.create_index("ix_finance_operations_company_id", "finance_operations", ["company_id"])
    op.create_index("ix_finance_operations_author_id", "finance_operations", ["author_id"])
    # Сумма стоит в самом индексе: агрегат по статье за период иначе лез бы в
    # таблицу за каждым числом.
    op.create_index(
        "ix_finance_ops_category_happened",
        "finance_operations",
        ["category_id", "happened_at", "amount_minor"],
    )
    op.create_index(
        "ix_finance_ops_happened_amount",
        "finance_operations",
        ["happened_at", "amount_minor"],
    )

    op.create_index(
        "ix_finance_budgets_period", "finance_budgets", ["period_start", "period_end"]
    )


def downgrade() -> None:
    # Индексы отдельно не снимаем — их уносит `drop_table`, а на MySQL снятие
    # индекса под живым внешним ключом отвергается (1553). Разбор — в откате
    # `e4451c527c34`. Здесь на это упирался `ix_finance_ops_category_happened`:
    # он же и есть индекс под ключ на `finance_categories`.
    op.drop_table("finance_budgets")
    op.drop_table("finance_operations")

    # Статьи — последними: на них ссылались обе таблицы выше, и RESTRICT не дал
    # бы снести справочник раньше того, что на него смотрит.
    op.drop_table("finance_categories")
