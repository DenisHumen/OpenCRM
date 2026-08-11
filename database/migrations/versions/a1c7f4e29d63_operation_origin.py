"""finance operations: origin snapshot — what caused the accrual

Revision ID: a1c7f4e29d63
Revises: c8f13d0a7b62
Create Date: 2026-08-11 12:00:00.000000

Одна колонка — `finance_operations.origin`: чем вызвано начисление.

Зачем она вообще. Отмена проведения заказа отыгрывает назад НАЧИСЛЕННОЕ, и
отбирала она всё, что висит на бланке. Налог с оборота висит там же — но
начислен он ПЛАТЕЖОМ, и на бланк попал лишь потому, что платёж назвал бланк.
Снятый отменой заказа, он оставлял оборот при нулевом налоге, а следующий шаг
(человек возвращает деньги отдельным действием) снимал его второй раз, уже с
отрицательной базы.

Почему снимок на операции, а не взгляд на `finance_rules.base`. По тому же
доводу, что у соседних `rate_bp` и `base_amount_minor`: правило правят на месте,
и `base` в том числе. Вчерашняя «упаковка», ставшая сегодня процентом с прихода,
уехала бы из своей корзины задним числом — молча. Снимок ещё и не требует
перечислять виды правил: следующий вид (эквайринг) проставит своё значение, а
отбор «порождено закрытием заказа» останется прежним.

**Миграция добавочная.** Колонка строго `nullable`: `NULL` здесь означает «не
порождено правилом» — так лежат платежи и всё, что человек завёл руками, и это
не «не успели проставить».

Шаг заполнения ОДИН `UPDATE` на вид, без построчного обхода через ORM. Заполнять
есть что: предыдущая миграция уже могла завести начисления, и вид каждого
однозначно читается по правилу, которое его породило, — других видов начисления
на тот момент не существовало. Поправки и отмены получают тот же `origin`, что и
их голова, потому что стоят на том же `rule_id`: цепочка целиком принадлежит
одному событию.

Индекса нет намеренно: отбор всегда идёт по `document_id` (у него свой составной
индекс), а `origin` только досеивает несколько строк одного бланка.

Откат снимает колонку. Данные при этом теряются те же, что и добавлялись, —
производные от правил, восстановимые тем же UPDATE.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c7f4e29d63"
down_revision: Union[str, None] = "c8f13d0a7b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Те же ключи, что в `database/models/finance.py`. Скопированы значениями, а не
#: импортом: миграция обязана делать то же самое и через год, когда набор ключей
#: в коде уедет вперёд. Импорт превратил бы прошлую миграцию в зависимую от
#: сегодняшней модели — ровно то, из-за чего миграции перестают воспроизводиться.
ORIGIN_ORDER_CLOSED = "order_closed"
ORIGIN_PAYMENT = "payment"


def upgrade() -> None:
    # 1. Колонка — nullable: у платежей и ручных операций её значения не бывает.
    op.add_column(
        "finance_operations", sa.Column("origin", sa.String(length=24), nullable=True)
    )

    # 2. Заполняем одним UPDATE на вид. Подзапрос идёт в ДРУГУЮ таблицу, поэтому
    #    он законен и в MySQL (запрет 1093 — про обновляемую таблицу в FROM).
    for origin, base in (
        (ORIGIN_ORDER_CLOSED, "per_order"),
        (ORIGIN_PAYMENT, "income_percent"),
    ):
        op.execute(
            sa.text(
                "UPDATE finance_operations SET origin = :origin "
                "WHERE rule_id IS NOT NULL AND rule_id IN "
                "(SELECT id FROM finance_rules WHERE base = :base)"
            ).bindparams(origin=origin, base=base)
        )


def downgrade() -> None:
    with op.batch_alter_table("finance_operations") as batch:
        batch.drop_column("origin")
