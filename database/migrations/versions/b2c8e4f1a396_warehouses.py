"""warehouses and transfers

Revision ID: b2c8e4f1a396
Revises: a1f5c8d2b704
Create Date: 2026-08-07 10:00:00.000000

Несколько складов: место переезжает на движение, переезд — два связанных движения.

**Порядок здесь строгий, иначе миграция не пройдёт на живой базе.** На боевом
сервере движения уже есть, а `warehouse_id` у них обязан стать NOT NULL —
значит колонку сначала добавляют пустой, заполняют одним UPDATE и только потом
ужесточают. Обратный порядок отвалится на первой же существующей строке.

Склад «Основной» засевается прямо здесь, а не при старте приложения. Причина:
шаг 4 обязан на что-то сослаться, а взяться этому «чему-то» больше неоткуда —
на пустой базе склада тоже нет, а система без склада не может принять ни одного
прихода.

**Миграция добавочная и обратимая.** Данные не переписываются: старые движения
получают ссылку на единственный существовавший склад — что и есть правда о них.
Откат снимает колонки и таблицы, движения остаются на месте; теряются только
сами склады и переезды.

Оговорка про большие базы: шаг 4 — это UPDATE по всей таблице движений, то есть
блокировка на запись. На двухстах тысячах строк это единицы секунд, и внутри
окна обновления (контейнер в это время уже остановлен) простоя не добавляет.
Понадобится больше — лить пачками по `WHERE id BETWEEN`.
"""
from typing import Sequence, Union

from alembic import op
# У TEXT значение по умолчанию записано выражением — `DEFAULT ('')`, не `DEFAULT ''`:
# обычную форму MySQL отвергает (ошибка 1101), и миграция обрывается на середине.
# Скобки понимают оба движка. Подробности — database/types.text_default.
import sqlalchemy as sa


revision: str = "b2c8e4f1a396"
down_revision: Union[str, None] = "a1f5c8d2b704"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Места и шапки переездов.
    op.create_table(
        "warehouses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=True),
        sa.Column("address", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_warehouses_name", "warehouses", ["name"])
    op.create_index("ix_warehouses_code", "warehouses", ["code"], unique=True)
    op.create_index("ix_warehouses_deleted_at", "warehouses", ["deleted_at"])

    op.create_table(
        "stock_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_warehouse_id", sa.Integer(), nullable=False),
        sa.Column("to_warehouse_id", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("reverses_id", sa.Integer(), nullable=True),
        sa.Column("happened_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["from_warehouse_id"], ["warehouses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_warehouse_id"], ["warehouses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reverses_id"], ["stock_transfers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_transfers_from_warehouse_id", "stock_transfers", ["from_warehouse_id"])
    op.create_index("ix_stock_transfers_to_warehouse_id", "stock_transfers", ["to_warehouse_id"])
    op.create_index("ix_stock_transfers_reverses_id", "stock_transfers", ["reverses_id"])
    op.create_index("ix_stock_transfers_happened_at", "stock_transfers", ["happened_at"])
    op.create_index("ix_stock_transfers_author_id", "stock_transfers", ["author_id"])

    # 2. Один склад обязан существовать до шага 4 — ему и достанутся все старые
    #    движения. Название по-русски: язык интерфейса по умолчанию русский, а
    #    переименовать склад можно в первый же день.
    warehouses = sa.table(
        "warehouses",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("is_default", sa.Boolean),
    )
    op.bulk_insert(warehouses, [{"id": 1, "name": "Основной", "is_default": True}])

    # 3. Колонка сначала пустая: на живой базе движения уже есть, и NOT NULL
    #    сразу отвалился бы на первой строке.
    op.add_column("stock_moves", sa.Column("warehouse_id", sa.Integer(), nullable=True))
    op.add_column("stock_moves", sa.Column("transfer_id", sa.Integer(), nullable=True))

    # 4. Заполняем одним запросом, а не построчно: построчный обход двухсот
    #    тысяч движений через ORM — это минуты вместо секунд.
    op.execute(sa.text("UPDATE stock_moves SET warehouse_id = 1 WHERE warehouse_id IS NULL"))

    # 5. Теперь ужесточаем. В SQLite это пересоздание таблицы, alembic делает его
    #    сам внутри batch_alter_table; в MySQL — обычный ALTER.
    with op.batch_alter_table("stock_moves") as batch:
        batch.alter_column("warehouse_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key(
            "fk_stock_moves_warehouse", "warehouses", ["warehouse_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_foreign_key(
            "fk_stock_moves_transfer",
            "stock_transfers",
            ["transfer_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 6. Индексы — после заполнения: строить их на пустой колонке и потом
    #    переписывать всю таблицу дороже, чем один раз по готовым данным.
    op.create_index("ix_stock_moves_warehouse_id", "stock_moves", ["warehouse_id"])
    op.create_index("ix_stock_moves_transfer_id", "stock_moves", ["transfer_id"])
    op.create_index(
        "ix_stock_moves_wh_product_qty",
        "stock_moves",
        ["warehouse_id", "product_id", "quantity_milli"],
    )
    op.create_index("ix_stock_moves_wh_happened", "stock_moves", ["warehouse_id", "happened_at"])


def downgrade() -> None:
    op.drop_index("ix_stock_moves_wh_happened", table_name="stock_moves")
    op.drop_index("ix_stock_moves_wh_product_qty", table_name="stock_moves")
    op.drop_index("ix_stock_moves_transfer_id", table_name="stock_moves")
    op.drop_index("ix_stock_moves_warehouse_id", table_name="stock_moves")
    with op.batch_alter_table("stock_moves") as batch:
        batch.drop_constraint("fk_stock_moves_transfer", type_="foreignkey")
        batch.drop_constraint("fk_stock_moves_warehouse", type_="foreignkey")
        batch.drop_column("transfer_id")
        batch.drop_column("warehouse_id")

    op.drop_index("ix_stock_transfers_author_id", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_happened_at", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_reverses_id", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_to_warehouse_id", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_from_warehouse_id", table_name="stock_transfers")
    op.drop_table("stock_transfers")

    op.drop_index("ix_warehouses_deleted_at", table_name="warehouses")
    op.drop_index("ix_warehouses_code", table_name="warehouses")
    op.drop_index("ix_warehouses_name", table_name="warehouses")
    op.drop_table("warehouses")
