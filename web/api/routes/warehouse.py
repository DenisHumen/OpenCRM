"""Склад: товары, остатки, движения.

Роутер закрыт `require_module("warehouse")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отдавать данные
тому, кто помнит адрес.

Ни один обработчик здесь не складывает движения сам — остаток приходит из
агрегата репозитория (`database/repositories/warehouse.py`).
"""

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    permissions_service,
    product_photo_service,
    settings_service,
    warehouse_service,
)
from database.models import User
from database.repositories import users as users_repo
from database.repositories import warehouse as warehouse_repo
from database.repositories import warehouses as places_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm

class PhotoOrderIn(BaseModel):
    """Полный порядок снимков товара.

    Целиком, а не «подвинуть этот на одну позицию»: частичная перестановка
    требует знать, что было до неё, и двое, двигающие соседние снимки, получили
    бы порядок, которого не задавал ни один.
    """

    order: list[int]


router = APIRouter(
    prefix="/warehouse",
    tags=["warehouse"],
    dependencies=[Depends(require_module("warehouse"))],
)

#: Склады как места — отдельным ресурсом верхнего уровня.
#:
#: Адрес `/warehouse/warehouses` читался бы как оговорка, а склад — не часть
#: карточки товара: это место, у которого своя жизнь, свои права и свой журнал.
#: Гейт блока тот же и назван явно: пропусти его — и выключенный склад
#: продолжал бы отдавать список мест тому, кто помнит адрес.
places_router = APIRouter(
    prefix="/warehouses",
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
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    low_only: bool = False,
    include_services: bool = True,
    # Остаток одного склада вместо суммы по всем: «а на точке-то оно есть?».
    warehouse_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    amounts = permissions_service.sees_amounts(db, user, "warehouse")
    items, total = warehouse_repo.search_products(
        db, q=search, include_services=include_services, page=page, per_page=per_page
    )
    goods = [p.id for p in items if not p.is_service]
    # Остатки — одним запросом на всю страницу списка, а не по одному на строку.
    stock = warehouse_repo.stock_by_product(db, goods, warehouse_id=warehouse_id)
    # Раскладка «где и сколько» — тоже одним запросом и только когда складов
    # больше одного: при единственном складе она повторяла бы общий остаток.
    many = warehouse_service.warehouse_count(db) > 1
    spread = warehouse_repo.stock_by_warehouse(db, goods) if many else {}
    rows = []
    for p in items:
        row = schemas.product_out(
            p, None if p.is_service else stock.get(p.id, 0), amounts=amounts
        )
        if many and not p.is_service:
            row["by_warehouse"] = spread.get(p.id, {})
        rows.append(row)
    if low_only:
        # Фильтр применяется после подсчёта остатков: «мало» — свойство не строки
        # в таблице, а суммы движений, и в WHERE его не выразить, не повторив тот
        # же агрегат. `total` остаётся числом товаров, а не строк после фильтра:
        # иначе пагинация прыгала бы при каждом движении.
        rows = [row for row in rows if row["low_stock"]]
    data = schemas.paginated(rows, total, page, per_page)
    data["currency"] = _currency(db)
    return data


# Ответ пишущей ручки — такая же карточка товара, как у GET, и суммы в ней
# закрываются тем же правом. Умолчание `amounts=True` в сериализаторе делало
# из PATCH обход: кладовщик без `warehouse.view_amounts` не видел закупочную
# цену в списке и в карточке, но получал её в ответ на переименование товара.
@router.post("/products", status_code=201)
def create_product(
    payload: schemas.ProductIn,
    user: User = Depends(require_perm("warehouse", "create")),
    db: Session = Depends(get_db),
):
    product = warehouse_service.create_product(db, payload.model_dump())
    return schemas.product_out(
        product,
        None if product.is_service else 0,
        amounts=permissions_service.sees_amounts(db, user, "warehouse"),
    )


@router.get("/products/{product_id}")
def get_product(
    product_id: int,
    user: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    product = warehouse_service.get_product(db, product_id)
    data = schemas.product_out(
        product,
        warehouse_service.stock_of(db, product),
        amounts=permissions_service.sees_amounts(db, user, "warehouse"),
    )
    # Раскладка «где и сколько» — здесь же, а не отдельной ручкой и не выуживанием
    # карточки из списка: остаток один, и способов посчитать его должно быть
    # столько же. Только когда складов больше одного — иначе она повторяла бы
    # общий остаток строка в строку.
    if not product.is_service and warehouse_service.warehouse_count(db) > 1:
        data["by_warehouse"] = warehouse_repo.stock_by_warehouse(db, [product.id]).get(
            product.id, {}
        )
    data["currency"] = _currency(db)
    return data


@router.patch("/products/{product_id}")
def update_product(
    product_id: int,
    payload: schemas.ProductPatchIn,
    user: User = Depends(require_perm("warehouse", "edit")),
    db: Session = Depends(get_db),
):
    product = warehouse_service.update_product(
        db, product_id, payload.model_dump(exclude_unset=True)
    )
    return schemas.product_out(
        product,
        warehouse_service.stock_of(db, product),
        amounts=permissions_service.sees_amounts(db, user, "warehouse"),
    )


@router.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    user: User = Depends(require_perm("warehouse", "delete")),
    db: Session = Depends(get_db),
):
    warehouse_service.delete_product(db, product_id, user)
    return {"message": "Product deleted"}


@router.post("/products/{product_id}/restore")
def restore_product(
    product_id: int,
    user: User = Depends(require_perm("warehouse", "restore")),
    db: Session = Depends(get_db),
):
    product = warehouse_service.restore_product(db, product_id, user)
    return schemas.product_out(
        product,
        warehouse_service.stock_of(db, product),
        amounts=permissions_service.sees_amounts(db, user, "warehouse"),
    )


@router.get("/products/{product_id}/moves")
def product_moves(
    product_id: int,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    """История по товару. Товар ищем вместе с удалёнными: карточки нет, а история есть."""
    amounts = permissions_service.sees_amounts(db, user, "warehouse")
    product = warehouse_service.get_product(db, product_id, include_deleted=True)
    items, total = warehouse_repo.list_moves(db, product_id=product.id, page=page, per_page=per_page)
    data = schemas.paginated(
        _decorate(db, [schemas.stock_move_out(m, amounts=amounts) for m in items]),
        total,
        page,
        per_page,
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
    warehouse_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    amounts = permissions_service.sees_amounts(db, user, "warehouse")
    items, total = warehouse_repo.list_moves(
        db, product_id=product_id, deal_id=deal_id, warehouse_id=warehouse_id,
        page=page, per_page=per_page
    )
    data = schemas.paginated(
        _decorate(db, [schemas.stock_move_out(m, amounts=amounts) for m in items]),
        total,
        page,
        per_page,
    )
    data["currency"] = _currency(db)
    if deal_id is not None:
        # Врезка в карточке заявки: во сколько списанные товары обошлись складу.
        # Считает база — сумма по видимой странице занизила бы итог.
        data["cost"] = warehouse_repo.deal_cost_minor(db, deal_id) if amounts else None
    return data


@router.post("/moves", status_code=201)
def create_move(
    payload: schemas.StockMoveIn,
    user: User = Depends(require_perm("warehouse", "create")),
    db: Session = Depends(get_db),
):
    move, went_negative = warehouse_service.add_move(db, payload.model_dump(), user)
    data = schemas.stock_move_out(
        move, amounts=permissions_service.sees_amounts(db, user, "warehouse")
    )
    # Уход в минус — предупреждение, а не отказ (обоснование — в add_move).
    # Движение уже записано; клиент показывает предупреждение оператору.
    data["went_negative"] = went_negative
    data["stock_milli"] = warehouse_repo.stock_of(db, move.product_id)
    return data


# --- склады как места ---------------------------------------------------------
#
# Заводить склады — действие структурное, вроде «завести юрлицо», поэтому оно на
# `warehouse.manage`, а не на `create`: приход, расход и перемещение остаются у
# кладовщика, а склады заводит тот, кто отвечает за структуру.


@places_router.get("")
def list_warehouses(
    _: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    """Список складов. `many` — показывать ли выбор склада в формах.

    Считает сервер, а не экран: правило «выбор появляется, когда складов больше
    одного» посчитанное на фронте стало бы вторым экземпляром того же правила, а
    два экземпляра расходятся молча.
    """
    items = warehouse_service.list_warehouses(db)
    return {"items": [schemas.warehouse_out(w) for w in items], "many": len(items) > 1}


@places_router.post("", status_code=201)
def create_warehouse(
    payload: schemas.WarehouseIn,
    _: User = Depends(require_perm("warehouse", "manage")),
    db: Session = Depends(get_db),
):
    return schemas.warehouse_out(warehouse_service.create_warehouse(db, payload.model_dump()))


@places_router.patch("/{warehouse_id}")
def update_warehouse(
    warehouse_id: int,
    payload: schemas.WarehousePatchIn,
    _: User = Depends(require_perm("warehouse", "manage")),
    db: Session = Depends(get_db),
):
    return schemas.warehouse_out(
        warehouse_service.update_warehouse(
            db, warehouse_id, payload.model_dump(exclude_unset=True)
        )
    )


@places_router.delete("/{warehouse_id}")
def close_warehouse(
    warehouse_id: int,
    user: User = Depends(require_perm("warehouse", "manage")),
    db: Session = Depends(get_db),
):
    """Закрыть склад. Последний и непустой закрыть нельзя — почему, в сервисе."""
    warehouse_service.close_warehouse(db, warehouse_id, user)
    return {"message": "Warehouse closed"}


# --- переезды -----------------------------------------------------------------


@router.get("/transfers")
def list_transfers(
    warehouse_id: int | None = None,
    product_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    """Журнал переездов — отдельным списком, а не строками в общей истории.

    В общем списке движений переезд читается плохо: две строки подряд с разными
    знаками, и понять, одно это событие или два разных, можно только по времени
    и по памяти. Здесь он — одна запись: что, сколько, откуда, куда и кто.
    """
    headers, total = places_repo.list_transfers(
        db, warehouse_id=warehouse_id, product_id=product_id, page=page, per_page=per_page
    )
    # Три запроса на страницу, а не три на строку: имена складов и позиции
    # переездов берём пачкой — ровно как остатки в списке товаров.
    rows = places_repo.moves_by_transfers(db, [h.id for h in headers])
    names = places_repo.names_of(
        db, [h.from_warehouse_id for h in headers] + [h.to_warehouse_id for h in headers]
    )
    reverted = places_repo.reverted_ids(db, [h.id for h in headers])
    items = [
        schemas.transfer_out(h, rows.get(h.id, []), names, reverted=h.id in reverted)
        for h in headers
    ]
    return schemas.paginated(items, total, page, per_page)


@router.post("/transfers", status_code=201)
def create_transfer(
    payload: schemas.TransferIn,
    user: User = Depends(require_perm("warehouse", "create")),
    db: Session = Depends(get_db),
):
    """Перевезти товар. Две строки движения в одной транзакции — см. сервис."""
    header = warehouse_service.transfer(db, payload.model_dump(), user)
    moves = places_repo.moves_of_transfer(db, header.id)
    names = places_repo.names_of(db, [header.from_warehouse_id, header.to_warehouse_id])
    return schemas.transfer_out(header, moves, names)


@router.post("/transfers/{transfer_id}/revert", status_code=201)
def revert_transfer(
    transfer_id: int,
    user: User = Depends(require_perm("warehouse", "create")),
    db: Session = Depends(get_db),
):
    """Отменить переезд обратным переездом. Дважды один и тот же — отказ."""
    header = warehouse_service.revert_transfer(db, transfer_id, user)
    moves = places_repo.moves_of_transfer(db, header.id)
    names = places_repo.names_of(db, [header.from_warehouse_id, header.to_warehouse_id])
    return schemas.transfer_out(header, moves, names)


# --- снимки товара -------------------------------------------------------------
#
# Название опознаёт вещь плохо: «шлейф 40-pin» и «шлейф 40-pin (узкий)» —
# отличить их на полке можно только глазами. Разбор устройства — в
# `core/services/product_photo_service.py`.


def _photo_out(photo) -> dict:
    return {
        "id": photo.id,
        "original_name": photo.original_name,
        "size_bytes": photo.size_bytes,
        "sort_order": photo.sort_order,
        "created_at": photo.created_at.isoformat() if photo.created_at else None,
    }


@router.get("/products/{product_id}/photos")
def list_photos(
    product_id: int,
    _: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    warehouse_service.get_product(db, product_id)  # нет товара — честный 404
    return {"items": [_photo_out(p) for p in product_photo_service.spisok(db, product_id)]}


@router.post("/products/{product_id}/photos", status_code=201)
async def add_photo(
    product_id: int,
    file: UploadFile,
    user: User = Depends(require_perm("warehouse", "edit")),
    db: Session = Depends(get_db),
):
    content = await file.read()
    photo = product_photo_service.dobavit(
        db, product_id, user, file.filename or "photo", content
    )
    return _photo_out(photo)


@router.get("/products/{product_id}/photos/{photo_id}")
def download_photo(
    product_id: int,
    photo_id: int,
    size: str = Query("view", pattern="^(view|thumb)$"),
    _: User = Depends(require_perm("warehouse", "view")),
    db: Session = Depends(get_db),
):
    """Отдать снимок. Через приложение, а не статикой.

    Тот же довод, что у файлов клиента: остаток на складе и то, как выглядит
    деталь, — сведения фирмы, и ссылка на них не должна работать у всякого, кто
    её узнал. Медиа досок отдаётся напрямую nginx, но там это осознанный обмен:
    витрину показывают клиенту.
    """
    photo = product_photo_service.poluchit(db, product_id, photo_id)
    put = product_photo_service.put_na_diske(photo, size)
    if not put.exists():
        raise errors.NotFoundError("Photo is missing on disk", code="photo_missing")
    return FileResponse(
        put,
        media_type="image/webp",
        headers={
            "X-Content-Type-Options": "nosniff",
            # Имя файла на диске неизменяемо, содержимое под ним — тоже:
            # правка снимка означает новый снимок. Значит кэшировать можно
            # надолго, и список товаров перестаёт перекачивать плитки.
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.delete("/products/{product_id}/photos/{photo_id}")
def delete_photo(
    product_id: int,
    photo_id: int,
    user: User = Depends(require_perm("warehouse", "edit")),
    db: Session = Depends(get_db),
):
    product_photo_service.udalit(db, product_id, photo_id, user)
    return {"message": "Photo deleted"}


@router.put("/products/{product_id}/photos/order")
def reorder_photos(
    product_id: int,
    payload: PhotoOrderIn,
    _: User = Depends(require_perm("warehouse", "edit")),
    db: Session = Depends(get_db),
):
    """Задать порядок. Первый снимок — тот, что показывают везде, где место одно."""
    warehouse_service.get_product(db, product_id)
    items = product_photo_service.perestavit(db, product_id, payload.order)
    return {"items": [_photo_out(p) for p in items]}
