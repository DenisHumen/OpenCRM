"""product barcodes

Revision ID: a1f5c8d2b704
Revises: f9b41c7e2d08
Create Date: 2026-08-06 20:00:00.000000

Штрихкоды товара — отдельной таблицей, а не колонкой в `products`.

Колонка ломается на второй месяц: у товара бывает несколько упаковок со своими
кодами, поставщик перевыпускает код на том же товаре, один товар приходит от
двух поставщиков с разными кодами, а у собственного изделия заводского кода нет
вовсе. Разбор — в докстроке модели `ProductBarcode`.

**Миграция добавочная.** Ни одной существующей таблицы она не трогает: на
боевом сервере это создание одной пустой таблицы, данные не переписываются и
простоя не добавляется. Откат — удаление той же таблицы; ничего, кроме самих
штрихкодов, при этом не теряется.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1f5c8d2b704"
down_revision: Union[str, None] = "f9b41c7e2d08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_barcodes",
        sa.Column("id", sa.Integer(), nullable=False),
        # CASCADE: штрихкод — свойство карточки, а не история. Ушла карточка
        # по-настоящему — уходят и коды. Движения склада при этом остаются, у
        # них свой RESTRICT.
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        # Сколько единиц товара в упаковке, в тысячных: штука — 1000, блок из
        # десяти — 10000. Отсканировали коробку — в заказ уходит десять штук.
        sa.Column("pack_size_milli", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Уникальность в базе, а не в сервисе: двое приёмщиков заводят карточки
    # одновременно, и окно между «проверили» и «вставили» есть всегда. Один код
    # не может означать два товара — иначе сканирование перестаёт быть
    # однозначным, а однозначность и есть весь его смысл.
    op.create_index("ix_product_barcodes_code", "product_barcodes", ["code"], unique=True)
    op.create_index("ix_product_barcodes_product_id", "product_barcodes", ["product_id"])


def downgrade() -> None:
    # Индексы отдельно не снимаем — их уносит `drop_table`, а на MySQL снятие
    # индекса под живым внешним ключом отвергается (1553). Разбор — в откате
    # `e4451c527c34`. Здесь на это упирался `ix_product_barcodes_product_id`.
    op.drop_table("product_barcodes")
