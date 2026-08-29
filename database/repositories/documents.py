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

from database.models import Document, DocumentEvent, DocumentLine
from database.query import as_int, contains, page_of


def get(db: Session, document_id: int) -> Document | None:
    return db.get(Document, document_id)


def po_osnovaniyu(db: Session, basis_id: int) -> list[Document]:
    """Бумаги, выписанные на основании этой: накладные заказа, сторно накладной.

    Отбор по `basis_id`, под который стоит индекс. Сортировка по номеру записи,
    а не по времени: у двух накладных, выписанных в одну секунду, порядок должен
    быть определённым — иначе список «по нему отгружено вот этим» на двух
    открытиях подряд показывает разное, и человек решает, что данные меняются.
    """
    return list(
        db.execute(
            select(Document).where(Document.basis_id == basis_id).order_by(Document.id)
        ).scalars()
    )


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
    basis_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Document], int]:
    stmt = select(Document)
    if kinds is not None:
        # Заказы и квитанции живут в одной таблице, но на разных экранах: список
        # бланков, куда затесались заказы, отвечает не на тот вопрос, с которым
        # туда пришли.
        stmt = stmt.where(Document.kind.in_(kinds))
    if q:
        needle = q.strip()
        # Ищем и по номеру, и по снимку: в мастерской спрашивают «где ноутбук
        # Петрова», а не номер бланка.
        stmt = stmt.where(contains(Document.number, needle) | contains(Document.payload, needle))
    if status:
        stmt = stmt.where(Document.status == status)
    if client_id:
        stmt = stmt.where(Document.client_id == client_id)
    if basis_id:
        # «Какие бумаги выписаны по этому заказу» — вопрос, который задают с
        # карточки заказа. Без отбора ответить на него было нечем: список
        # накладных умел сузиться по клиенту и заявке, а по основанию нет,
        # и найти накладную своего заказа человек мог только глазами по
        # всему списку.
        stmt = stmt.where(Document.basis_id == basis_id)
    if deal_id:
        # Нужен карточке сделки: выданный из неё бланк обязан быть в ней виден,
        # иначе он уходит в общий список и связь теряется.
        stmt = stmt.where(Document.deal_id == deal_id)

    stmt = stmt.order_by(Document.created_at.desc())
    return page_of(db, stmt, page=page, per_page=per_page)


# --- строки перечня ---
#
# Таблица общая для заказа и акта выполненных работ: две параллельные системы
# строк — это два места, где считаются деньги, и они разъедутся (см. докстроку
# модели `DocumentLine`).


def lines_of(db: Session, document_id: int) -> list[DocumentLine]:
    return list(
        db.scalars(
            select(DocumentLine)
            .where(DocumentLine.document_id == document_id)
            .order_by(DocumentLine.sort_order.asc(), DocumentLine.id.asc())
        )
    )


def get_line(db: Session, document_id: int, line_id: int) -> DocumentLine | None:
    """Строка вместе с проверкой принадлежности: номер в адресе — не пропуск к
    чужому бланку."""
    return db.scalars(
        select(DocumentLine).where(
            DocumentLine.id == line_id, DocumentLine.document_id == document_id
        )
    ).first()


def line_by_product(db: Session, document_id: int, product_id: int) -> DocumentLine | None:
    """Строка этого товара в этом бланке — для сборки сканером: второй писк по
    той же коробке обязан увеличить количество, а не завести вторую строку."""
    return db.scalars(
        select(DocumentLine).where(
            DocumentLine.document_id == document_id, DocumentLine.product_id == product_id
        )
    ).first()


def next_sort_order(db: Session, document_id: int) -> int:
    """Куда встанет следующая позиция. Порядок задаёт человек, и менять его
    сортировкой по имени значит переставлять строки под руками."""
    last = db.scalar(
        select(func.max(DocumentLine.sort_order)).where(
            DocumentLine.document_id == document_id
        )
    )
    return (last or 0) + 1


def lines_by_documents(db: Session, document_ids) -> dict[int, list[DocumentLine]]:
    """Строки сразу нескольких бланков — одним запросом на страницу списка."""
    document_ids = [i for i in set(document_ids) if i]
    if not document_ids:
        return {}
    grouped: dict[int, list[DocumentLine]] = {}
    rows = db.scalars(
        select(DocumentLine)
        .where(DocumentLine.document_id.in_(document_ids))
        .order_by(DocumentLine.sort_order.asc(), DocumentLine.id.asc())
    )
    for row in rows:
        grouped.setdefault(row.document_id, []).append(row)
    return grouped


def promised(db: Session, kind: str, statuses, product_ids=None) -> dict[int, int]:
    """Сколько товара обещано незакрытыми бланками этого вида: {product_id: тысячные}.

    Это резерв (для заказа покупателя) и «ожидается» (для заказа поставщику), и
    **считается это запросом, а не хранится числом**. Довод тот же, что у
    остатка склада: хранимое число рассинхронизируется в первый же откат
    транзакции, и узнать потом, какая из двух цифр верна, будет неоткуда. Отсюда
    же берётся и правило «резерв не переживает заказ»: отменили — обещание
    исчезло само, без единой строки кода на уборку.

    Один запрос на весь список товаров, как `stock_by_product`: запрос на строку
    превратил бы экран склада из 500 позиций в 500 обращений к базе.
    """
    stmt = (
        select(DocumentLine.product_id, func.coalesce(func.sum(DocumentLine.quantity_milli), 0))
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            DocumentLine.product_id.is_not(None),
        )
        .group_by(DocumentLine.product_id)
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        stmt = stmt.where(DocumentLine.product_id.in_(product_ids))
    # `as_int` — `SUM()` в MySQL возвращает `Decimal`, а количество в проекте
    # целое в тысячных; разбор — в докстроке `query.as_int`.
    return {product_id: as_int(total) for product_id, total in db.execute(stmt).all()}


def add_line(db: Session, line: DocumentLine) -> DocumentLine:
    db.add(line)
    db.flush()
    return line


def drop_line(db: Session, line: DocumentLine) -> None:
    db.delete(line)
    db.flush()
