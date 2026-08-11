"""finance rules: tax on turnover, standard per-order costs, payment snapshots

Revision ID: c8f13d0a7b62
Revises: d3b8c05f1e2a
Create Date: 2026-08-11 10:00:00.000000

Правила начисления (`finance_rules`) и шесть снимковых колонок у операции.

**Миграция чисто добавочная.** Одна новая таблица и шесть колонок, все строго
`nullable`. Шага «заполнить одним UPDATE» здесь нет и быть не может: заполнять
нечего — у операций, заведённых до этой миграции, правила не было, ставки не
было и бланка не было. `NULL` здесь означает ровно это, а не «не успели
проставить»; поставь мы ноль, и «начислено по ставке 0%» стало бы неотличимо от
«начисления не было вовсе».

**Ставок НЕ СЕЯТЬ.** Справочник статей эта база засевает (`d4e1a83c2f60`), и это
правильно: операцию без статьи завести нельзя. Со ставками наоборот. Чужая
налоговая ставка, появившаяся молча при обновлении, начнёт откладывать деньги
без спроса — и владелец увидит это не раньше, чем сведёт кассу. Таблица
создаётся пустой; «упаковка 80 / доставка 150» заводит человек на экране.

Откат снимает индексы, внешние ключи, колонки и таблицу. Порядок обратный
подъёму, и в нём есть одно жёсткое место: ссылку `finance_operations.rule_id`
надо снять ДО того, как сносить `finance_rules`, — она стоит на RESTRICT, и
иначе снос упрётся в неё.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8f13d0a7b62"
down_revision: Union[str, None] = "d3b8c05f1e2a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Новая таблица — первой: на неё будет ссылаться колонка операции.
    op.create_table(
        "finance_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # `income_percent` | `per_order` — закрытый набор в коде
        # (`database/models/finance.BASES`), а не CHECK в базе: набор ключей
        # растёт (эквайринг, привязка к товару), а CHECK пришлось бы менять
        # миграцией на каждое новое значение.
        sa.Column("base", sa.String(length=24), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("source_category_id", sa.Integer(), nullable=True),
        # Ставка в базисных пунктах (сотых долях процента): 5% = 500.
        sa.Column("rate_bp", sa.Integer(), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        # RESTRICT у обеих ссылок: снести статью вместе с правилом значит молча
        # переписать то, куда ложились деньги.
        sa.ForeignKeyConstraint(
            ["category_id"], ["finance_categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_category_id"], ["finance_categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # 2. Колонки операции — строго nullable. Заполнять нечего: у прошлых
    #    операций правила не было.
    op.add_column("finance_operations", sa.Column("rule_id", sa.Integer(), nullable=True))
    op.add_column("finance_operations", sa.Column("rate_bp", sa.Integer(), nullable=True))
    op.add_column(
        "finance_operations", sa.Column("base_amount_minor", sa.Integer(), nullable=True)
    )
    op.add_column("finance_operations", sa.Column("document_id", sa.Integer(), nullable=True))
    op.add_column("finance_operations", sa.Column("corrects_id", sa.Integer(), nullable=True))
    op.add_column(
        "finance_operations", sa.Column("correction", sa.String(length=16), nullable=True)
    )

    # 3. Внешние ключи. В SQLite это пересоздание таблицы, alembic делает его сам
    #    внутри batch_alter_table; в MySQL — обычный ALTER.
    with op.batch_alter_table("finance_operations") as batch:
        batch.create_foreign_key(
            "fk_finance_ops_rule", "finance_rules", ["rule_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_foreign_key(
            "fk_finance_ops_document", "documents", ["document_id"], ["id"], ondelete="SET NULL"
        )
        # Ссылка на саму себя: поправка уточняет исходное начисление.
        batch.create_foreign_key(
            "fk_finance_ops_corrects",
            "finance_operations",
            ["corrects_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 4. Индексы — последними.
    op.create_index("ix_finance_rules_category_id", "finance_rules", ["category_id"])
    op.create_index(
        "ix_finance_rules_source_category_id", "finance_rules", ["source_category_id"]
    )
    op.create_index("ix_finance_rules_deleted_at", "finance_rules", ["deleted_at"])
    op.create_index(
        "ix_finance_rules_active_sort", "finance_rules", ["is_active", "sort_order"]
    )

    op.create_index("ix_finance_operations_rule_id", "finance_operations", ["rule_id"])
    op.create_index("ix_finance_operations_corrects_id", "finance_operations", ["corrects_id"])
    # Сумма внутри индекса: «получено по заказу» и «расходы по заказу» — это SUM
    # по бланку, и без суммы в индексе база лезла бы в таблицу за каждым числом.
    op.create_index(
        "ix_finance_ops_document",
        "finance_operations",
        ["document_id", "category_id", "amount_minor"],
    )


def downgrade() -> None:
    op.drop_index("ix_finance_ops_document", table_name="finance_operations")
    op.drop_index("ix_finance_operations_corrects_id", table_name="finance_operations")
    op.drop_index("ix_finance_operations_rule_id", table_name="finance_operations")

    op.drop_index("ix_finance_rules_active_sort", table_name="finance_rules")
    op.drop_index("ix_finance_rules_deleted_at", table_name="finance_rules")
    op.drop_index("ix_finance_rules_source_category_id", table_name="finance_rules")
    op.drop_index("ix_finance_rules_category_id", table_name="finance_rules")

    # Ссылки снимаются вместе с колонками, и обязательно ДО сноса `finance_rules`:
    # `rule_id` стоит на RESTRICT.
    with op.batch_alter_table("finance_operations") as batch:
        batch.drop_constraint("fk_finance_ops_corrects", type_="foreignkey")
        batch.drop_constraint("fk_finance_ops_document", type_="foreignkey")
        batch.drop_constraint("fk_finance_ops_rule", type_="foreignkey")
        batch.drop_column("correction")
        batch.drop_column("corrects_id")
        batch.drop_column("document_id")
        batch.drop_column("base_amount_minor")
        batch.drop_column("rate_bp")
        batch.drop_column("rule_id")

    op.drop_table("finance_rules")
