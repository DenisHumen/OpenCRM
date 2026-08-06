"""Склад: товары, остатки, движения.

Роутер закрыт `require_module("warehouse")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отдавать данные
тому, кто помнит адрес.

Ни один обработчик здесь не складывает движения сам — остаток приходит из
агрегата репозитория (`database/repositories/warehouse.py`).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.services import settings_service, warehouse_service
from database.models import User
from database.repositories import users as users_repo
from database.repositories import warehouse as warehouse_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_staff

router = APIRouter(
    prefix="/warehouse",
    tags=["warehouse"],
    dependencies=[Depends(require_module("warehouse"))],
)


def _currency(db: Session) -> str:
    """Валюта — в ответ, как у сделок: полные настройки читает только root, а
    сумму без обозначения показывать нельзя никому."""
    return settings_service.get_all(db).get("currency", "USD")


def _decorate(db: Session, moves: list[dict]) -> list[dict]:
    """Дописывает к движениям имена автора и товара — двумя запросами на выдачу.

    «Кто списал» и «что списал» — первые два вопроса к любой строке истории, а по
    запросу на строку журнал из двухсот движений сделал бы четыреста обращений
    к базе.
    """
    ids = {move["author_id"] for move in moves if move["author_id"]}
    names = {u.id: u.name for u in users_repo.get_many(db, ids)} if ids else {}
    products = warehouse_repo.names_of(db, {move["product_id"] for move in moves})
    for move in moves:
        move["author_name"] = names.get(move["author_id"])
        product = products.get(move["product_id"])
        move["product_name"] = product[0] if product else None
        move["unit"] = product[1] if product else None
    return moves


# --- товары ---

@router.get("/products")
def list_products(
    search: str | None = None,
    low_only: bool = False,
    include_services: bool = True,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    items, total = warehouse_repo.search_products(
        db, q=search, include_services=include_services, page=page, per_page=per_page
    )
    # Остатки — одним запросом на всю страницу списка, а не по одному на строку.
    stock = warehouse_repo.stock_by_product(db, [p.id for p in items if not p.is_service])
    rows = [
        schemas.product_out(p, None if p.is_service else stock.get(p.id, 0)) for p in items
    ]
    if low_only:
        # Фильтр применяется после подсчёта остатков: «мало» — свойство не строки
        # в таблице, а суммы движений, и в WHERE его не выразить, не повторив тот
        # же агрегат. `total` остаётся числом товаров, а не строк после фильтра:
        # иначе пагинация прыгала бы при каждом движении.
        rows = [row for row in rows if row["low_stock"]]
    data = schemas.paginated(rows, total, page, per_page)
    data["currency"] = _currency(db)
    return data


@router.post("/products", status_code=201)
def create_product(
    payload: schemas.ProductIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    product = warehouse_service.create_product(db, payload.model_dump())
    return schemas.product_out(product, None if product.is_service else 0)


@router.get("/products/{product_id}")
def get_product(
    product_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    product = warehouse_service.get_product(db, product_id)
    data = schemas.product_out(product, warehouse_service.stock_of(db, product))
    data["currency"] = _currency(db)
    return data


@router.patch("/products/{product_id}")
def update_product(
    product_id: int,
    payload: schemas.ProductPatchIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    product = warehouse_service.update_product(
        db, product_id, payload.model_dump(exclude_unset=True)
    )
    return schemas.product_out(product, warehouse_service.stock_of(db, product))


@router.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    warehouse_service.delete_product(db, product_id, user)
    return {"message": "Product deleted"}


@router.post("/products/{product_id}/restore")
def restore_product(
    product_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    product = warehouse_service.restore_product(db, product_id)
    return schemas.product_out(product, warehouse_service.stock_of(db, product))


@router.get("/products/{product_id}/moves")
def product_moves(
    product_id: int,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """История по товару. Товар ищем вместе с удалёнными: карточки нет, а история есть."""
    product = warehouse_service.get_product(db, product_id, include_deleted=True)
    items, total = warehouse_repo.list_moves(db, product_id=product.id, page=page, per_page=per_page)
    data = schemas.paginated(
        _decorate(db, [schemas.stock_move_out(m) for m in items]), total, page, per_page
    )
    # Остаток отдаём рядом с историей и считаем запросом: страница показывает
    # 50 движений из 3000, и сложить видимые значило бы соврать.
    data["stock_milli"] = warehouse_service.stock_of(db, product)
    data["currency"] = _currency(db)
    return data


# --- движения ---

@router.get("/moves")
def list_moves(
    product_id: int | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    items, total = warehouse_repo.list_moves(
        db, product_id=product_id, deal_id=deal_id, page=page, per_page=per_page
    )
    data = schemas.paginated(
        _decorate(db, [schemas.stock_move_out(m) for m in items]), total, page, per_page
    )
    data["currency"] = _currency(db)
    if deal_id is not None:
        # Врезка в карточке заявки: во сколько списанные товары обошлись складу.
        # Считает база — сумма по видимой странице занизила бы итог.
        data["cost"] = warehouse_repo.deal_cost_minor(db, deal_id)
    return data


@router.post("/moves", status_code=201)
def create_move(
    payload: schemas.StockMoveIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    move, went_negative = warehouse_service.add_move(db, payload.model_dump(), user)
    data = schemas.stock_move_out(move)
    # Уход в минус — предупреждение, а не отказ (обоснование — в add_move).
    # Движение уже записано; клиент показывает предупреждение оператору.
    data["went_negative"] = went_negative
    data["stock_milli"] = warehouse_repo.stock_of(db, move.product_id)
    return data
