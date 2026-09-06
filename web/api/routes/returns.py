"""Возвраты покупателя: строки, вложения, проведение, статистика.

Роутер закрыт `require_module("orders")` целиком: возврат — действие по
заказу, живёт в его блоке и под его правами (`orders.*`). Заводится с карточки
заказа (`POST /orders/{id}/returns`); здесь — всё, что происходит с уже
заведённым возвратом.
"""

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    modules_service,
    order_service,
    permissions_service,
    report_service,
    return_service,
    settings_service,
)
from database.models import User
from database.models.document import KIND_RETURN, WAYBILL_KINDS
from database.repositories import clients as clients_repo
from database.repositories import documents as documents_repo
from database.repositories import users as users_repo
from database.repositories import vozvraty as vozvraty_repo
from fastapi.responses import HTMLResponse
from core.services.barcode_service import UNIT_NAMES
from core.utils import money_for_print
from database.repositories import warehouse as warehouse_repo
from web.public import routes as public_routes
from core.services import codes
from core.services import document_service
from core.services import warehouse_service
from database.models.document import STATUS_CLOSED
from database.models.document import DOCUMENT_LOCALES
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm

router = APIRouter(
    prefix="/returns",
    tags=["returns"],
    dependencies=[Depends(require_module("orders"))],
)


class ReturnPatchIn(BaseModel):
    note: str | None = None
    #: Сумма к возврату клиенту, минорные единицы. Ноль законен: обмен без денег.
    refund: int | str | None = None
    client_id: int | None = None
    #: Доходная статья, через которую деньги уходят обратно. Нужна при
    #: включённых финансах и ненулевой сумме — проверяет проведение.
    category_id: int | None = None


class LineIn(BaseModel):
    product_id: int
    quantity: str | int | float | None = None


class LinePatchIn(BaseModel):
    quantity: str | int | float | None = None


class PostIn(BaseModel):
    warehouse_id: int | None = None


class NoteIn(BaseModel):
    note: str = ""


def _amounts(db: Session, user: User) -> bool:
    return permissions_service.sees_amounts(db, user, "orders")


def _imya_klienta(db: Session, vozvrat) -> str | None:
    if not vozvrat.client_id:
        return None
    client = clients_repo.get(db, vozvrat.client_id, include_deleted=True)
    return client.name if client else None


def _out(db: Session, user: User, vozvrat) -> dict:
    order = return_service.zakaz(db, vozvrat)
    return schemas.return_out(
        vozvrat,
        return_service.lines(db, vozvrat.id),
        amounts=_amounts(db, user),
        client_name=_imya_klienta(db, vozvrat),
        order_number=order.number,
    )


@router.get("")
def list_returns(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    status: str | None = None,
    sort: str | None = None,
    client_id: int | None = None,
    order_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    """Список возвратов. Категория — состояние, как у заказов."""
    if sort and sort not in documents_repo.PORYADKI:
        raise errors.ValidationError(
            f"Unknown sort: {sort}. Known: {', '.join(sorted(documents_repo.PORYADKI))}",
            code="unknown_sort",
        )
    items, total = documents_repo.search(
        db, q=search, status=status, client_id=client_id, basis_id=order_id,
        kinds=(KIND_RETURN,), sort=sort, page=page, per_page=per_page,
    )
    rows = documents_repo.lines_by_documents(db, [item.id for item in items])
    amounts = _amounts(db, user)
    imena = clients_repo.names_by_ids(db, [item.client_id for item in items if item.client_id])
    nomera = {
        o.id: o.number
        for o in documents_repo.po_ids(db, {item.basis_id for item in items if item.basis_id})
    }
    otvet = schemas.paginated(
        [
            schemas.return_out(
                item, rows.get(item.id, []), amounts=amounts,
                client_name=imena.get(item.client_id), order_number=nomera.get(item.basis_id),
            )
            for item in items
        ],
        total, page, per_page,
    )
    otvet["counts"] = documents_repo.schyot_po_statusam(
        db, q=search, client_id=client_id, basis_id=order_id, kinds=(KIND_RETURN,)
    )
    return otvet


@router.get("/stats")
def stats(
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    tz_offset: int = Query(default=0, ge=-840, le=840),
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    """Статистика возвратов за период: сколько, на сколько, доля от отгрузок,
    по месяцам и что возвращают. Деньги — только тому, кто видит суммы."""
    start, end, start_day, end_day = report_service.parse_period(date_from, date_to, tz_offset)
    buckets = report_service.month_buckets(start_day, end_day, tz_offset)
    itog = vozvraty_repo.svodka(db, start, end)
    po_mesyatsam = vozvraty_repo.po_mesyatsam(db, [(s, e) for _label, s, e in buckets])
    amounts = _amounts(db, user)
    dengi = (lambda x: x) if amounts else (lambda _x: None)
    return {
        "from": start_day.isoformat(),
        "to": end_day.isoformat(),
        "currency": settings_service.get_all(db).get("currency", "USD"),
        "count": itog["count"],
        "refund_amount": dengi(itog["refund"]),
        "avg_refund": dengi(itog["refund"] // itog["count"]) if itog["count"] else None,
        "shipped_count": itog["shipped_count"],
        "share": report_service.share(itog["count"], itog["shipped_count"]),
        "months": [
            {
                "month": label,
                "count": po_mesyatsam.get(index, {}).get("count", 0),
                "refund_amount": dengi(po_mesyatsam.get(index, {}).get("refund", 0)),
                "shipped_count": po_mesyatsam.get(index, {}).get("shipped_count", 0),
            }
            for index, (label, _s, _e) in enumerate(buckets)
        ],
        "products": vozvraty_repo.tovary(db, start, end),
    }


@router.get("/{return_id}")
def get_return(
    return_id: int,
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    vozvrat = return_service.get(db, return_id)
    order = return_service.zakaz(db, vozvrat)
    data = _out(db, user, vozvrat)
    data["files"] = [schemas.document_file_out(f) for f in return_service.files(db, vozvrat.id)]
    # Сколько ещё можно вернуть — считается сервером: экран, считающий это
    # сам, завёл бы второй ответ на тот же вопрос.
    mozhno = return_service.dostupno(db, order, krome_id=vozvrat.id)
    data["order_lines"] = [
        {
            "product_id": row.product_id,
            "name": row.name_snapshot,
            "price": row.price_minor if _amounts(db, user) else None,
            "max_milli": mozhno.get(row.product_id, 0),
        }
        for row in order_service.lines(db, order.id)
        if row.product_id is not None and row.product_id in mozhno
    ]
    if modules_service.is_enabled(db, "waybills"):
        bumagi, _vsego = documents_repo.search(
            db, basis_id=vozvrat.id, kinds=WAYBILL_KINDS, page=1, per_page=50
        )
        data["waybills"] = [
            {"id": w.id, "number": w.number, "kind": w.kind, "status": w.status} for w in bumagi
        ]
    events = return_service.events(db, vozvrat.id)
    authors = {
        u.id: u.name
        for u in users_repo.get_many(db, {e.author_id for e in events if e.author_id})
    }
    data["events"] = [
        {
            "id": e.id,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "note": e.note,
            "author_name": (e.author_name or "") or authors.get(e.author_id),
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
    return data


@router.patch("/{return_id}")
def update_return(
    return_id: int,
    payload: ReturnPatchIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    vozvrat = return_service.pravit(db, return_id, payload.model_dump(exclude_unset=True))
    return _out(db, user, vozvrat)


@router.post("/{return_id}/lines", status_code=201)
def add_line(
    return_id: int,
    payload: LineIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    line = return_service.add_line(db, return_id, payload.model_dump(), user)
    return schemas.order_line_out(line, amounts=_amounts(db, user))


@router.patch("/{return_id}/lines/{line_id}")
def update_line(
    return_id: int,
    line_id: int,
    payload: LinePatchIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    line = return_service.update_line(db, return_id, line_id, payload.model_dump(exclude_unset=True))
    return schemas.order_line_out(line, amounts=_amounts(db, user))


@router.delete("/{return_id}/lines/{line_id}")
def remove_line(
    return_id: int,
    line_id: int,
    _: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    return_service.remove_line(db, return_id, line_id)
    return {"message": "Line removed"}


@router.post("/{return_id}/files", status_code=201)
async def upload_file(
    return_id: int,
    file: UploadFile,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    content = await file.read()
    record = return_service.add_file(db, return_id, user, file.filename or "file", content)
    return schemas.document_file_out(record)


@router.get("/{return_id}/files/{file_id}/download")
def download_file(
    return_id: int,
    file_id: int,
    _: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    record = return_service.get_file(db, return_id, file_id)
    path = return_service.file_path_on_disk(record)
    if not path.exists():
        raise errors.NotFoundError("File is missing on disk", code="file_missing")
    # Снимок отдаётся картинкой, а не вложением: его смотрят на карточке.
    # Тип — из расширения, уже сверенного с содержимым при приёме.
    return FileResponse(
        path,
        media_type=record.mime,
        filename=record.original_name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{return_id}/files/{file_id}")
def delete_file(
    return_id: int,
    file_id: int,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    return_service.delete_file(db, return_id, file_id, user)
    return {"message": "File deleted"}


RETURN_PRINT_STRINGS = {
    "ru": {
        "title": "Акт возврата", "number": "№", "date": "Дата", "party": "Покупатель",
        "basis": "По заказу", "warehouse": "Склад", "item": "Наименование", "unit": "Ед.",
        "qty": "Кол-во", "price": "Цена", "sum": "Сумма", "total": "Итого по позициям",
        "refund": "Возвращено деньгами", "note": "Описание", "taxId": "Налоговый номер",
        "gave": "Сдал", "took": "Принял", "print": "Печать",
    },
    "en": {
        "title": "Return act", "number": "No.", "date": "Date", "party": "Customer",
        "basis": "Order", "warehouse": "Warehouse", "item": "Item", "unit": "Unit",
        "qty": "Qty", "price": "Price", "sum": "Amount", "total": "Items total",
        "refund": "Refunded", "note": "Description", "taxId": "Tax ID",
        "gave": "Returned by", "took": "Received by", "print": "Print",
    },
    "uk": {
        "title": "Акт повернення", "number": "№", "date": "Дата", "party": "Покупець",
        "basis": "За замовленням", "warehouse": "Склад", "item": "Найменування", "unit": "Од.",
        "qty": "К-сть", "price": "Ціна", "sum": "Сума", "total": "Разом за позиціями",
        "refund": "Повернуто грошима", "note": "Опис", "taxId": "Податковий номер",
        "gave": "Здав", "took": "Прийняв", "print": "Друк",
    },
}


@router.get("/{return_id}/print", response_class=HTMLResponse)
def print_return(
    return_id: int,
    locale: str | None = None,
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    """Акт возврата: перечень, итог по позициям, возвращённые деньги, подписи.

    Черновик не печатается — по той же причине, что у накладной: подписанная
    бумага не правится, а черновик правится целиком. Цены — только тому, кому
    они видны на экране; без права столбцы исчезают, а не пустеют.
    """
    vozvrat = return_service.get(db, return_id)
    if vozvrat.status != STATUS_CLOSED:
        raise errors.ValidationError("Only a posted return can be printed", code="return_not_posted")
    lang = locale if locale in DOCUMENT_LOCALES else vozvrat.locale
    t = RETURN_PRINT_STRINGS.get(lang, RETURN_PRINT_STRINGS["ru"])
    rows = return_service.lines(db, vozvrat.id)
    payload = return_service.payload_of(vozvrat)
    money = _amounts(db, user)
    currency = settings_service.get_all(db).get("currency", "USD")
    tovary = warehouse_repo.products_by_ids(
        db, {line.product_id for line in rows if line.product_id}, include_deleted=True
    )
    edinicy = {p.id: p.unit for p in tovary}
    nazvaniya = UNIT_NAMES.get(lang, UNIT_NAMES["ru"])
    warehouse = None
    if vozvrat.warehouse_id:
        warehouse = warehouse_service.get_warehouse(db, vozvrat.warehouse_id, include_deleted=True).name
    zakaz = return_service.zakaz(db, vozvrat)
    # Кто принял — из события проведения: имя снимком, как у накладной.
    prinyal = ""
    for sobytie in return_service.events(db, vozvrat.id):
        if sobytie.to_status == STATUS_CLOSED:
            prinyal = sobytie.author_name or ""
    klient = (payload.get("client") or {}).get("name") or _imya_klienta(db, vozvrat)
    html = public_routes.templates.get_template("return_print.html").render(
        doc=vozvrat,
        locale=lang,
        t=t,
        title=t["title"],
        party_label=t["party"],
        company=payload.get("company") or {},
        client=klient,
        deal=None,
        note=payload.get("note"),
        basis=zakaz.number if zakaz else None,
        warehouse=warehouse,
        created=vozvrat.updated_at.strftime("%d.%m.%Y %H:%M") if vozvrat.updated_at else "",
        released_by=klient or "",
        received_by=prinyal,
        money=money,
        lines=[
            {
                "name": line.name_snapshot,
                "unit": nazvaniya.get(edinicy.get(line.product_id, ""), ""),
                "quantity": warehouse_service.format_quantity(line.quantity_milli),
                "price": money_for_print(line.price_minor, currency),
                "sum": money_for_print(summa, currency),
            }
            for line, summa in zip(rows, document_service.line_totals(rows))
        ],
        total=money_for_print(document_service.total_minor(rows), currency),
        refund=money_for_print(vozvrat.refund_minor or 0, currency),
        barcode=codes.barcode_svg(vozvrat.number),
    )
    return HTMLResponse(html)


@router.post("/{return_id}/post")
def post_return(
    return_id: int,
    payload: PostIn,
    user: User = Depends(require_perm("orders", "issue")),
    db: Session = Depends(get_db),
):
    """Провести: товар на склад, деньги клиенту. Право то же, что у отгрузки."""
    vozvrat = return_service.provesti(db, return_id, user, warehouse_id=payload.warehouse_id)
    return _out(db, user, vozvrat)


@router.post("/{return_id}/cancel")
def cancel_return(
    return_id: int,
    payload: NoteIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    vozvrat = return_service.otmenit(db, return_id, user, payload.note)
    return _out(db, user, vozvrat)


@router.delete("/{return_id}")
def delete_return(
    return_id: int,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    """Удалить черновик, заведённый по ошибке. Проведённый — `422 document_in_use`."""
    return return_service.udalit(db, return_id, user)

