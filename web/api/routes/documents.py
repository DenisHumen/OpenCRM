"""Бланки: создание, печать, поиск сканом, смена состояния."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import act_service, codes, document_service, settings_service
from database.models import User
from database.models.document import DOCUMENT_LOCALES
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm
from web.public import routes as public_routes
from web.public.document_strings import strings_for

router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(require_module("documents"))],
)

PRINT_LABELS = {"ru": "Печать", "en": "Print", "uk": "Друк"}


class DocumentIn(BaseModel):
    client_id: int | None = None
    deal_id: int | None = None
    # От чьего имени выдаём бланк. Не прислали — берём фирму заявки, а если её
    # нет, фирму по умолчанию (core/services/company_service.for_document).
    company_id: int | None = None
    locale: str = "ru"
    # Если клиента в базе ещё нет — принимаем данные прямо в бланк: в мастерской
    # человек стоит у стойки, и заводить карточку до квитанции неудобно.
    client_name: str | None = None
    client_phone: str | None = None
    item: str
    serial: str | None = None
    condition: str | None = None
    accessories: str | None = None
    problem: str | None = None
    estimate: str | None = None
    terms: str | None = None


class StatusIn(BaseModel):
    status: str
    note: str = ""


class ActIn(BaseModel):
    """Заведение акта. Заявка обязательна: акт закрывает работу, а не бумагу."""

    deal_id: int
    client_id: int | None = None
    company_id: int | None = None
    locale: str = "ru"
    # Заголовок бумаги: «Акт выполненных работ», «Наряд-заказ», «Акт
    # сдачи-приёмки». Пусто — подставится общий.
    title: str | None = None
    # Гарантия и условия — печатаются под перечнем работ.
    terms: str | None = None
    # Куда акт переведёт заявку при проведении. Пусто — решим при проведении:
    # возьмётся следующий этап воронки.
    next_stage: str | None = None


class ActLineIn(BaseModel):
    # Пусто — выполненная работа без карточки («выезд мастера», «диагностика»).
    # Указан обычный товар — израсходованный материал, он и спишется.
    product_id: int | None = None
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class ActCompleteIn(BaseModel):
    # Склад выбирается явно: молчаливое списание с основного однажды снимет
    # деталь не оттуда, где её взяли.
    warehouse_id: int | None = None
    # Этап, названный в момент подписи. Пусто — берётся записанный на акте, а
    # если и его нет — следующий по воронке.
    stage: str | None = None
    # Явное согласие списать больше, чем лежит. По умолчанию нехватка
    # останавливает проведение: списывать нечего физически.
    confirm_negative: bool = False


class ActCancelIn(BaseModel):
    note: str = ""


@router.get("")
def list_documents(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_perm("documents", "view")),
    db: Session = Depends(get_db),
):
    items, total = document_service.search(
        db,
        q=search,
        status=status,
        client_id=client_id,
        deal_id=deal_id,
        page=page,
        per_page=per_page,
    )
    return schemas.paginated([schemas.document_out(d) for d in items], total, page, per_page)


@router.post("", status_code=201)
def create_document(
    payload: DocumentIn,
    user: User = Depends(require_perm("documents", "create")),
    db: Session = Depends(get_db),
):
    document = document_service.create(db, payload.model_dump(), user)
    return schemas.document_out(document)


@router.get("/by-number/{number}")
def find_by_number(
    number: str,
    _: User = Depends(require_perm("documents", "view")),
    db: Session = Depends(get_db),
):
    """Поиск сканом: сюда приходит то, что прочитал сканер штрихкода."""
    return schemas.document_out(document_service.by_number(db, number))


@router.get("/{document_id}")
def get_document(
    document_id: int,
    _: User = Depends(require_perm("documents", "view")),
    db: Session = Depends(get_db),
):
    document = document_service.get(db, document_id)
    events = document_service.events(db, document_id)
    authors = {
        u.id: u.name
        for u in users_repo.get_many(db, {e.author_id for e in events if e.author_id})
    }
    data = schemas.document_out(document)
    data["events"] = [
        {
            "id": e.id,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "note": e.note,
            "author_name": authors.get(e.author_id),
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
    return data


@router.post("/{document_id}/status")
def change_status(
    document_id: int,
    payload: StatusIn,
    user: User = Depends(require_perm("documents", "issue")),
    db: Session = Depends(get_db),
):
    document = document_service.set_status(db, document_id, payload.status, user, payload.note)
    return schemas.document_out(document)


@router.get("/{document_id}/print", response_class=HTMLResponse)
def print_document(
    document_id: int,
    locale: str | None = None,
    _: User = Depends(require_perm("documents", "view")),
    db: Session = Depends(get_db),
):
    """Готовый к печати бланк: две одинаковые половины с линией отреза.

    Язык можно переопределить параметром: бланк печатают под клиента, а не под
    сотрудника — приехал турист, печатаем по-английски, интерфейс мастера при
    этом остаётся прежним.
    """
    document = document_service.get(db, document_id)
    lang = locale if locale in DOCUMENT_LOCALES else document.locale

    from config.settings import get_settings

    base = (get_settings().base_url or "").rstrip("/")
    html = public_routes.templates.get_template("document_print.html").render(
        doc=document,
        payload=document_service.payload_of(document),
        t=strings_for(lang),
        print_label=PRINT_LABELS.get(lang, PRINT_LABELS["ru"]),
        created=document.created_at.strftime("%d.%m.%Y %H:%M"),
        barcode=codes.barcode_svg(document.number),
        # В QR кладём адрес страницы состояния, а не голый номер: телефон
        # клиента откроет её сразу, без приложения-сканера и без вопросов.
        qr=codes.qr_svg(f"{base}/d/{document.number}"),
    )
    return HTMLResponse(html)


# --- акт выполненных работ ----------------------------------------------------
#
# Акт живёт в роутере бланков, а не в своём: он **вид бланка**, и отдельный
# роутер потребовал бы второго переключателя блока, второй строки прав и второго
# адреса у той же самой бумаги. Отсюда же и права: заводит акт тот, кто заводит
# бланки (`create`), строки правит тот, кто правит (`edit`), а **проводит** —
# тот, кому доверено выпускать (`issue`). Последнее не формальность: проведение
# двигает склад и переводит заявку, и это не то же самое, что набрать перечень.
#
# Пути двухсегментные (`/acts/{id}`), поэтому с однословным `/{document_id}`
# они не пересекаются ни при каком порядке объявления.


@router.post("/acts", status_code=201)
def create_act(
    payload: ActIn,
    user: User = Depends(require_perm("documents", "create")),
    db: Session = Depends(get_db),
):
    act = act_service.create(db, payload.model_dump(), user)
    return schemas.act_out(act, [])


@router.get("/acts/{act_id}")
def get_act(
    act_id: int,
    _: User = Depends(require_perm("documents", "view")),
    db: Session = Depends(get_db),
):
    act = act_service.get(db, act_id)
    data = schemas.act_out(act, act_service.lines(db, act.id))
    # История акта — тем же составом, что у бланка: экран у них общий.
    events = act_service.events(db, act.id)
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
            "author_name": authors.get(e.author_id),
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
    return data


@router.post("/acts/{act_id}/lines", status_code=201)
def add_act_line(
    act_id: int,
    payload: ActLineIn,
    user: User = Depends(require_perm("documents", "edit")),
    db: Session = Depends(get_db),
):
    line = act_service.add_line(db, act_id, payload.model_dump(), user)
    return schemas.order_line_out(line)


@router.delete("/acts/{act_id}/lines/{line_id}")
def remove_act_line(
    act_id: int,
    line_id: int,
    _: User = Depends(require_perm("documents", "edit")),
    db: Session = Depends(get_db),
):
    act_service.remove_line(db, act_id, line_id)
    return {"message": "Line removed"}


@router.post("/acts/{act_id}/complete")
def complete_act(
    act_id: int,
    payload: ActCompleteIn,
    user: User = Depends(require_perm("documents", "issue")),
    db: Session = Depends(get_db),
):
    """Провести акт: списать израсходованное, зафиксировать работы, двинуть заявку.

    **Один маршрут на все три действия — это и есть задача.** Три отдельные
    ручки означали бы, что состояние «списано, но заявка не переведена»
    достижимо обычным нажатием, а не только сбоем. Здесь оно недостижимо вовсе:
    всё едет одной транзакцией запроса, и отказ любой её части отменяет
    остальные (разбор — в докстроке `act_service.complete`).
    """
    act = act_service.complete(
        db,
        act_id,
        user,
        warehouse_id=payload.warehouse_id,
        stage=payload.stage,
        confirm_negative=payload.confirm_negative,
    )
    return schemas.act_out(act, act_service.lines(db, act.id))


@router.post("/acts/{act_id}/cancel")
def cancel_act(
    act_id: int,
    payload: ActCancelIn,
    user: User = Depends(require_perm("documents", "edit")),
    db: Session = Depends(get_db),
):
    """Отменить непроведённый акт. Склада и воронки не касается — их не трогали."""
    act = act_service.cancel(db, act_id, user, payload.note)
    return schemas.act_out(act, act_service.lines(db, act.id))


#: Подписи печатной формы акта. Отдельно от квитанции и от заказа: у квитанции
#: описание одной вещи, у заказа «что и почём мне отдадут», у акта — «что
#: сделано и принято». Общий словарь пришлось бы делить на три половины прямо в
#: шаблоне, а перевод бумаги живёт своей жизнью от интерфейса (см.
#: `web/public/document_strings.py`).
ACT_PRINT_STRINGS = {
    "ru": {
        "title": "Акт выполненных работ", "number": "№", "date": "Дата",
        "client": "Заказчик", "deal": "По заявке", "item": "Наименование работ",
        "qty": "Кол-во", "price": "Цена", "sum": "Сумма", "total": "Итого",
        "terms": "Гарантия и условия",
        "statement": "Работы выполнены полностью и в срок. Заказчик претензий "
                     "по объёму, качеству и срокам не имеет.",
        "gave": "Исполнитель", "took": "Заказчик", "print": "Печать",
    },
    "en": {
        "title": "Certificate of completed works", "number": "No.", "date": "Date",
        "client": "Customer", "deal": "For request", "item": "Work performed",
        "qty": "Qty", "price": "Price", "sum": "Amount", "total": "Total",
        "terms": "Warranty and terms",
        "statement": "The works have been completed in full and on time. The "
                     "customer has no claims as to scope, quality or timing.",
        "gave": "Contractor", "took": "Customer", "print": "Print",
    },
    "uk": {
        "title": "Акт виконаних робіт", "number": "№", "date": "Дата",
        "client": "Замовник", "deal": "За заявкою", "item": "Найменування робіт",
        "qty": "К-сть", "price": "Ціна", "sum": "Сума", "total": "Разом",
        "terms": "Гарантія та умови",
        "statement": "Роботи виконано в повному обсязі та в строк. Замовник "
                     "претензій щодо обсягу, якості та строків не має.",
        "gave": "Виконавець", "took": "Замовник", "print": "Друк",
    },
}


@router.get("/acts/{act_id}/print", response_class=HTMLResponse)
def print_act(
    act_id: int,
    locale: str | None = None,
    _: User = Depends(require_perm("documents", "view")),
    db: Session = Depends(get_db),
):
    """Печатная форма акта: перечень работ, итог и две подписи.

    Одна половина, а не две с линией отреза, как у квитанции: акт подписывают
    обе стороны на одном листе, и второй экземпляр печатают повторно, если он
    нужен. Резать подписанную бумагу пополам нечего.

    Себестоимости на бумаге нет и быть не может: клиенту показывают, во сколько
    работа обошлась ЕМУ. Закупочная цена — наше внутреннее дело, и место ей на
    экране, под правом на раздел.

    Печатает браузер, как и всё остальное: сервер на VPS, принтер на столе.
    """
    act = act_service.get(db, act_id)
    rows = act_service.lines(db, act.id)
    lang = locale if locale in DOCUMENT_LOCALES else act.locale

    currency = settings_service.get_all(db).get("currency", "USD")
    payload = document_service.payload_of(act)
    html = public_routes.templates.get_template("act_print.html").render(
        doc=act,
        locale=lang,
        t=ACT_PRINT_STRINGS[lang],
        company=payload.get("company"),
        client=(payload.get("client") or {}).get("name"),
        deal=(payload.get("deal") or {}).get("title"),
        terms=(payload.get("fields") or {}).get("terms"),
        created=act.created_at.strftime("%d.%m.%Y %H:%M") if act.created_at else "",
        lines=[
            {
                "name": line.name_snapshot,
                "quantity": _quantity(line.quantity_milli),
                "price": _money(line.price_minor, currency),
                # Сумма строки — тем же счётом, что и итог, только по одной
                # строке. Своя арифметика здесь означала бы второе место, где
                # считаются деньги, и разошлись бы они первым же округлением.
                "sum": _money(document_service.total_minor([line]), currency),
            }
            for line in rows
        ],
        total=_money(document_service.total_minor(rows), currency),
        barcode=codes.barcode_svg(act.number),
    )
    return HTMLResponse(html)


def _money(minor: int | None, currency: str) -> str:
    if minor is None:
        return ""
    whole, cents = divmod(abs(int(minor)), 100)
    return f"{'-' if minor < 0 else ''}{whole}.{cents:02d} {currency}"


def _quantity(milli: int) -> str:
    from core.services import warehouse_service

    return warehouse_service.format_quantity(milli)
