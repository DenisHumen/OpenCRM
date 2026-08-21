"""Снимки товара: таблица `product_photos`.

Новая таблица и только она — населённых колонок эта правка не трогает, поэтому
опасного места «добавить `NOT NULL` на живых строках» здесь нет. Порядок из
CLAUDE.md соблюдён тем, что шаг ровно один: создать.

Индекс составной, `(product_id, sort_order)`: список снимков читают ровно одним
способом — «все снимки этого товара по порядку», — и отдельный индекс по товару
потребовал бы сортировки поверх.

`downgrade` снимает таблицу целиком. Файлы на диске при этом остаются: откат
базы не обязан быть удалением данных, а восстановленная из копии база вернёт
строки, для которых файлы обязаны найтись на месте. Сироты в
`storage/products/` стоят места, а не правды — и видны менеджеру файлов.

Revision ID: f3c72a19d5e8
Revises: e2f6b81d3c47
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3c72a19d5e8"
down_revision: Union[str, None] = "e2f6b81d3c47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("photo_uid", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("photo_uid"),
    )
    # Индекс ОДИН и составной. Отдельный по `product_id` был бы не просто
    # лишним (составной начинается с той же колонки, и префикс обслуживает и
    # отбор, и внешний ключ) — MySQL не даёт снять индекс, на который опирается
    # внешний ключ, и `downgrade` упирался бы в него ошибкой 1553.
    op.create_index(
        "ix_product_photos_product_order",
        "product_photos",
        ["product_id", "sort_order"],
        unique=False,
    )


def downgrade() -> None:
    # Только таблица: её индексы MySQL снимает вместе с ней, а снимать их
    # заранее по одному значит наткнуться на внешний ключ, который на них
    # опирается.
    op.drop_table("product_photos")
