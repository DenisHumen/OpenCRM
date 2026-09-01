"""Строки заявки: товары со склада и свои траты.

Третий пункт плана `docs/19-sborka-zakaza.md`. До этой ревизии заявка
описывалась одной суммой, набранной руками: что именно продали — знал только
человек, и посчитать себестоимость или списать товар со склада было не из чего.

**Одна таблица на товары и на свои траты.** Соблазн завести две (позиции и
«прочие расходы») отвергнут: сумма заявки тогда складывается из двух источников,
и первый же отчёт забудет один из них. Вид строки не хранится, а выводится из
`product_id` — разбор в §Р2 плана.

**Ключи разной строгости, и это не небрежность.** `deal_id` — CASCADE: строка
есть часть заявки, а не история. `product_id` и `warehouse_id` — RESTRICT: тот
же довод, что у `stock_moves`, — удалить товар вместе со строками проданных
заявок значит переписать историю продаж. Штатное удаление товара мягкое
(`products.deleted_at`), так что RESTRICT срабатывает только на прямом DELETE.

**Таблица создаётся пустой, и заполнять её нечем.** Разложить прежнюю
`deals.amount` по строкам нельзя: числа «из чего она сложилась» не существует
нигде. Поэтому у старых заявок строк не будет, и сумма у них останется той,
которую назвал человек, — это правильное состояние, а не потеря.

**Индекс по товару ОДИН, составной.** Отдельный `(product_id)` был бы его
приставкой: MySQL берёт составной и для отбора по одному столбцу, и под внешний
ключ. Два индекса означали бы двойную плату на каждой вставке строки.

**Индекс `(product_id, deal_id)` — под вопрос «кто держит этот товар».**
Карточка товара показывает заявки, которые его удерживают; без пары она
перебирала бы строки всех заявок. Второй колонкой `deal_id`, потому что после
отбора по товару из строки нужна именно заявка.

**Проверено на населённой базе.** Прогон: MySQL 8, 200 000 заявок на предыдущей
ревизии, строк 0 — таблица новая.

    накат            1,3 с (вместе с запуском alembic)
    таблица          создана, 10 колонок, 4 индекса (первичный, два ключа, пара)
    заявки           200 000 на месте, `amount` не тронут
    откат            таблица удалена, 200 000 заявок на месте

Revision ID: e2b64f1a7c85
Revises: d7a15c93b402
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b64f1a7c85"
down_revision: Union[str, None] = "d7a15c93b402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deal_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("quantity_milli", sa.Integer(), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=True),
        sa.Column("cost_minor", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deal_lines_deal_id", "deal_lines", ["deal_id"])
    op.create_index("ix_deal_lines_warehouse_id", "deal_lines", ["warehouse_id"])
    op.create_index("ix_deal_lines_product_deal", "deal_lines", ["product_id", "deal_id"])


def downgrade() -> None:
    # Индексы уходят вместе с таблицей — снимать их отдельно значит просить
    # MySQL отдать индекс, на котором висит внешний ключ (ошибка 1553).
    op.drop_table("deal_lines")
