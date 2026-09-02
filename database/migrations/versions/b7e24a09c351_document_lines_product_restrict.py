"""строка бумаги держит товар: SET NULL → RESTRICT

Revision ID: b7e24a09c351
Revises: a3f81c62d947

**Одинаковые по смыслу таблицы отвечали на удаление товара по-разному.** Строка
заявки (`deal_lines.product_id`) объявлена `RESTRICT` с доводом «как у движения
склада». Строка бумаги — `SET NULL`, и довода при ней не было. Из двух перечней
один был защищён, другой нет.

**Довод «этого всё равно не бывает» неверен, и проверен.** Он держался на том,
что товар удаляют мягко, а прямое удаление отбивает `stock_moves.product_id`
(`RESTRICT`). У УСЛУГИ движений склада не бывает вовсе — значит её ничто не
держит, прямой `DELETE` проходит, и `SET NULL` срабатывает. Дальше молча: строка
проведённой накладной теряет товар и превращается в разовую позицию,
`waybill_service` отбирает строки с непустым `product_id`, и склад по ней больше
не считается. Сторож неизменяемости этого не видит — он стоит событиями ORM, а
`ON DELETE` исполняет сама база.

**Почему RESTRICT, а не наоборот.** Свести всё к `SET NULL` выглядит проще, а
ломает больше: остаток склада есть `SUM(quantity_milli) GROUP BY product_id`, и
обнулённая ссылка вычёркивает движение из суммы. Остаток вырос бы сам собой,
задним числом, без записи о причине.

`nullable=True` остаётся: разовая позиция без карточки товара — законный случай,
и она проходит по-прежнему. Меняется только то, что бывает с УЖЕ проставленной
ссылкой.

**Имя ключа достаётся из базы, а не пишется руками.** В `c3d9f2a71b58` он создан
безымянным, поэтому MySQL зовёт его `document_lines_ibfk_N`, и номер зависит от
порядка колонок в той миграции. `op.drop_constraint("fk_document_lines_…")` упал
бы на несуществующем имени.

Индекс `ix_document_lines_product_id` не трогаем вовсе: снять его при живом
ключе MySQL не даст (ошибка 1553), а пересоздавать незачем — он не меняется.
"""

from alembic import op
from sqlalchemy import inspect

revision = "b7e24a09c351"
down_revision = "a3f81c62d947"
branch_labels = None
depends_on = None

IMYA = "fk_document_lines_product_id"


def _nayti_kluch(nazvanie: str) -> str:
    """Как ключ на `products` зовётся в ЭТОЙ базе.

    Ищем по колонке, а не по имени: имя и есть то, чего мы не знаем.
    """
    inspector = inspect(op.get_bind())
    for kluch in inspector.get_foreign_keys("document_lines"):
        if kluch["referred_table"] == "products":
            return kluch["name"]
    raise RuntimeError(
        f"в document_lines нет внешнего ключа на products — {nazvanie} не сделать"
    )


def _peresobrat(pravilo: str) -> None:
    staroe = _nayti_kluch(pravilo)
    op.drop_constraint(staroe, "document_lines", type_="foreignkey")
    op.create_foreign_key(
        IMYA, "document_lines", "products", ["product_id"], ["id"], ondelete=pravilo
    )


def upgrade() -> None:
    _peresobrat("RESTRICT")


def downgrade() -> None:
    _peresobrat("SET NULL")
