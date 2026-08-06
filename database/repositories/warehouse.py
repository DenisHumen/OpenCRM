"""Запросы склада.

Здесь живёт главное правило блока: **остаток считается запросом**.

Соблазн загрузить движения товара и сложить их в Python велик, но он ломается
ровно там, где это дороже всего: список товаров показывает первые 50 движений, а
их 3000 — и остаток тихо оказывается неверным, причём выглядит правдоподобно.
Поэтому суммирует всегда база: `SUM(quantity_milli) GROUP BY product_id` не
зависит ни от пагинации, ни от того, что успело попасть в сессию.
"""

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from database.models import Product, StockMove
from database.models.warehouse import QUANTITY_SCALE


def get_product(db: Session, product_id: int, include_deleted: bool = False) -> Product | None:
    product = db.get(Product, product_id)
    if product is None:
        return None
    if product.deleted_at is not None and not include_deleted:
        return None
    return product


def get_by_sku(db: Session, sku: str) -> Product | None:
    """Ищет и среди удалённых: артикул уникален по всей таблице, включая корзину."""
    return db.scalar(select(Product).where(Product.sku == sku))


def search_products(
    db: Session,
    q: str | None = None,
    include_services: bool = True,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Product], int]:
    stmt = select(Product).where(Product.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(like), Product.sku.ilike(like)))
    if not include_services:
        stmt = stmt.where(Product.is_service.is_(False))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(Product.name).offset((page - 1) * per_page).limit(per_page)
    return list(db.scalars(stmt)), total


def names_of(db: Session, product_ids: set[int]) -> dict[int, tuple[str, str]]:
    """{id: (название, единица)} — для строк истории и врезки в карточке заявки."""
    if not product_ids:
        return {}
    rows = db.execute(
        select(Product.id, Product.name, Product.unit).where(Product.id.in_(product_ids))
    ).all()
    return {product_id: (name, unit) for product_id, name, unit in rows}


def stock_of(db: Session, product_id: int) -> int:
    """Остаток одного товара в тысячных долях единицы.

    coalesce — потому что SUM по пустому набору даёт NULL, а «движений не было»
    означает ровно ноль, а не «неизвестно».
    """
    return (
        db.scalar(
            select(func.coalesce(func.sum(StockMove.quantity_milli), 0)).where(
                StockMove.product_id == product_id
            )
        )
        or 0
    )


def stock_by_product(db: Session, product_ids: list[int] | None = None) -> dict[int, int]:
    """Остатки пачкой: {product_id: остаток в тысячных}.

    Один запрос на весь список товаров вместо запроса на строку — иначе экран
    склада на 500 позиций делает 500 обращений к базе.

    Если когда-нибудь этого станет мало (десятки тысяч движений на товар), кэш
    остатка следует завести отдельной таблицей `product_stock`, пересчитываемой
    в той же транзакции, что и вставка движения, — и обязательно с фоновой
    сверкой против этого запроса. Источником правды кэш при этом не становится
    НИКОГДА: разойдясь с историей однажды, он не даст способа узнать, какое из
    двух чисел верное, и склад придётся пересчитывать вручную по бумагам.
    """
    stmt = select(StockMove.product_id, func.coalesce(func.sum(StockMove.quantity_milli), 0))
    if product_ids is not None:
        if not product_ids:
            return {}
        stmt = stmt.where(StockMove.product_id.in_(product_ids))
    stmt = stmt.group_by(StockMove.product_id)
    return {product_id: total for product_id, total in db.execute(stmt).all()}


def _moves_base(product_id: int | None, deal_id: int | None) -> Select:
    stmt = select(StockMove)
    if product_id is not None:
        stmt = stmt.where(StockMove.product_id == product_id)
    if deal_id is not None:
        stmt = stmt.where(StockMove.deal_id == deal_id)
    return stmt


def list_moves(
    db: Session,
    product_id: int | None = None,
    deal_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[StockMove], int]:
    base = _moves_base(product_id, deal_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = (
        base.order_by(StockMove.happened_at.desc(), StockMove.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return list(db.scalars(stmt)), total


#: количество × цена даёт «минорные единицы, умноженные на QUANTITY_SCALE» —
#: сумма копится в этом масштабе и приводится к деньгам один раз в конце
_COST_EXPR = func.coalesce(
    func.sum(-StockMove.quantity_milli * func.coalesce(StockMove.cost_minor, 0)), 0
)


def _scaled_to_minor(total: int) -> int:
    """Масштабированная сумма → минорные единицы, целочисленно.

    Делить на 1000 обычным `/` нельзя: в Python это float, а деньги через float
    мы не считаем принципиально. Округление — к ближайшему от нуля, чтобы возврат
    на склад (отрицательный вклад) округлялся так же, как списание.
    """
    half = QUANTITY_SCALE // 2
    if total >= 0:
        return (total + half) // QUANTITY_SCALE
    return -((-total + half) // QUANTITY_SCALE)


# Себестоимости ОДНОГО движения здесь больше нет: единственным её читателем был
# подписчик, вписывавший сумму в строку ленты, а лента вычёркивать деньги не
# умеет и проносила их мимо `warehouse.view_amounts` (см. `core/subscriptions.py`).
# Понадобится снова — считать её обязательно здесь же, рядом с `deal_cost_minor`
# и его округлением: посчитанная отдельно, она разойдётся с итогом во врезке на
# копейку, и объяснить расхождение будет нечем.


def deal_cost_minor(db: Session, deal_id: int) -> int:
    """Себестоимость товаров, списанных под заявку, в минорных единицах.

    Считается запросом по той же причине, что и остаток. Расход хранится
    отрицательным количеством, поэтому знак переворачиваем: себестоимость заявки —
    число положительное, а возврат на склад её уменьшает.
    """
    return _scaled_to_minor(db.scalar(select(_COST_EXPR).where(StockMove.deal_id == deal_id)) or 0)


def deal_cost_by_deal(db: Session, deal_ids: list[int]) -> dict[int, int]:
    """То же пачкой — для списка заявок."""
    if not deal_ids:
        return {}
    rows = db.execute(
        select(StockMove.deal_id, _COST_EXPR)
        .where(StockMove.deal_id.in_(deal_ids))
        .group_by(StockMove.deal_id)
    ).all()
    return {deal_id: _scaled_to_minor(total or 0) for deal_id, total in rows}
