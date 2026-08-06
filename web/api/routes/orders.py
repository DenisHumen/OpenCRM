"""Заказы: перечень позиций, сборка сканером, отгрузка и приёмка.

Роутер закрыт `require_module("orders")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отвечать тому, кто
помнит адрес.

Заказ — вид бланка, поэтому номер, статусы, печать и поиск сканом живут в
`documents`; здесь только то, чего у квитанции нет.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import order_service, permissions_service
from database.models import User
from database.models.document import ORDER_KINDS
from database.repositories import documents as documents_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm

router = APIRouter(
    prefix="/orders",
    tags=["orders"],
    dependencies=[Depends(require_module("orders"))],
)


class OrderIn(BaseModel):
    kind: str
    client_id: int | None = None
    deal_id: int | None = None
    company_id: int | None = None
    client_name: str | None = None
    locale: str | None = None
    note: str | None = None


class LineIn(BaseModel):
    # Пусто — разовая позиция без карточки товара («доставка», «упаковка»).
    product_id: int | None = None
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class LinePatchIn(BaseModel):
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class PickIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    # Сколько добавить за один писк. Обычно одна штука; коробка со своим кодом
    # приносит столько, сколько в ней лежит (`pack_size_milli` штрихкода).
    quantity_milli: int = Field(default=1000, gt=0)


class CloseIn(BaseModel):
    # Склад выбирается явно: молчаливое списание с основного однажды снимет
    # деталь не оттуда, где её взяли.
    warehouse_id: int | None = None
    # Явное согласие отгрузить больше, чем лежит. По умолчанию отгрузка при
    # нехватке останавливается — отгрузить нечего физически.
    confirm_negative: bool = False


@router.get("")
def list_orders(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    kind: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    kinds = (kind,) if kind in ORDER_KINDS else ORDER_KINDS
    items, total = documents_repo.search(
        db, q=search, status=status, client_id=client_id, deal_id=deal_id,
        kinds=kinds, page=page, per_page=per_page,
    )
    # Строки — одним запросом на страницу, а не запросом на строку списка:
    # сумма заказа складывается из них, и без них список молчит о деньгах.
    rows = documents_repo.lines_by_documents(db, [item.id for item in items])
    amounts = permissions_service.sees_amounts(db, user, "orders")
    return schemas.paginated(
        [schemas.order_out(item, rows.get(item.id, []), amounts=amounts) for item in items],
        total, page, per_page,
    )


@router.post("", status_code=201)
def create_order(
    payload: OrderIn,
    user: User = Depends(require_perm("orders", "create")),
    db: Session = Depends(get_db),
):
    order = order_service.create(db, payload.model_dump(), user)
    return schemas.order_out(order, [], amounts=True)


@router.get("/{order_id}")
def get_order(
    order_id: int,
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    order = order_service.get(db, order_id)
    return schemas.order_out(
        order,
        order_service.lines(db, order.id),
        amounts=permissions_service.sees_amounts(db, user, "orders"),
    )


@router.post("/{order_id}/lines", status_code=201)
def add_line(
    order_id: int,
    payload: LineIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    line = order_service.add_line(db, order_id, payload.model_dump(), user)
    return schemas.order_line_out(line, amounts=permissions_service.sees_amounts(db, user, "orders"))


@router.patch("/{order_id}/lines/{line_id}")
def update_line(
    order_id: int,
    line_id: int,
    payload: LinePatchIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    line = order_service.update_line(db, order_id, line_id, payload.model_dump(exclude_unset=True))
    return schemas.order_line_out(line, amounts=permissions_service.sees_amounts(db, user, "orders"))


@router.delete("/{order_id}/lines/{line_id}")
def remove_line(
    order_id: int,
    line_id: int,
    _: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    order_service.remove_line(db, order_id, line_id)
    return {"message": "Line removed"}


@router.post("/{order_id}/pick")
def pick(
    order_id: int,
    payload: PickIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    """Отметить позицию собранной по отсканированному коду.

    Собранное живёт отдельно от заказанного: расхождение «заказано пять, собрано
    четыре» видно построчно до отгрузки, а не на выдаче.
    """
    line = order_service.pick(db, order_id, payload.code, payload.quantity_milli)
    return schemas.order_line_out(line, amounts=permissions_service.sees_amounts(db, user, "orders"))


@router.post("/{order_id}/ready")
def mark_ready(
    order_id: int,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    order = order_service.mark_ready(db, order_id, user)
    return schemas.order_out(order, order_service.lines(db, order.id), amounts=True)


@router.post("/{order_id}/close")
def close_order(
    order_id: int,
    payload: CloseIn,
    user: User = Depends(require_perm("orders", "issue")),
    db: Session = Depends(get_db),
):
    """Отгрузить заказ покупателя или принять заказ поставщику.

    Право отдельное (`issue`, как выпуск бланка): набирать позиции и двигать
    склад — разные полномочия. Сборщик набирает, отгружает старший.
    """
    order = order_service.close(
        db, order_id, user,
        warehouse_id=payload.warehouse_id,
        confirm_negative=payload.confirm_negative,
    )
    return schemas.order_out(order, order_service.lines(db, order.id), amounts=True)


@router.post("/{order_id}/revert")
def revert_order(
    order_id: int,
    user: User = Depends(require_perm("orders", "issue")),
    db: Session = Depends(get_db),
):
    """Отменить проведение обратными движениями. Прежние остаются на месте."""
    order = order_service.revert(db, order_id, user)
    return schemas.order_out(order, order_service.lines(db, order.id), amounts=True)


@router.post("/{order_id}/cancel")
def cancel_order(
    order_id: int,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    """Отменить непроведённый заказ. Резерв снимется сам — он не хранится."""
    order = order_service.cancel(db, order_id, user)
    return schemas.order_out(order, order_service.lines(db, order.id), amounts=True)
