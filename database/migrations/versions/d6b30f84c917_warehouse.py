"""warehouse: products and stock moves

Revision ID: d6b30f84c917
Revises: b81f6a2d40c7
Create Date: 2026-08-06 10:00:00.000000

Склад: номенклатура и движения.

Колонки «остаток» здесь нет и не будет. Остаток равен сумме движений и считается
запросом (database/repositories/warehouse.py): хранимое число однажды разойдётся
с историей — из-за отката, правки задним числом, второго процесса, — и способа
узнать, какая из двух цифр верна, не останется.
"""
from typing import Sequence, Union

from alembic import op
# У TEXT значение по умолчанию записано выражением — `DEFAULT ('')`, не `DEFAULT ''`:
# обычную форму MySQL отвергает (ошибка 1101), и миграция обрывается на середине.
# Скобки понимают оба движка. Подробности — database/types.text_default.
# Сравнение строк в MySQL по умолчанию регистронезависимо. Для колонок ниже
# это меняет смысл: `UNIQUE` начинает считать одним значением то, что в
# SQLite было разным, а поиск по токену находит чужую запись, отличающуюся
# регистром. Поэтому у них сравнение побайтное — как было и остаётся в
# SQLite. Разбор — database/types.ExactString.
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = 'd6b30f84c917'
down_revision: Union[str, None] = "f8d53b06e124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'products',
        sa.Column('id', sa.Integer(), nullable=False),
        # Артикул необязателен, поэтому nullable: NULL в уникальном индексе не
        # конфликтует, а пустая строка конфликтовала бы со второй такой же.
        sa.Column('sku', sa.String(length=64).with_variant(mysql.VARCHAR(64, collation="utf8mb4_bin"), "mysql"), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('unit', sa.String(length=16), nullable=False, server_default='pcs'),
        # Деньги — целые в минимальных единицах; NULL значит «цену не назвали».
        sa.Column('price_minor', sa.Integer(), nullable=True),
        sa.Column('cost_minor', sa.Integer(), nullable=True),
        sa.Column('is_service', sa.Boolean(), nullable=False, server_default='0'),
        # Порог предупреждения — в тех же тысячных долях, что и количество.
        sa.Column('min_stock_milli', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_products_sku'), ['sku'], unique=True)
        batch_op.create_index(batch_op.f('ix_products_name'), ['name'], unique=False)
        batch_op.create_index(batch_op.f('ix_products_deleted_at'), ['deleted_at'], unique=False)

    op.create_table(
        'stock_moves',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        # Знаковое количество в тысячных долях единицы: приход +, расход −.
        sa.Column('quantity_milli', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('deal_id', sa.Integer(), nullable=True),
        sa.Column('cost_minor', sa.Integer(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column('happened_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('author_id', sa.Integer(), nullable=True),
        # RESTRICT: удаление товара не должно бесшумно стирать историю списаний.
        # Штатный путь удаления — products.deleted_at.
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='RESTRICT'),
        # SET NULL: товар со склада ушёл, и удаление заявки не возвращает его.
        sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('stock_moves', schema=None) as batch_op:
        # product_id — и внешний ключ, и колонка группировки для остатка:
        # без индекса SUM(...) GROUP BY product_id читал бы таблицу целиком.
        batch_op.create_index(batch_op.f('ix_stock_moves_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_moves_deal_id'), ['deal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_moves_author_id'), ['author_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_moves_kind'), ['kind'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_stock_moves_happened_at'), ['happened_at'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('stock_moves', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stock_moves_happened_at'))
        batch_op.drop_index(batch_op.f('ix_stock_moves_kind'))
        batch_op.drop_index(batch_op.f('ix_stock_moves_author_id'))
        batch_op.drop_index(batch_op.f('ix_stock_moves_deal_id'))
        batch_op.drop_index(batch_op.f('ix_stock_moves_product_id'))
    op.drop_table('stock_moves')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_products_deleted_at'))
        batch_op.drop_index(batch_op.f('ix_products_name'))
        batch_op.drop_index(batch_op.f('ix_products_sku'))
    op.drop_table('products')
