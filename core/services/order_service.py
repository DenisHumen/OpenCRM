"""Заказы: перечень позиций, резерв, отгрузка и приёмка.

Заказ — это **вид бланка**, а не отдельная сущность (разбор — в докстроке
`database/models/document.py`). Поэтому номера, статусы, печать, поиск сканом и
связь с заявкой берутся готовыми, а здесь живёт только то, чего у квитанции
нет: строки, обещания складу и проведение.

Три решения, на которых всё держится.

**Резерв — отдельное понятие, а не «минус остаток».** Самая частая ошибка —
списывать при создании заказа: тогда «продали» и «отложили» становятся одним, и
на вопрос «что физически лежит на полке» ответить нечем. Поэтому три числа:
остаток (сколько есть), резерв (сколько обещано покупателям), доступно = остаток
− резерв. Зеркальное число у заказа поставщику — «ожидается».

**Резерв считается запросом, а не хранится.** Довод тот же, что у остатка
склада, и следствие важнее самого правила: заказ отменили — обещание исчезло
само, без единой строки кода на уборку. Хранимое число дало бы вечный призрачный
резерв, и найти его источник было бы нечем.

**Движение склада случается ровно на одном переходе** — в `closed`. Создание
заказа склад не трогает вовсе; отмена — тоже. Это делает «отгружено» и
«обещано» разными событиями, а не оттенками одного.

Защита от двойной отгрузки стоит **на условной смене статуса** (`take_status`),
а не на проверке «уже отгружен» в сервисе. Проверка гоняется: двое нажали разом,
оба прочитали `issued`, оба списали. Условный UPDATE пропускает ровно одного —
тот же приём, что у этапа заявки и у последнего root.
"""

import json

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import audit_service, document_service, modules_service, warehouse_service
from core.utils import now_utc
from database.models import Document, DocumentEvent, DocumentLine, User
from database.models.audit import SOURCE_MANUAL
from database.models.document import (
    KIND_PURCHASE_ORDER,
    KIND_SALES_ORDER,
    OPEN_ORDER_STATUSES,
    ORDER_KINDS,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_ISSUED,
    STATUS_READY,
)
from database.models.warehouse import MOVE_IN, MOVE_OUT, MOVE_RETURN, MOVE_WRITEOFF
from database.repositories import documents as documents_repo
from database.repositories import warehouse as warehouse_repo

MAX_LINE_NAME = 200


# --- обещания складу ----------------------------------------------------------


def reserved(db: Session, product_ids: list[int] | None = None) -> dict[int, int]:
    """Сколько обещано покупателям по неотгруженным заказам: {product_id: тысячные}."""
    return documents_repo.promised(db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, product_ids)


def expected(db: Session, product_ids: list[int] | None = None) -> dict[int, int]:
    """Сколько приедет по заказам поставщику. Нужно, чтобы не заказать дважды."""
    return documents_repo.promised(db, KIND_PURCHASE_ORDER, OPEN_ORDER_STATUSES, product_ids)


def availability(db: Session, product_ids: list[int]) -> dict[int, dict[str, int]]:
    """Остаток, резерв, ожидается и доступно — по каждому товару.

    Три запроса на весь список, а не три на строку.

    Блок заказов выключен — резерва и ожидания не существует, и «доступно»
    снова равно остатку. Это правильно: раздела нет, обещаний тоже. Проверка
    здесь, а не в реестре зависимостей: склад работает и без заказов, и
    объявлять эту связь жёсткой значило бы запретить склад тому, кому заказы не
    нужны (см. `core/modules.py`).
    """
    if not product_ids:
        return {}
    stock = warehouse_repo.stock_by_product(db, product_ids)
    if modules_service.is_enabled(db, "orders"):
        hold = reserved(db, product_ids)
        coming = expected(db, product_ids)
    else:
        hold, coming = {}, {}
    return {
        product_id: {
            "stock_milli": stock.get(product_id, 0),
            "reserved_milli": hold.get(product_id, 0),
            "expected_milli": coming.get(product_id, 0),
            "available_milli": stock.get(product_id, 0) - hold.get(product_id, 0),
        }
        for product_id in product_ids
    }


# --- заказ --------------------------------------------------------------------


def create(db: Session, data: dict, author: User) -> Document:
    """Завести заказ. Позиции добавляются отдельно — их может не быть сразу.

    Пустой заказ законен: у стойки сначала заводят бумагу, потом набивают
    позиции сканером. Отказать в пустом значит заставить набирать первую позицию
    раньше, чем заказ вообще появился.
    """
    kind = data.get("kind")
    if kind not in ORDER_KINDS:
        raise errors.ValidationError(f"Not an order kind: {kind}", code="not_an_order")

    fields = dict(data)
    # Квитанция требует описания вещи, заказу оно не нужно: у него есть строки.
    fields["item"] = data.get("item") or _title(kind)
    # **Заказ без клиента законен, и это не поблажка.** У заказа поставщику
    # клиента нет по устройству: он адресован поставщику, а поставщик отдельной
    # сущностью в системе не заведён. У заказа покупателя клиент бывает не
    # сразу: у стойки сначала набивают позиции, а карточку заводят, когда
    # покупатель назвал телефон, — и часто не заводят вовсе.
    #
    # Бланк при этом требует хоть какое-то имя на бумаге. Подставляем название
    # вида; вписать покупателя или поставщика можно потом.
    #
    # Без этого заказ не создавался ВООБЩЕ ни один: система отвечала «укажите
    # клиента» на запрос, где клиента может не быть по существу. Поймано живым
    # прогоном на стенде — кнопка «Заказ покупателя» на экране создаёт ровно
    # такой запрос.
    if not data.get("client_id"):
        fields["client_name"] = (data.get("client_name") or "").strip() or _title(kind)
    return document_service.create(db, fields, author)


def _title(kind: str) -> str:
    return "Заказ покупателя" if kind == KIND_SALES_ORDER else "Заказ поставщику"


def get(db: Session, document_id: int) -> Document:
    """Заказ по номеру записи. Квитанция сюда не проходит: у неё нет строк, и
    отгружать её нечем."""
    document = document_service.get(db, document_id)
    if document.kind not in ORDER_KINDS:
        raise errors.ValidationError("This document is not an order", code="not_an_order")
    return document


def lines(db: Session, document_id: int) -> list[DocumentLine]:
    return documents_repo.lines_of(db, document_id)


def add_line(db: Session, document_id: int, data: dict, author: User) -> DocumentLine:
    """Добавить позицию. Название и цена фиксируются снимком — здесь и сейчас.

    Товар переименуют, прайс поменяют — в заказе останется то, что человек
    заказывал. Ровно та же причина, по которой у бланка снимок для печати.
    """
    order = get(db, document_id)
    _assert_open(order)

    quantity = warehouse_service.parse_quantity(data.get("quantity"))
    if quantity is None or quantity <= 0:
        raise errors.ValidationError(
            "Quantity must be greater than zero", code="bad_line_quantity"
        )

    product = None
    product_id = data.get("product_id")
    if product_id is not None:
        product = warehouse_service.get_product(db, product_id)
        if product.is_service and order.kind == KIND_PURCHASE_ORDER:
            # Услугу нельзя принять на склад: остатка у неё нет и быть не может.
            raise errors.ValidationError("A service has no stock", code="service_has_no_stock")

    name = (data.get("name") or (product.name if product else "")).strip()
    if not name:
        # Разовая позиция без карточки товара законна («доставка», «упаковка»),
        # но названа она быть обязана: строка без имени не читается ни в заказе,
        # ни в печати.
        raise errors.ValidationError("Line needs a name", code="line_name_required")

    price = data.get("price")
    if price is None and product is not None:
        price = product.price_minor

    line = DocumentLine(
        document_id=order.id,
        product_id=product.id if product else None,
        name_snapshot=name[:MAX_LINE_NAME],
        quantity_milli=quantity,
        price_minor=price,
        # Себестоимость снимаем при проведении, а не сейчас: до отгрузки она
        # ещё может измениться закупкой, и снимок «на момент заказа» соврал бы.
        cost_minor=None,
        sort_order=documents_repo.next_sort_order(db, order.id),
    )
    documents_repo.add_line(db, line)
    return line


def update_line(db: Session, document_id: int, line_id: int, data: dict) -> DocumentLine:
    order = get(db, document_id)
    _assert_open(order)
    line = _line(db, order.id, line_id)

    if "quantity" in data:
        quantity = warehouse_service.parse_quantity(data.get("quantity"))
        if quantity is None or quantity <= 0:
            raise errors.ValidationError(
                "Quantity must be greater than zero", code="bad_line_quantity"
            )
        line.quantity_milli = quantity
    if "price" in data:
        line.price_minor = data.get("price")
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise errors.ValidationError("Line needs a name", code="line_name_required")
        line.name_snapshot = name[:MAX_LINE_NAME]
    db.flush()
    return line


def remove_line(db: Session, document_id: int, line_id: int) -> None:
    order = get(db, document_id)
    _assert_open(order)
    documents_repo.drop_line(db, _line(db, order.id, line_id))


def total_minor(rows: list[DocumentLine]) -> int:
    """Сумма заказа в минорных единицах.

    Сам счёт переехал в `document_service`, когда строками начал пользоваться и
    акт выполненных работ: строки общие, значит и место, где по ним считаются
    деньги, обязано быть одно. Здесь остался вход — его зовут сериализатор
    заказа и печатная форма, и переучивать их незачем.
    """
    return document_service.total_minor(rows)


# --- сборка сканером ----------------------------------------------------------


def pick(db: Session, document_id: int, code: str, quantity_milli: int = 1000) -> DocumentLine:
    """Отметить позицию собранной по отсканированному коду.

    Сборщик подносит сканер к коробке — количество растёт. Отсканировали
    лишнее — расхождение видно построчно **до отгрузки**, а не на выдаче: для
    этого `picked_milli` живёт отдельно от `quantity_milli`, а не уменьшает его.

    Кода нет в заказе — отказ с самим кодом внутри: пустой ответ после писка
    сканера читается как «сканер сломался».
    """
    from core.services import barcode_service

    order = get(db, document_id)
    _assert_open(order)
    product = barcode_service.scan(db, code)

    line = documents_repo.line_by_product(db, order.id, product.id)
    if line is None:
        raise errors.NotFoundError(
            f"{product.name} is not in this order", code="product_not_in_order"
        )
    line.picked_milli += quantity_milli
    db.flush()
    return line


# --- проведение ---------------------------------------------------------------


def close(
    db: Session,
    document_id: int,
    author: User,
    warehouse_id: int | None = None,
    confirm_negative: bool = False,
) -> Document:
    """Отгрузить заказ покупателя или принять заказ поставщику.

    **Одной транзакцией**: статус и движения склада едут вместе. Половина
    проведения — это списанный товар при заказе, который числится необработанным,
    или наоборот; разбирать такое потом придётся по журналу вручную.

    Двойное нажатие ловится условной сменой статуса, а не проверкой: проверка
    гоняется, условный UPDATE — нет.

    Нехватка на складе **останавливает**. У ручного движения принято «разрешаем
    с предупреждением» (товар отдали, приход занести забыли), у отгрузки по
    заказу этого мало: отгрузить нечего физически. Остановка и явное
    подтверждение, которое записывается.
    """
    order = get(db, document_id)
    _assert_open(order)

    rows = documents_repo.lines_of(db, order.id)
    if not rows:
        # Провести пустой заказ значит закрыть его, ничего не сделав, — и
        # обнаружить это, когда клиент придёт за товаром.
        raise errors.ValidationError("This order has no lines", code="order_is_empty")

    # Услуги отбрасываем сразу: остатка у них нет, и в проверке нехватки они
    # всегда выглядели бы недостачей — «доставки на складе ноль». Отгрузка
    # заказа, где есть хоть одна услуга, вставала бы намертво.
    goods = [row for row in rows if row.product_id is not None and not _is_service(db, row)]
    # Склад выбирается явно, а не подставляется молча: списание с основного
    # однажды снимет деталь не оттуда, где её взяли. Не назвали — основной.
    warehouse = warehouse_service.resolve_warehouse(db, warehouse_id)
    outgoing = order.kind == KIND_SALES_ORDER

    if outgoing and goods and not confirm_negative:
        short = _shortages(db, goods, warehouse.id)
        if short:
            raise errors.ValidationError(
                "Not enough stock: " + ", ".join(short), code="not_enough_stock"
            )

    previous = order.status
    if not documents_repo.take_status(db, order, expected=previous, status=STATUS_CLOSED):
        raise errors.ConflictError(
            "The order has already been processed by someone else",
            code="document_status_changed",
        )

    for row in goods:
        product = warehouse_service.get_product(db, row.product_id, include_deleted=True)
        row.cost_minor = product.cost_minor
        warehouse_service.add_move(
            db,
            {
                "product_id": row.product_id,
                "kind": MOVE_OUT if outgoing else MOVE_IN,
                "quantity": _as_text(row.quantity_milli),
                "warehouse_id": warehouse.id,
                "deal_id": order.deal_id,
                "comment": f"{'отгрузка' if outgoing else 'приёмка'} по заказу {order.number}",
                "document_id": order.id,
            },
            author,
        )

    _record(db, order, previous, STATUS_CLOSED, author, "")
    audit_service.record(
        db,
        action=audit_service.ACTION_ORDER_CLOSED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=order.id,
        entity_label=order.number,
        before=previous,
        after=STATUS_CLOSED,
    )
    return order


def revert(db: Session, document_id: int, author: User) -> Document:
    """Отменить проведение — обратными движениями, а не стиранием прежних.

    Склад обязан помнить, что уходило и что вернулось: сотри мы движения, и
    остаток сойдётся, а вопрос «куда делись пять матриц» останется без ответа —
    то есть ошибка станет невидимой.

    Заказ после отмены становится отменённым, а не снова открытым: «отгрузили,
    вернули, отгрузили опять» — это два разных события в истории, и склеивать
    их в одно значит терять первое.
    """
    order = get(db, document_id)
    if order.status != STATUS_CLOSED:
        raise errors.ValidationError(
            "Only a processed order can be reverted", code="order_not_closed"
        )

    if not documents_repo.take_status(db, order, expected=STATUS_CLOSED, status=STATUS_CANCELLED):
        raise errors.ConflictError(
            "The order has already been changed by someone else",
            code="document_status_changed",
        )

    for move in warehouse_repo.moves_of_document(db, order.id):
        if move.kind not in (MOVE_IN, MOVE_OUT):
            continue
        warehouse_service.add_move(
            db,
            {
                "product_id": move.product_id,
                # Возврат на склад — `return`, снятие с него — `writeoff`:
                # обратное движение обязано называться тем, чем оно является,
                # иначе журнал склада перестаёт читаться словами.
                "kind": MOVE_RETURN if move.quantity_milli < 0 else MOVE_WRITEOFF,
                "quantity": _as_text(abs(move.quantity_milli)),
                "warehouse_id": move.warehouse_id,
                "deal_id": move.deal_id,
                "cost": move.cost_minor,
                "comment": f"отмена по заказу {order.number}",
                "document_id": order.id,
            },
            author,
        )

    _record(db, order, STATUS_CLOSED, STATUS_CANCELLED, author, "отмена проведения")
    audit_service.record(
        db,
        action=audit_service.ACTION_ORDER_REVERTED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=order.id,
        entity_label=order.number,
        before=STATUS_CLOSED,
        after=STATUS_CANCELLED,
    )
    return order


def cancel(db: Session, document_id: int, author: User, note: str = "") -> Document:
    """Отменить непроведённый заказ. Склада не касается — резерв снимется сам."""
    order = get(db, document_id)
    _assert_open(order)
    return document_service.set_status(db, order.id, STATUS_CANCELLED, author, note)


def mark_ready(db: Session, document_id: int, author: User) -> Document:
    """Собран и ждёт отгрузки. Резерв при этом держится: товар ещё наш."""
    order = get(db, document_id)
    if order.status != STATUS_ISSUED:
        raise errors.ValidationError("Only a new order can be marked ready", code="order_not_new")
    return document_service.set_status(db, order.id, STATUS_READY, author)


# --- мелочи -------------------------------------------------------------------


def _assert_open(order: Document) -> None:
    if order.status not in OPEN_ORDER_STATUSES:
        raise errors.ValidationError(
            "This order is already finished", code="order_finished"
        )


def _is_service(db: Session, row: DocumentLine) -> bool:
    product = warehouse_service.get_product(db, row.product_id, include_deleted=True)
    return product.is_service


def _line(db: Session, document_id: int, line_id: int) -> DocumentLine:
    line = documents_repo.get_line(db, document_id, line_id)
    if line is None:
        raise errors.NotFoundError("Line not found", code="line_not_found")
    return line


def _shortages(db: Session, rows: list[DocumentLine], warehouse_id: int) -> list[str]:
    """Чего и сколько не хватает на складе. Пусто — отгружать можно.

    Сам ответ переехал в склад, когда тот же вопрос перед проведением начал
    задавать акт: «чего не хватает» — вопрос к складу, и двух ответов на него
    быть не должно. Разошлись бы они не в цифрах, а в формулировке, и человек,
    привыкший к одной, не узнал бы вторую.
    """
    return warehouse_service.shortages(db, rows, warehouse_id)


def _as_text(quantity_milli: int) -> str:
    """Количество обратно в строку: `add_move` разбирает его сам и на своих
    правилах, и обходить его разбор значит завести второй."""
    return warehouse_service.format_quantity(quantity_milli)


def _record(
    db: Session, order: Document, previous: str, status: str, author: User, note: str
) -> None:
    documents_repo.add_event(
        db,
        DocumentEvent(
            document_id=order.id,
            from_status=previous,
            to_status=status,
            note=note,
            author_id=author.id,
        ),
    )


def payload_of(order: Document) -> dict:
    return json.loads(order.payload or "{}")


def touched_at(order: Document):
    return order.updated_at or now_utc()
