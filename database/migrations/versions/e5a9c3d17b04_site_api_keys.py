"""API сайта: ключи доступа, тип склада, описание товара, бронь у заказа

Revision ID: e5a9c3d17b04
Revises: d4c17e6b0a92

Порядок строгий: на живой базе склады и товары уже есть, и NOT NULL сразу
отвалился бы на первой же существующей строке. Разбор — docs/16-api-sayta.md §10.

Ничего не помечается складом магазина. Обновление, само открывшее витрину
наружу, — это утечка, оформленная как улучшение.
"""

import sqlalchemy as sa
from alembic import op

from database.types import ExactString

revision = "e5a9c3d17b04"
down_revision = "d4c17e6b0a92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Новые таблицы.
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("prefix", sa.String(length=12), nullable=False),
        sa.Column("token_hash", ExactString(64), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("stock_mode", sa.String(length=8), server_default="bucket", nullable=False),
        sa.Column("few_threshold_milli", sa.Integer(), server_default="5000", nullable=False),
        sa.Column("rate_per_min", sa.Integer(), server_default="120", nullable=False),
        sa.Column("max_reserve_minutes", sa.Integer(), server_default="1440", nullable=False),
        sa.Column("ttl_sec", sa.Integer(), server_default="60", nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), server_default="", nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "api_key_scopes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", "scope", name="uq_api_key_scope"),
    )

    # 2. Засевать нечего — и это решение, а не пропущенный шаг (см. шапку).

    # 3. Колонки — нулевыми.
    op.add_column("warehouses", sa.Column("kind", sa.String(length=16), nullable=True))
    op.add_column("products", sa.Column("site_description", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("reserved_until", sa.DateTime(), nullable=True))
    op.add_column("documents", sa.Column("site_ref", ExactString(64), nullable=True))
    op.add_column("documents", sa.Column("api_key_id", sa.Integer(), nullable=True))

    # 4. Заполняем ОДНИМ UPDATE на таблицу, а не обходом через ORM.
    op.execute(sa.text("UPDATE warehouses SET kind = 'stock' WHERE kind IS NULL"))
    op.execute(sa.text("UPDATE products SET site_description = '' WHERE site_description IS NULL"))

    # 5. Ужесточаем и вешаем внешние ключи.
    with op.batch_alter_table("warehouses") as batch:
        batch.alter_column(
            "kind", existing_type=sa.String(length=16), server_default="stock", nullable=False
        )
    with op.batch_alter_table("products") as batch:
        batch.alter_column(
            "site_description",
            existing_type=sa.Text(),
            server_default=sa.text("('')"),
            nullable=False,
        )
    with op.batch_alter_table("documents") as batch:
        batch.create_foreign_key(
            "fk_documents_api_key", "api_keys", ["api_key_id"], ["id"], ondelete="SET NULL"
        )

    # 6. Индексы — последними, по уже заполненным данным.
    op.create_index("ix_api_keys_token_hash", "api_keys", ["token_hash"], unique=True)
    op.create_index("ix_api_keys_warehouse_id", "api_keys", ["warehouse_id"])
    op.create_index("ix_api_keys_created_by", "api_keys", ["created_by"])
    op.create_index("ix_api_key_scopes_api_key_id", "api_key_scopes", ["api_key_id"])
    op.create_index("ix_documents_site_ref", "documents", ["site_ref"], unique=True)
    op.create_index("ix_documents_api_key_id", "documents", ["api_key_id"])
    op.create_index("ix_products_updated_at", "products", ["updated_at"])
    # Лента изменений отбирает движения по дате ЗАПИСИ, а не операции:
    # существующий `ix_stock_moves_wh_happened` стоит по другой дате.
    op.create_index("ix_stock_moves_wh_created", "stock_moves", ["warehouse_id", "created_at"])


def downgrade() -> None:
    # Снимаются индексы, потом внешние ключи, потом колонки, потом таблицы.
    # Теряются ключи, области, типы складов, описания и сроки броней; данные
    # склада, товаров и заказов не трогаются ни строкой.
    # Внешний ключ — раньше индекса под ним: MySQL не снимает индекс, на который
    # опирается ограничение (ошибка 1553).
    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("fk_documents_api_key", type_="foreignkey")
    op.drop_index("ix_stock_moves_wh_created", table_name="stock_moves")
    op.drop_index("ix_products_updated_at", table_name="products")
    op.drop_index("ix_documents_api_key_id", table_name="documents")
    op.drop_index("ix_documents_site_ref", table_name="documents")
    op.drop_column("documents", "api_key_id")
    op.drop_column("documents", "site_ref")
    op.drop_column("documents", "reserved_until")
    op.drop_column("products", "site_description")
    op.drop_column("warehouses", "kind")
    # Индексы сносимых таблиц уходят вместе с ними — снимать их порознь незачем.
    op.drop_table("api_key_scopes")
    op.drop_table("api_keys")
