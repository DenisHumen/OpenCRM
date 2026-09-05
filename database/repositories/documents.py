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

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from core.utils import now_utc
from database.models import Document, DocumentEvent, DocumentFile, DocumentLine
from database.models.document import (
    KIND_RETURN,
    KIND_SALES_ORDER,
    KIND_WAYBILL_IN,
    KIND_WAYBILL_OUT,
    STATUS_CLOSED,
    STATUS_ISSUED,
)
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


def drop(db: Session, document: Document) -> None:
    """Стереть бумагу; строки и переходы уходят каскадом по внешнему ключу."""
    db.delete(document)
    db.flush()


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


#: Порядок списка бумаг: КЛЮЧ → чем сортировать.
#:
#: Закрытый перечень, а не имя колонки из запроса. Имя колонки снаружи — это
#: `ORDER BY` по чему угодно, включая то, чего нет в индексах, и повод для
#: запроса, читающего таблицу целиком по чужой воле.
#:
#: **Разрешитель ничьей выписан здесь, а не оставлен `page_of`.** Тот дописывает
#: первичный ключ ВСЕГДА по убыванию (`database/query._with_tiebreak`), и для
#: «свежие сверху» это правильно, а для «старые сверху» — нет: `created_at`
#: хранится с точностью до секунды, пять бумаг, заведённых подряд, получают одну
#: отметку, и порядок между ними решает дописанный ключ. Убывающий давал
#: «старые сверху», в точности совпадающие со «свежими сверху». Поймано
#: собственной проверкой `test_starye_sverkhu_eto_obratnyy_poryadok`.
#:
#: Чего здесь НЕТ и почему: «по клиенту» и «по сумме». Имя клиента лежит в
#: снимке (`payload`, JSON), а суммы у бумаги нет колонкой вовсе — она
#: складывается из строк перечня. И то, и другое означало бы соединение или
#: разбор JSON в `ORDER BY`, то есть новую цену у списка; заводить её надо с
#: замером, а не заодно.
PORYADKI: dict[str, tuple] = {
    "new": (Document.created_at.desc(), Document.id.desc()),
    "old": (Document.created_at.asc(), Document.id.asc()),
    "number": (Document.number.desc(), Document.id.desc()),
    "status": (Document.status.asc(), Document.created_at.desc(), Document.id.desc()),
}
PORYADOK_PO_UMOLCHANIYU = "new"


def _usloviya(
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    basis_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
) -> list:
    """Условия отбора бумаг — одним списком на два запроса.

    Список и счёт по видам обязаны отбирать ОДИНАКОВО, иначе счётчик над
    свёрнутой категорией назовёт не то число, что в ней лежит. Два набора
    условий в двух местах расходятся на первой же новой строке отбора — здесь он
    один.
    """
    usloviya = []
    if kinds is not None:
        # Заказы и квитанции живут в одной таблице, но на разных экранах: список
        # бланков, куда затесались заказы, отвечает не на тот вопрос, с которым
        # туда пришли.
        usloviya.append(Document.kind.in_(kinds))
    if q:
        needle = q.strip()
        # Ищем и по номеру, и по снимку: в мастерской спрашивают «где ноутбук
        # Петрова», а не номер бланка.
        usloviya.append(contains(Document.number, needle) | contains(Document.payload, needle))
    if status:
        usloviya.append(Document.status == status)
    if client_id:
        usloviya.append(Document.client_id == client_id)
    if basis_id:
        # «Какие бумаги выписаны по этому заказу» — вопрос, который задают с
        # карточки заказа. Без отбора ответить на него было нечем: список
        # накладных умел сузиться по клиенту и заявке, а по основанию нет,
        # и найти накладную своего заказа человек мог только глазами по
        # всему списку.
        usloviya.append(Document.basis_id == basis_id)
    if deal_id:
        # Нужен карточке сделки: выданный из неё бланк обязан быть в ней виден,
        # иначе он уходит в общий список и связь теряется.
        usloviya.append(Document.deal_id == deal_id)
    return usloviya


def search(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    basis_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
    sort: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Document], int]:
    stmt = select(Document).where(*_usloviya(q, status, client_id, deal_id, basis_id, kinds))
    stmt = stmt.order_by(*PORYADKI.get(sort or "", PORYADKI[PORYADOK_PO_UMOLCHANIYU]))
    return page_of(db, stmt, page=page, per_page=per_page)


def schyot_po_vidam(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    basis_id: int | None = None,
    sredi: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Сколько бумаг каждого вида при ЭТОМ отборе: {вид: сколько}.

    **Отбор по виду сюда не передаётся нарочно.** Счёт нужен, чтобы над
    свёрнутой категорией стояло её число, и чтобы можно было развернуть
    соседнюю; посчитай мы с уже применённым видом — в ответе осталась бы одна
    строка, и остальные категории исчезли бы с экрана вместе со своими числами.
    `sredi` сужает не отбор пользователя, а область экрана: у списка заказов
    видов всего два, и квитанции ему считать незачем.

    Одним запросом на весь список, а не запросом на вид: видов шесть, и шесть
    отдельных `COUNT` — это шесть проходов там, где хватает одного.
    """
    usloviya = _usloviya(q, status, client_id, deal_id, basis_id, sredi)
    ryady = db.execute(
        select(Document.kind, func.count())
        .where(*usloviya)
        .group_by(Document.kind)
    ).all()
    return {vid: int(skolko) for vid, skolko in ryady}


def schyot_po_statusam(
    db: Session,
    q: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    basis_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Сколько бумаг в каждом состоянии при этом отборе: {статус: сколько}.

    Тот же приём, что у счёта по видам, и по тому же доводу: отбор по СТАТУСУ
    сюда не передаётся — иначе свёрнутые соседние состояния исчезли бы с
    экрана. Нужен списку заказов, где категория — это состояние: вид там уже
    выбран чипами.
    """
    ryady = db.execute(
        select(Document.status, func.count())
        .where(*_usloviya(q, None, client_id, deal_id, basis_id, kinds))
        .group_by(Document.status)
    ).all()
    return {status: int(skolko) for status, skolko in ryady}


def nezakrytaya(db: Session, deal_id: int, kind: str, statuses) -> Document | None:
    """Незакрытая бумага этого вида у заявки, если есть. Первая по номеру записи."""
    return db.scalar(
        select(Document)
        .where(
            Document.deal_id == deal_id,
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
        )
        .order_by(Document.id)
        .limit(1)
    )


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
            # Бронь с сайта истекает ЛЕНИВО, этим условием: истёкшая перестаёт
            # занимать товар ровно в свою секунду, без таймера, которому можно
            # не сработать. Заказ при этом остаётся открытым — очередь на разбор.
            _bron_zhiva(),
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


# --- заказы с сайта ------------------------------------------------------------


def _bron_zhiva():
    """Условие «бронь ещё держит»: без срока или срок впереди."""
    return or_(Document.reserved_until.is_(None), Document.reserved_until > now_utc())


def get_by_site_ref(db: Session, site_ref: str) -> Document | None:
    return db.scalar(select(Document).where(Document.site_ref == site_ref))


def min_reserved_until(db: Session, kind: str, statuses):
    """Ближайший срок брони среди открытых заказов — когда картинка сайта устареет сама."""
    return db.scalar(
        select(func.min(Document.reserved_until)).where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            Document.reserved_until > now_utc(),
        )
    )


def tovary_zakazov_s(db: Session, kind: str, statuses, since) -> set[int]:
    """Товары открытых заказов, менявшихся после `since` — для ленты изменений."""
    stmt = (
        select(DocumentLine.product_id)
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            Document.updated_at > since,
            DocumentLine.product_id.is_not(None),
        )
        .distinct()
    )
    return {as_int(product_id) for product_id in db.scalars(stmt)}


def bron_istekla(db: Session, kind: str, statuses, page: int = 1, per_page: int = 50):
    """Заказы, чья бронь истекла, а сами они открыты: очередь на разбор, не мусор."""
    stmt = (
        select(Document)
        .where(
            Document.kind == kind,
            Document.status.in_(tuple(statuses)),
            Document.reserved_until.is_not(None),
            Document.reserved_until <= now_utc(),
        )
        .order_by(Document.reserved_until.asc())
    )
    return page_of(db, stmt, page=page, per_page=per_page)


# --- возвраты ---


def zapert_bumagu(db: Session, document_id: int) -> None:
    """Занять бумагу до конца транзакции. Нужен возврату: «сколько ещё можно
    вернуть по заказу» считается запросом, и двое, проводящие два возврата по
    одному заказу разом, вернули бы больше, чем отгружено. Замок — на строку
    ЗАКАЗА: она одна и та же для обоих (разбор — `deals.zapert_zayavku`)."""
    db.execute(select(Document.id).where(Document.id == document_id).with_for_update()).all()


def vozvraty_po_zakazu(db: Session, order_id: int) -> list[Document]:
    return list(
        db.scalars(
            select(Document)
            .where(Document.basis_id == order_id, Document.kind == KIND_RETURN)
            .order_by(Document.created_at.asc(), Document.id.asc())
        ).all()
    )


def vozvrashcheno_po_zakazu(db: Session, order_id: int, krome_id: int | None = None) -> dict[int, int]:
    """Сколько каждого товара уже вернулось по заказу проведёнными возвратами:
    {товар: тысячные}. `krome_id` — свой черновик в счёт не идёт."""
    stmt = (
        select(DocumentLine.product_id, func.coalesce(func.sum(DocumentLine.quantity_milli), 0))
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.basis_id == order_id,
            Document.kind == KIND_RETURN,
            Document.status == STATUS_CLOSED,
            DocumentLine.product_id.is_not(None),
        )
        .group_by(DocumentLine.product_id)
    )
    if krome_id is not None:
        stmt = stmt.where(Document.id != krome_id)
    return {int(product_id): as_int(summa) for product_id, summa in db.execute(stmt).all()}


# --- вложения бумаги ---


def files_of(db: Session, document_id: int) -> list[DocumentFile]:
    return list(
        db.scalars(
            select(DocumentFile)
            .where(DocumentFile.document_id == document_id)
            .order_by(DocumentFile.created_at.desc(), DocumentFile.id.desc())
        ).all()
    )


def get_file(db: Session, document_id: int, file_id: int) -> DocumentFile | None:
    return db.scalar(
        select(DocumentFile).where(
            DocumentFile.id == file_id, DocumentFile.document_id == document_id
        )
    )


def add_file(db: Session, file: DocumentFile) -> DocumentFile:
    db.add(file)
    db.flush()
    return file


def drop_file(db: Session, file: DocumentFile) -> None:
    db.delete(file)
    db.flush()


def po_ids(db: Session, ids) -> list[Document]:
    """Бумаги по номерам записей — одним запросом на страницу списка."""
    ids = {int(i) for i in ids if i}
    if not ids:
        return []
    return list(db.scalars(select(Document).where(Document.id.in_(ids)).order_by(Document.id.asc())).all())


def stornirovano_po_zakazu(db: Session, order_id: int) -> dict[int, int]:
    """Сколько товара вернулось сторно накладных заказа: {товар: тысячные}.

    Приходная по основанию-накладной заказа — сторно; проведена (`issued`) или
    принята (`closed`). Без этого возврат по заказу, чью накладную уже
    сторнировали, вернул бы товар второй раз.
    """
    ishodnaya = aliased(Document)
    storno = aliased(Document)
    rows = db.execute(
        select(DocumentLine.product_id, func.coalesce(func.sum(DocumentLine.quantity_milli), 0))
        .join(storno, storno.id == DocumentLine.document_id)
        .join(ishodnaya, ishodnaya.id == storno.basis_id)
        .where(
            ishodnaya.basis_id == order_id,
            ishodnaya.kind == KIND_WAYBILL_OUT,
            storno.kind == KIND_WAYBILL_IN,
            storno.status.in_((STATUS_ISSUED, STATUS_CLOSED)),
            DocumentLine.product_id.is_not(None),
        )
        .group_by(DocumentLine.product_id)
    ).all()
    return {int(product_id): as_int(summa) for product_id, summa in rows}
