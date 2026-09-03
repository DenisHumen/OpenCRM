"""Накладные: набор позиций, проведение, подтверждение приёмки, сторнирование.

Роутер закрыт `require_module("waybills")` целиком, а не по маршруту:
пропущенный маршрут остался бы открытым, и выключенный блок продолжал бы
отвечать тому, кто помнит адрес.

Накладная — вид бланка, поэтому номер, статусы и поиск сканом живут в
`documents`; здесь только то, чего у квитанции нет. Печать — здесь: у квитанции
и у накладной общего в ней ровно номер, а общая ручка бланка печатала накладную
квитанцией приёма, без единой позиции на листе.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    codes,
    document_service,
    permissions_service,
    settings_service,
    warehouse_service,
    waybill_service,
)
from core.services.barcode_service import UNIT_NAMES
from core.utils import money_for_print
from database.models import User
from database.models.document import (
    DOCUMENT_LOCALES,
    KIND_WAYBILL_OUT,
    STATUS_CLOSED,
    STATUS_ISSUED,
    WAYBILL_KINDS,
)
from database.repositories import documents as documents_repo
from database.repositories import warehouse as warehouse_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm
from web.public import routes as public_routes

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
    basis_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("waybills", "view")),
    db: Session = Depends(get_db),
):
    kinds = (kind,) if kind in WAYBILL_KINDS else WAYBILL_KINDS
    items, total = documents_repo.search(
        db, q=search, status=status, client_id=client_id, deal_id=deal_id,
        basis_id=basis_id, kinds=kinds, page=page, per_page=per_page,
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


#: Словарь бумаги, отдельный от интерфейса и отдельный от акта.
#:
#: Общий словарь с актом пришлось бы делить прямо в шаблоне: акт отвечает «что
#: сделано и принято», накладная — «что передали физически», и совпадают у них
#: только «№» и «Итого». Перевод бумаги к тому же живёт своей жизнью от
#: интерфейса — её печатают под ПОЛУЧАТЕЛЯ, а не под сотрудника.
WAYBILL_PRINT_STRINGS = {
    "ru": {
        "out": "Расходная накладная", "in": "Приходная накладная",
        "number": "№", "date": "Дата", "party": "Получатель", "partyIn": "Поставщик",
        "basis": "Основание", "deal": "Заявка", "warehouse": "Склад",
        "item": "Наименование", "unit": "Ед.", "qty": "Кол-во", "price": "Цена",
        "sum": "Сумма", "total": "Итого", "note": "Примечание", "taxId": "Налоговый номер",
        "gave": "Отпустил", "took": "Получил", "print": "Печать",
    },
    "en": {
        "out": "Delivery note", "in": "Goods receipt note",
        "number": "No.", "date": "Date", "party": "Consignee", "partyIn": "Supplier",
        "basis": "Reference", "deal": "Request", "warehouse": "Warehouse",
        "item": "Item", "unit": "Unit", "qty": "Qty", "price": "Price",
        "sum": "Amount", "total": "Total", "note": "Note", "taxId": "Tax number",
        "gave": "Released by", "took": "Received by", "print": "Print",
    },
    "uk": {
        "out": "Видаткова накладна", "in": "Прибуткова накладна",
        "number": "№", "date": "Дата", "party": "Одержувач", "partyIn": "Постачальник",
        "basis": "Підстава", "deal": "Заявка", "warehouse": "Склад",
        "item": "Найменування", "unit": "Од.", "qty": "К-сть", "price": "Ціна",
        "sum": "Сума", "total": "Разом", "note": "Примітка", "taxId": "Податковий номер",
        "gave": "Відпустив", "took": "Одержав", "print": "Друк",
    },
}


@router.get("/{waybill_id}/print", response_class=HTMLResponse)
def print_waybill(
    waybill_id: int,
    locale: str | None = None,
    user: User = Depends(require_perm("waybills", "view")),
    db: Session = Depends(get_db),
):
    """Печатная форма накладной: перечень, итог и две подписи.

    **Черновик не печатается, и это правило, а не придирка.** Черновик правится
    целиком, а подписанная бумага — нет; напечатанный черновик даёт лист с
    номером и подписями получателя под перечнем, который назавтра станет другим.
    Ровно от этого модуль и построен вокруг деления «до проведения — после».

    Цены печатаются только тому, кому они видны на экране (`view_amounts`). Без
    права столбцы не пустеют, а исчезают: пустая колонка «Сумма» под подписью
    читается как «бесплатно», а не как «вам не показано».
    """
    waybill = waybill_service.get(db, waybill_id)
    if waybill.kind not in WAYBILL_KINDS:
        raise errors.NotFoundError("Waybill not found", code="waybill_not_found")
    if waybill.status not in (STATUS_ISSUED, STATUS_CLOSED):
        raise errors.ValidationError(
            "Only a posted waybill can be printed", code="waybill_not_posted"
        )

    lang = locale if locale in DOCUMENT_LOCALES else waybill.locale
    t = WAYBILL_PRINT_STRINGS.get(lang, WAYBILL_PRINT_STRINGS["ru"])
    ishodyashchaya = waybill.kind == KIND_WAYBILL_OUT
    rows = waybill_service.lines(db, waybill.id)
    payload = waybill_service.payload_of(waybill)
    money = _amounts(db, user)
    currency = settings_service.get_all(db).get("currency", "USD")

    # Единицы — из товаров, одним запросом. По строке за штуку это дало бы
    # полсотни обращений на печать пятидесятипозиционной накладной.
    # `include_deleted`: бумага печатается такой, какой её выписали. Без этого
    # удаление товара опустошало столбец «Ед.» РАЗОМ у всех прошлых накладных —
    # «3» без единицы, а штуки это, килограммы или метры, сказать нечем.
    tovary = warehouse_repo.products_by_ids(
        db,
        {line.product_id for line in rows if line.product_id},
        include_deleted=True,
    )
    edinicy = {p.id: p.unit for p in tovary}
    nazvaniya = UNIT_NAMES.get(lang, UNIT_NAMES["ru"])

    warehouse = None
    if waybill.warehouse_id:
        warehouse = warehouse_service.get_warehouse(
            db, waybill.warehouse_id, include_deleted=True
        ).name
    basis = None
    if waybill.basis_id:
        basis = document_service.get(db, waybill.basis_id).number
    kto_provyol = waybill_service.kto_otpustil(db, waybill)

    html = public_routes.templates.get_template("waybill_print.html").render(
        doc=waybill,
        locale=lang,
        t=t,
        # Заголовок и вторая сторона зависят от направления: у приходной
        # накладной «Получатель» — это мы, и печатать там имя клиента значило
        # бы напечатать неправду.
        title=t["out"] if ishodyashchaya else t["in"],
        party_label=t["party"] if ishodyashchaya else t["partyIn"],
        company=payload.get("company") or {},
        client=(payload.get("client") or {}).get("name"),
        deal=(payload.get("deal") or {}).get("title"),
        note=payload.get("note"),
        basis=basis,
        warehouse=warehouse,
        created=waybill.created_at.strftime("%d.%m.%Y %H:%M") if waybill.created_at else "",
        # У ПРИХОДНОЙ наш сотрудник — принимающая сторона: товар отпустил
        # поставщик. Имя под «Отпустил» было бы неправдой, а пустой осталась
        # бы ровно та строка, под которой стоит его настоящая подпись.
        released_by=kto_provyol if ishodyashchaya else "",
        received_by="" if ishodyashchaya else kto_provyol,
        money=money,
        lines=[
            {
                "name": line.name_snapshot,
                "unit": nazvaniya.get(edinicy.get(line.product_id, ""), ""),
                "quantity": _quantity(line.quantity_milli),
                "price": money_for_print(line.price_minor, currency),
                # Сумма строки — из общего счёта нарастающим итогом, чтобы
                # колонка СКЛАДЫВАЛАСЬ в «Итого» под ней. Округление на каждой
                # строке против округления один раз на итоге даёт лист, где
                # 61.73 + 61.73 стоит под «Итого 123.45». Разбор — в
                # `document_service.line_totals`.
                "sum": money_for_print(summa, currency),
            }
            for line, summa in zip(rows, document_service.line_totals(rows))
        ],
        total=money_for_print(waybill_service.total_minor(rows), currency),
        barcode=codes.barcode_svg(waybill.number),
    )
    return HTMLResponse(html)


def _quantity(milli: int) -> str:
    return warehouse_service.format_quantity(milli)
