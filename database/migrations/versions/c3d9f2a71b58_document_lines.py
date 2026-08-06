"""document lines and stock move source

Revision ID: c3d9f2a71b58
Revises: b2c8e4f1a396
Create Date: 2026-08-07 12:00:00.000000

Перечень позиций у бланка и ссылка движения склада на бланк, которым оно вызвано.

**Миграция чисто добавочная.** Ни одной существующей строки она не переписывает:
новая таблица плюс одна nullable-колонка к `stock_moves`. Обычное движение
бланком не вызвано, и NULL здесь — правда о нём, а не пропуск. Поэтому шага
«заполнить и ужесточить» нет вовсе, а простоя миграция не добавляет.

Откат снимает колонку и таблицу; движения при этом остаются на месте, теряются
только перечни позиций.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d9f2a71b58"
down_revision: Union[str, None] = "b2c8e4f1a396"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("quantity_milli", sa.Integer(), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=True),
        sa.Column("cost_minor", sa.Integer(), nullable=True),
        sa.Column("picked_milli", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_lines_document_id", "document_lines", ["document_id"])
    op.create_index("ix_document_lines_product_id", "document_lines", ["product_id"])

    op.add_column("stock_moves", sa.Column("document_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("stock_moves") as batch:
        batch.create_foreign_key(
            "fk_stock_moves_document", "documents", ["document_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_stock_moves_document_id", "stock_moves", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_stock_moves_document_id", table_name="stock_moves")
    with op.batch_alter_table("stock_moves") as batch:
        batch.drop_constraint("fk_stock_moves_document", type_="foreignkey")
        batch.drop_column("document_id")

    op.drop_index("ix_document_lines_product_id", table_name="document_lines")
    op.drop_index("ix_document_lines_document_id", table_name="document_lines")
    op.drop_table("document_lines")
