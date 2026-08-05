"""Бланки: создание, печать, поиск сканом, смена состояния."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import codes, document_service
from database.models import User
from database.models.document import DOCUMENT_LOCALES
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_staff
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


@router.get("")
def list_documents(
    search: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
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
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    document = document_service.create(db, payload.model_dump(), user)
    return schemas.document_out(document)


@router.get("/by-number/{number}")
def find_by_number(
    number: str,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Поиск сканом: сюда приходит то, что прочитал сканер штрихкода."""
    return schemas.document_out(document_service.by_number(db, number))


@router.get("/{document_id}")
def get_document(
    document_id: int,
    _: User = Depends(require_staff),
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
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    document = document_service.set_status(db, document_id, payload.status, user, payload.note)
    return schemas.document_out(document)


@router.get("/{document_id}/print", response_class=HTMLResponse)
def print_document(
    document_id: int,
    locale: str | None = None,
    _: User = Depends(require_staff),
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
