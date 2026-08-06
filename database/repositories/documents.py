"""Запросы по бланкам.

Правил, стоящих за этими запросами, два, и оба про бумагу, а не про базу.

**Номер занимает тот, кто успел.** «Максимум по году плюс один» считается перед
каждой попыткой вставки, а не один раз: у стойки в окно между счётом и вставкой
попадают двое приёмщиков с одной накладной. Отсюда `max_number`, вызываемый в
цикле, и `number_exists` — предикат «место и правда занято» для
`core/uniqueness.py`.

**Статус меняет тот, кто прочитал прежний.** `take_status` пишет условием
`WHERE status = прочитанный`, и проигравший узнаёт об этом по нулю изменённых
строк. Без условия двое, нажавшие «готово» и «выдано» разом, оставили бы в
истории бланка два перехода из одного состояния — данные целы, а история
перестаёт отвечать на вопрос «когда клиент забрал», единственный, ради которого
её и ведут.
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database.models import Document, DocumentEvent
from database.query import contains, page_of


def get(db: Session, document_id: int) -> Document | None:
    return db.get(Document, document_id)


def get_by_number(db: Session, number: str) -> Document | None:
    """Бланк по номеру. Номер приходит со штрихкода или из адреса в QR."""
    return db.scalar(select(Document).where(Document.number == (number or "").strip()))


def number_exists(db: Session, number: str) -> bool:
    return db.scalar(select(Document.id).where(Document.number == number)) is not None


def max_number(db: Session, prefix: str) -> str | None:
    """Наибольший выданный номер года. Пусто — в этом году бланков ещё не было.

    Сравнение идёт по строке, и это верно ровно потому, что счётчик пишется с
    ведущими нулями (`2026-000123`): без них «2026-9» оказался бы больше
    «2026-10», и следующий номер повторил бы уже выданный.
    """
    return db.scalar(
        select(func.max(Document.number)).where(Document.number.startswith(prefix, autoescape=True))
    )


def add(db: Session, document: Document) -> Document:
    db.add(document)
    db.flush()
    return document


def take_status(db: Session, document: Document, *, expected: str, status: str) -> bool:
    """Сменить статус, пока он тот, что прочитали. False — кто-то успел раньше.

    После удачной записи объект обновляется из базы: писали запросом, мимо него,
    и в памяти он помнит прежний статус.
    """
    changed = db.execute(
        update(Document)
        .where(Document.id == document.id, Document.status == expected)
        .values(status=status)
    )
    if changed.rowcount == 0:
        return False
    db.refresh(document)
    return True


def add_event(db: Session, event: DocumentEvent) -> DocumentEvent:
    db.add(event)
    db.flush()
    return event


def events(db: Session, document_id: int) -> list[DocumentEvent]:
    """История бланка — по возрастанию: её читают сверху вниз, как рассказ."""
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
        needle = q.strip()
        # Ищем и по номеру, и по снимку: в мастерской спрашивают «где ноутбук
        # Петрова», а не номер бланка.
        stmt = stmt.where(contains(Document.number, needle) | contains(Document.payload, needle))
    if status:
        stmt = stmt.where(Document.status == status)
    if client_id:
        stmt = stmt.where(Document.client_id == client_id)
    if deal_id:
        # Нужен карточке сделки: выданный из неё бланк обязан быть в ней виден,
        # иначе он уходит в общий список и связь теряется.
        stmt = stmt.where(Document.deal_id == deal_id)

    stmt = stmt.order_by(Document.created_at.desc())
    return page_of(db, stmt, page=page, per_page=per_page)
