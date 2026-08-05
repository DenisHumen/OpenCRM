"""Бланки: приём вещи в работу, печать в двух экземплярах, поиск сканом."""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import settings_service
from core.utils import now_utc
from database.models import Client, Deal, Document, DocumentEvent, User
from database.models.document import (
    DOCUMENT_KINDS,
    DOCUMENT_LOCALES,
    DOCUMENT_STATUSES,
    KIND_INTAKE,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_ISSUED,
)

# Предел на поле бланка — не придирка к многословию, а условие того, что обе
# половины помещаются на один A4. Замерено на живой странице: при 400 символах в
# каждом поле лист вырастает до 474 мм против 277 доступных, линия отреза уезжает
# на второй лист и резать становится нечего. При 160 худший случай укладывается в
# 269 мм. Подробности не теряются — им место в сделке, а не на квитанции.
#
# Менять это число можно только вместе с замером: вёрстка бланка (парные поля в
# одной строке, 8pt) подогнана ровно под него.
MAX_TEXT = 160
MAX_NOTE = 200

# Поля снимка для приёмного бланка. Заведомо простые и отраслево-нейтральные:
# «предмет» — это и ноутбук, и велосипед, и швейная машинка. Заводить отдельный
# набор полей под каждую отрасль значит превратить бланк в конструктор.
INTAKE_FIELDS = (
    "item",          # что приняли: «Ноутбук Asus X515»
    "serial",        # серийный номер или примета
    "condition",     # внешнее состояние на момент приёма
    "accessories",   # что отдали вместе: зарядка, чехол, сумка
    "problem",       # со слов клиента
    "estimate",      # предварительная цена
    "terms",         # сроки и условия
)


def _payload(data: dict, client: Client | None, deal: Deal | None, site: dict) -> dict:
    """Снимок того, что напечатано.

    Ссылками не обойтись: у человека на руках бумага, и она обязана совпадать с
    записью в базе, даже если клиента потом переименовали, телефон поправили, а
    сделку удалили. Иначе спор «что вы у меня приняли» решать нечем.
    """
    return {
        "company": {
            "name": site.get("brand_name") or "",
            "phone": site.get("contact_phone") or "",
            "email": site.get("contact_email") or "",
        },
        "client": {
            "name": (client.name if client else data.get("client_name") or ""),
            "phone": (client.phone if client else data.get("client_phone") or ""),
            "email": (client.email if client else data.get("client_email") or ""),
        },
        "deal": {"id": deal.id if deal else None, "title": deal.title if deal else ""},
        "fields": {
            key: str(data.get(key) or "").strip()[:MAX_TEXT] for key in INTAKE_FIELDS
        },
    }


def next_number(db: Session) -> str:
    """Номер вида «2026-000123», сквозной внутри года.

    Считаем максимум по году, а не общий счётчик: номер должен читаться вслух по
    телефону и не превращаться в шестизначную абстракцию на второй год работы.
    """
    year = now_utc().year
    prefix = f"{year}-"
    last = db.scalar(
        select(func.max(Document.number)).where(Document.number.like(f"{prefix}%"))
    )
    counter = int(last.split("-")[1]) + 1 if last else 1
    return f"{prefix}{counter:06d}"


def create(db: Session, data: dict, author: User) -> Document:
    kind = data.get("kind") or KIND_INTAKE
    if kind not in DOCUMENT_KINDS:
        raise errors.ValidationError(f"Unknown document kind: {kind}", code="unknown_kind")

    locale = data.get("locale") or "ru"
    if locale not in DOCUMENT_LOCALES:
        raise errors.ValidationError(f"Unknown locale: {locale}", code="unknown_locale")

    client = db.get(Client, int(data["client_id"])) if data.get("client_id") else None
    deal = db.get(Deal, int(data["deal_id"])) if data.get("deal_id") else None
    if deal is not None and client is None:
        client = db.get(Client, deal.client_id)
    if client is None and not (data.get("client_name") or "").strip():
        raise errors.ValidationError(
            "Document needs a client or at least a name", code="client_required"
        )
    if not (data.get("item") or "").strip():
        raise errors.ValidationError("Item is required", code="item_required")

    document = Document(
        number=next_number(db),
        kind=kind,
        locale=locale,
        status=STATUS_ISSUED,
        client_id=client.id if client else None,
        deal_id=deal.id if deal else None,
        payload=json.dumps(
            _payload(data, client, deal, settings_service.get_all(db)), ensure_ascii=False
        ),
        created_by=author.id,
    )
    db.add(document)
    db.flush()
    db.add(
        DocumentEvent(
            document_id=document.id, from_status="", to_status=STATUS_ISSUED,
            author_id=author.id,
        )
    )
    db.flush()
    return document


def get(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise errors.NotFoundError("Document not found", code="document_not_found")
    return document


def by_number(db: Session, number: str) -> Document:
    """Поиск сканом. Номер приходит со штрихкода или из адреса в QR."""
    document = db.scalar(select(Document).where(Document.number == (number or "").strip()))
    if document is None:
        raise errors.NotFoundError("Document not found", code="document_not_found")
    return document


def set_status(db: Session, document_id: int, status: str, author: User, note: str = "") -> Document:
    if status not in DOCUMENT_STATUSES:
        raise errors.ValidationError(f"Unknown status: {status}", code="unknown_status")

    document = get(db, document_id)
    if document.status in (STATUS_CLOSED, STATUS_CANCELLED) and status != document.status:
        # Закрытый бланк — уже отданная вещь. Открывать его заново нельзя:
        # иначе история перестаёт отвечать на вопрос «когда клиент забрал».
        raise errors.ValidationError(
            "This document is already finished", code="document_finished"
        )
    if status == document.status:
        return document

    previous = document.status
    document.status = status
    db.flush()
    db.add(
        DocumentEvent(
            document_id=document.id,
            from_status=previous,
            to_status=status,
            note=(note or "").strip()[:MAX_NOTE],
            author_id=author.id,
        )
    )
    db.flush()
    return document


def events(db: Session, document_id: int) -> list[DocumentEvent]:
    return list(
        db.scalars(
            select(DocumentEvent)
            .where(DocumentEvent.document_id == document_id)
            .order_by(DocumentEvent.created_at.asc(), DocumentEvent.id.asc())
        )
    )


def search(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Document], int]:
    stmt = select(Document)
    if q:
        like = f"%{q.strip()}%"
        # Ищем и по номеру, и по снимку: в мастерской спрашивают «где ноутбук
        # Петрова», а не номер бланка.
        stmt = stmt.where(Document.number.ilike(like) | Document.payload.ilike(like))
    if status:
        stmt = stmt.where(Document.status == status)
    if client_id:
        stmt = stmt.where(Document.client_id == client_id)
    if deal_id:
        # Нужен карточке сделки: выданный из неё бланк обязан быть в ней виден,
        # иначе он уходит в общий список и связь теряется.
        stmt = stmt.where(Document.deal_id == deal_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(Document.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    return list(db.scalars(stmt)), total


def payload_of(document: Document) -> dict:
    try:
        return json.loads(document.payload or "{}")
    except ValueError:
        # Битый снимок не должен ронять печать всего бланка: показываем что есть.
        return {}
