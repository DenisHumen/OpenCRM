"""Накладные: набор позиций, проведение, подтверждение приёмки, сторнирование.

Роутер закрыт `require_module("waybills")` целиком, а не по маршруту:
пропущенный маршрут остался бы открытым, и выключенный блок продолжал бы
отвечать тому, кто помнит адрес.

Накладная — вид бланка, поэтому номер, статусы, печать и поиск сканом живут в
`documents`; здесь только то, чего у квитанции нет.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import permissions_service, waybill_service
from database.models import User
from database.models.document import WAYBILL_KINDS
from database.repositories import documents as documents_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm

router = APIRouter(
    prefix="/waybills",
    tags=["waybills"],
    dependencies=[Depends(require_module("waybills"))],
)


class WaybillIn(BaseModel):
    kind: str
    client_id: int | None = None
    deal_id: int | None = None
    #: На основании чего: заказ для отгрузки по нему.
    basis_id: int | None = None
    #: Склад выбирается явно. Не прислали — основной, и `resolve_warehouse`
    #: откажет, если складов несколько и молчаливый выбор был бы догадкой.
    warehouse_id: int | None = None
    locale: str | None = None
    note: str | None = None


class LineIn(BaseModel):
    product_id: int | None = None
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class LinePatchIn(BaseModel):
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class ProvestiIn(BaseModel):
    #: Явное согласие отгрузить больше, чем лежит. По умолчанию проведение при
    #: нехватке останавливается — отгрузить нечего физически.
    confirm_negative: bool = False


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=200)


def _amounts(db: Session, user: User) -> bool:
    return permissions_service.sees_amounts(db, user, "waybills")


@router.get("")
def list_waybills(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    kind: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("waybills", "view")),
    db: Session = Depends(get_db),
):
    kinds = (kind,) if kind in WAYBILL_KINDS else WAYBILL_KINDS
    items, total = documents_repo.search(
        db, q=search, status=status, client_id=client_id, deal_id=deal_id,
        kinds=kinds, page=page, per_page=per_page,
    )
    # Строки — одним запросом на страницу, а не запросом на строку списка: сумма
    # накладной складывается из них, и без них список молчит о деньгах.
    rows = documents_repo.lines_by_documents(db, [item.id for item in items])
    amounts = _amounts(db, user)
    return schemas.paginated(
        [schemas.waybill_out(item, rows.get(item.id, []), amounts=amounts) for item in items],
        total, page, per_page,
    )


@router.post("", status_code=201)
def create_waybill(
    payload: WaybillIn,
    user: User = Depends(require_perm("waybills", "create")),
    db: Session = Depends(get_db),
):
    waybill = waybill_service.create(db, payload.model_dump(), user)
    return schemas.waybill_out(waybill, [], amounts=_amounts(db, user))


@router.post("/from-order/{order_id}", status_code=201)
def create_from_order(
    order_id: int,
    user: User = Depends(require_perm("waybills", "create")),
    db: Session = Depends(get_db),
):
    """Черновик, заполненный позициями заказа.

    Отдельным маршрутом, а не флагом у создания: перенос позиций — это другое
    действие, а не другой аргумент того же. Флаг заставил бы обе половины ручки
    объяснять, какие поля она сегодня читает.
    """
    waybill = waybill_service.po_zakazu(db, order_id, user)
    return schemas.waybill_out(
        waybill, waybill_service.lines(db, waybill.id), amounts=_amounts(db, user)
    )


@router.get("/{waybill_id}")
def get_waybill(
    waybill_id: int,
    user: User = Depends(require_perm("waybills", "view")),
    db: Session = Depends(get_db),
):
    waybill = waybill_service.get(db, waybill_id)
    return schemas.waybill_out(
        waybill, waybill_service.lines(db, waybill.id), amounts=_amounts(db, user)
    )


@router.get("/{waybill_id}/reversals")
def list_reversals(
    waybill_id: int,
    user: User = Depends(require_perm("waybills", "view")),
    db: Session = Depends(get_db),
):
    """Бумаги, выписанные на основании этой: сторно накладной.

    Нужен экрану, чтобы рядом с проведённой накладной было видно, исправлена ли
    она. Без этого «отгружено шесть» выглядит правдой и после того, как одну
    вернули.
    """
    waybill = waybill_service.get(db, waybill_id)
    amounts = _amounts(db, user)
    return {
        "items": [
            schemas.waybill_out(w, [], amounts=amounts)
            for w in waybill_service.po_osnovaniyu(db, waybill.id)
        ]
    }


@router.post("/{waybill_id}/lines", status_code=201)
def add_line(
    waybill_id: int,
    payload: LineIn,
    user: User = Depends(require_perm("waybills", "edit")),
    db: Session = Depends(get_db),
):
    line = waybill_service.add_line(db, waybill_id, payload.model_dump(), user)
    return schemas.order_line_out(line, amounts=_amounts(db, user))


@router.patch("/{waybill_id}/lines/{line_id}")
def update_line(
    waybill_id: int,
    line_id: int,
    payload: LinePatchIn,
    user: User = Depends(require_perm("waybills", "edit")),
    db: Session = Depends(get_db),
):
    line = waybill_service.update_line(
        db, waybill_id, line_id, payload.model_dump(exclude_unset=True)
    )
    return schemas.order_line_out(line, amounts=_amounts(db, user))


@router.delete("/{waybill_id}/lines/{line_id}")
def remove_line(
    waybill_id: int,
    line_id: int,
    _: User = Depends(require_perm("waybills", "edit")),
    db: Session = Depends(get_db),
):
    waybill_service.remove_line(db, waybill_id, line_id)
    return {"message": "Line removed"}


@router.post("/{waybill_id}/post")
def provesti(
    waybill_id: int,
    payload: ProvestiIn,
    user: User = Depends(require_perm("waybills", "issue")),
    db: Session = Depends(get_db),
):
    """Провести: товар уехал, остаток падает.

    Право `issue`, а не `edit`, и это разные полномочия: кладовщик набирает
    накладную, отпускает старший. Тот же раздел прав, что у заказов.
    """
    waybill = waybill_service.provesti(
        db, waybill_id, user, confirm_negative=payload.confirm_negative
    )
    return schemas.waybill_out(
        waybill, waybill_service.lines(db, waybill.id), amounts=_amounts(db, user)
    )


@router.post("/{waybill_id}/confirm")
def podtverdit(
    waybill_id: int,
    payload: NoteIn,
    user: User = Depends(require_perm("waybills", "edit")),
    db: Session = Depends(get_db),
):
    """Получатель подтвердил приёмку. Ничего не двигает.

    Право `edit`, а не `issue`: переход дешёвый и безопасный — он не трогает ни
    склада, ни денег. Требовать за него право отпуска значило бы звать старшего
    ради отметки о том, что бумагу подписали.
    """
    waybill = waybill_service.podtverdit(db, waybill_id, user, payload.note)
    return schemas.waybill_out(
        waybill, waybill_service.lines(db, waybill.id), amounts=_amounts(db, user)
    )


@router.post("/{waybill_id}/cancel")
def otmenit(
    waybill_id: int,
    payload: NoteIn,
    user: User = Depends(require_perm("waybills", "edit")),
    db: Session = Depends(get_db),
):
    """Отменить черновик. Проведённую — нельзя, для неё сторнирование."""
    waybill = waybill_service.otmenit(db, waybill_id, user, payload.note)
    return schemas.waybill_out(
        waybill, waybill_service.lines(db, waybill.id), amounts=_amounts(db, user)
    )


@router.post("/{waybill_id}/reverse", status_code=201)
def stornirovat(
    waybill_id: int,
    user: User = Depends(require_perm("waybills", "issue")),
    db: Session = Depends(get_db),
):
    """Выписать сторнирующую накладную — черновиком.

    Право `issue`: сторно двинет склад обратно, а это то же полномочие, что и
    двинуть его вперёд. Само движение случится при проведении сторно, но
    решение принимается здесь.
    """
    storno = waybill_service.stornirovat(db, waybill_id, user)
    return schemas.waybill_out(
        storno, waybill_service.lines(db, storno.id), amounts=_amounts(db, user)
    )
