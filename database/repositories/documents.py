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

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, aliased

from database.models import Document, DocumentEvent, DocumentLine
from database.models.document import KIND_SALES_ORDER
from database.models.warehouse import StockMove
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


def est_nezakrytaya(db: Session, deal_id: int, kind: str, statuses) -> bool:
    """Есть ли у заявки бумага этого вида в незакрытом состоянии.

    Отдельным запросом, а не отбором среди первой страницы `search`: страница
    обрезана (пятьдесят), и на заявке с длинной историей открытая бумага
    оказалась бы за её краем. Тогда «второй заказ по заявке» прошёл бы, и бронь
    удвоилась — ровно то, от чего проверка и стоит. Та же беда, что была у
    счётчика артикулов, где окно тоже принимали за «все».
    """
    return (
        db.scalar(
            select(Document.id)
            .where(
                Document.deal_id == deal_id,
                Document.kind == kind,
                Document.status.in_(tuple(statuses)),
            )
            .limit(1)
        )
        is not None
    )


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

    **Обещано — это заказано МИНУС уже отгруженное по этому же заказу.** Пока
    отгрузка случалась только вместе с закрытием, вычитать было нечего: заказ
    закрывался и выходил из отбора целиком. Но накладную можно выписать по
    заказу и провести, не закрывая его, и тогда товар уходил дважды —
    физически движением и на бумаге резервом, который оставался прежним.
    Ошибка тихая и в опасную сторону: «доступно» занижалось, и товар, лежащий
    на полке, считался обещанным. Продавец отказывал покупателю, глядя на
    число, которого нет.

    Отгруженное берётся из движений, а не из бумаг: бумага может быть
    сторнирована, и сторно — это тоже движение, обратное по знаку. Считая по
    движениям, возврат учитывается сам собой; считая по накладным, пришлось бы
    отдельно вычитать сторно и помнить об этом вечно.

    Два запроса на весь список товаров, а не два на строку: запрос на строку
    превратил бы экран склада из 500 позиций в тысячу обращений к базе.

    Номера заказов уезжают во второй запрос списком, и это не расточительство:
    обрезка по нулю делается на каждом заказе отдельно, значит разложить
    заказанное по заказам всё равно придётся в питоне. Список уже собран —
    подзапрос вместо него сэкономил бы ничего.
    """
    zakazano = (
        select(
            Document.id,
            DocumentLine.product_id,
            func.coalesce(func.sum(DocumentLine.quantity_milli), 0),
        )
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            DocumentLine.product_id.is_not(None),
        )
        .group_by(Document.id, DocumentLine.product_id)
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        zakazano = zakazano.where(DocumentLine.product_id.in_(product_ids))

    po_zakazam: dict[tuple[int, int], int] = {
        (zakaz, tovar): as_int(skolko) for zakaz, tovar, skolko in db.execute(zakazano).all()
    }
    if not po_zakazam:
        return {}

    otgruzheno = _otgruzheno_po_zakazam(
        db, kind, {zakaz for zakaz, _ in po_zakazam}, product_ids
    )

    itog: dict[int, int] = {}
    for (zakaz, tovar), skolko in po_zakazam.items():
        # Обрезаем по нулю на КАЖДОМ заказе отдельно, а не на итоге: отгрузив
        # сверх заказанного по одному заказу, иначе съели бы резерв соседнего —
        # а тот покупатель своего товара ждёт по-прежнему.
        ostalos = skolko - otgruzheno.get((zakaz, tovar), 0)
        if ostalos > 0:
            itog[tovar] = itog.get(tovar, 0) + ostalos
    return itog


def _otgruzheno_po_zakazam(
    db: Session, kind: str, zakazy: set[int], product_ids
) -> dict[tuple[int, int], int]:
    """Сколько уже прошло по складу под каждый заказ: {(заказ, товар): тысячные}.

    Движение указывает на бумагу, которая его сделала, а не на заказ. Путей до
    заказа три, и все три настоящие:

      - движение сделано САМИМ заказом. Так работали закрытия до переезда на
        накладные, и такие движения в базе останутся навсегда;
      - движение сделано накладной, выписанной ПО заказу;
      - движение сделано сторно такой накладной. Основание сторно — сама
        накладная, поэтому до заказа отсюда два шага.

    Знак приводим к «сколько ушло под заказ»: у продажи движения расходные и
    потому отрицательные, у закупки приходные и положительные. Отрицательный
    итог (вернули больше, чем отгрузили) обрезаем нулём — иначе возврат
    РАЗДУЛ бы резерв выше заказанного.
    """
    bumaga = aliased(Document)
    osnovanie = aliased(Document)
    zakaz_id = case(
        (bumaga.kind == kind, bumaga.id),
        (osnovanie.kind == kind, bumaga.basis_id),
        else_=osnovanie.basis_id,
    )
    zapros = (
        select(
            zakaz_id.label("zakaz"),
            StockMove.product_id,
            func.coalesce(func.sum(StockMove.quantity_milli), 0),
        )
        .join(bumaga, bumaga.id == StockMove.document_id)
        .join(osnovanie, osnovanie.id == bumaga.basis_id, isouter=True)
        .where(zakaz_id.in_(tuple(zakazy)))
        .group_by(zakaz_id, StockMove.product_id)
    )
    if product_ids:
        zapros = zapros.where(StockMove.product_id.in_(product_ids))

    znak = -1 if kind == KIND_SALES_ORDER else 1
    return {
        (zakaz, tovar): max(0, znak * as_int(skolko))
        for zakaz, tovar, skolko in db.execute(zapros).all()
    }


def add_line(db: Session, line: DocumentLine) -> DocumentLine:
    db.add(line)
    db.flush()
    return line


def drop_line(db: Session, line: DocumentLine) -> None:
    db.delete(line)
    db.flush()


def zakazano_po_zayavkam(db: Session, kind: str, statuses, product_ids=None):
    """Сколько товара заявки уже стоит в её открытых заказах: {(заявка, товар): тысячные}.

    Это НЕ резерв, а «сколько нужды заявки заказ ещё держит на себе» — чтобы
    заявка не забронировала и не списала то же самое второй раз
    (`docs/19-sborka-zakaza.md` §Р3).

    Отгруженное вычитается ОБЯЗАТЕЛЬНО, и вот почему. Накладная по заказу
    наследует `deal_id` заявки и пишет его в движения, то есть отгруженное уже
    сидит в `spisano_po_zayavkam`. Не вычти его здесь — одно и то же вычтется
    дважды, и заявка на пятнадцать штук с заказом на десять после отгрузки
    десяти покажет бронь ноль вместо пяти: товар, который клиент ещё ждёт,
    свободно уйдёт другому.
    """
    zapros = (
        select(
            Document.deal_id,
            Document.id,
            DocumentLine.product_id,
            func.coalesce(func.sum(DocumentLine.quantity_milli), 0),
        )
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            Document.deal_id.is_not(None),
            DocumentLine.product_id.is_not(None),
        )
        .group_by(Document.deal_id, Document.id, DocumentLine.product_id)
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        zapros = zapros.where(DocumentLine.product_id.in_(product_ids))
    po_zakazam = {
        (zayavka, zakaz, tovar): as_int(skolko)
        for zayavka, zakaz, tovar, skolko in db.execute(zapros).all()
    }
    if not po_zakazam:
        return {}

    otgruzheno = _otgruzheno_po_zakazam(
        db, kind, {zakaz for _, zakaz, _ in po_zakazam}, product_ids
    )
    itog: dict[tuple[int, int], int] = {}
    for (zayavka, zakaz, tovar), skolko in po_zakazam.items():
        # Обрезаем на КАЖДОМ заказе отдельно — тот же довод, что у `promised`:
        # отгрузив сверх заказанного по одному, иначе съели бы обещание соседнего.
        ostalos = skolko - otgruzheno.get((zakaz, tovar), 0)
        if ostalos > 0:
            itog[(zayavka, tovar)] = itog.get((zayavka, tovar), 0) + ostalos
    return itog


def otkrytye_s_tovarom(db: Session, kind: str, statuses, product_id: int):
    """Открытые бланки этого вида, где стоит товар: [(бланк, тысячные)].

    Для ответа «кто держит товар»: заказ показывается номером, а не номером
    записи, — по рации называют именно его.
    """
    ryady = db.execute(
        select(Document, func.coalesce(func.sum(DocumentLine.quantity_milli), 0))
        .join(DocumentLine, DocumentLine.document_id == Document.id)
        .where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            DocumentLine.product_id == product_id,
        )
        .group_by(Document.id)
        .order_by(Document.id)
    ).all()
    # Отгруженное вычитается: иначе карточка товара показала бы «заказ держит
    # десять» рядом с «в брони шесть» — два числа об одном и том же на одном
    # экране отменяют доверие к обоим.
    otgruzheno = _otgruzheno_po_zakazam(
        db, kind, {bumaga.id for bumaga, _ in ryady}, [product_id]
    )
    return [
        (bumaga, max(0, as_int(skolko) - otgruzheno.get((bumaga.id, product_id), 0)))
        for bumaga, skolko in ryady
    ]
