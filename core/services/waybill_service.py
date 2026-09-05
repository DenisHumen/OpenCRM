"""Накладные: бумага, по которой товар физически уезжает со склада.

**Чем накладная отличается от заказа.** Не содержанием — перечень позиций у них
один и тот же (`document_lines`). Заказ ОБЕЩАЕТ: товар числится за клиентом, но
лежит на полке, и остаток его показывает. Накладная ОТДАЁТ: с полки товар ушёл,
и остаток обязан упасть. Отсюда всё остальное — черновик, неизменяемость,
сторнирование.

**Три правила, на которых стоит модуль.**

1. *Черновик правится, проведённая — нет.* Накладную собирают: кладовщик ходит
   по складу со сканером, позиции появляются по одной, что-то не находится и
   заменяется. Всё это — черновик. Момент проведения делит жизнь бумаги надвое:
   до него товар на полке и править можно всё, после — товар уехал, и бумага
   стала документом о свершившемся. Свершившееся не правят.

2. *Исправляют сторнированием, а не правкой.* «Отгрузили шесть, а надо было
   пять» чинится не изменением шестёрки на пятёрку, а обратной накладной на
   одну штуку. Довод не бухгалтерский, а складской: правка стёрла бы факт, что
   шесть штук физически покидали склад, и вопрос «куда делась одна» остался бы
   без ответа. Тот же довод — у возврата покупателя (`return_service`).

3. *К остатку ведёт ровно один путь.* Заказ двигает склад при закрытии,
   накладная — при проведении. Если по одному заказу пройдут оба, товар уедет
   дважды. Разбор и решение — в `_proverit_dvoynuyu_otgruzku` ниже.

**Чего здесь НЕТ намеренно.** Накладная не переводит заявку по воронке (это
делает акт) и не заводит денежных операций сама (их поднимает событие). Оба
отказа — про то же: у одного факта должен быть один хозяин.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from core import events as event_bus
from core import exceptions as errors
from core import references
from core.services import (
    notification_service,
    audit_service,
    company_service,
    document_service,
    modules_service,
    settings_service,
    warehouse_service,
)
from database.models import Document, DocumentEvent, DocumentLine, User
from database.models.audit import SOURCE_MANUAL
from database.models.document import (
    KIND_RETURN,
    KIND_SALES_ORDER,
    KIND_WAYBILL_IN,
    KIND_WAYBILL_OUT,
    OPEN_ORDER_STATUSES,
    ORDER_KINDS,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_ISSUED,
    WAYBILL_KINDS,
)
from database.models.warehouse import MOVE_IN, MOVE_OUT, MOVE_RETURN, MOVE_WRITEOFF
from database.repositories import documents as documents_repo
from database.repositories import warehouse as warehouse_repo

#: Накладная проведена: склад двинулся, бумага ушла с товаром.
#: Подробности: `waybill`, `lines`, `warehouse`.
WAYBILL_POSTED = "waybill.posted"

#: Предел длины названия позиции — тот же, что у заказа: строки общие, и разные
#: пределы означали бы, что одна и та же строка законна в заказе и незаконна в
#: накладной по нему.
MAX_LINE_NAME = 200


# --- чтение -------------------------------------------------------------------


def get(db: Session, document_id: int) -> Document:
    """Накладная по номеру записи. Чужой вид бумаги — отказ.

    Проверка вида здесь, а не в роуте, по той же причине, по которой она стоит у
    заказов: путь сюда сегодня один, а завтра накладные начнут выпускать из
    скрипта или пачкой, и правило не должно зависеть от того, каким путём
    пришли.
    """
    document = document_service.get(db, document_id)
    if document.kind not in WAYBILL_KINDS:
        raise errors.ValidationError(
            "This document is not a waybill", code="not_a_waybill"
        )
    return document


def lines(db: Session, document_id: int) -> list[DocumentLine]:
    return documents_repo.lines_of(db, document_id)


def events(db: Session, document_id: int) -> list[DocumentEvent]:
    return documents_repo.events(db, document_id)


def kto_otpustil(db: Session, waybill: Document) -> str:
    """Имя того, кто провёл накладную, — для строки «Отпустил» на бумаге.

    Берётся из события перехода, а не из `created_by`: черновик заводит один
    человек, а отпускает товар тот, кто нажал «провести», и на бумаге обязан
    стоять второй. Отдельной колонки под это нет намеренно — она стала бы вторым
    местом для факта, который уже записан переходом.
    """
    for event in events(db, waybill.id):
        if event.to_status == STATUS_ISSUED:
            return event.author_name or ""
    return ""


def total_minor(rows: list[DocumentLine]) -> int:
    """Сумма накладной. Счёт общий с заказом и актом — см. `document_service`."""
    return document_service.total_minor(rows)


def search(db: Session, **kwargs) -> tuple[list[Document], int]:
    """Список накладных — и только их.

    `kinds` задаётся здесь, а не приходит снаружи: список накладных, куда
    затесались квитанции, отвечает не на тот вопрос, с которым туда пришли.
    """
    return documents_repo.search(db, kinds=WAYBILL_KINDS, **kwargs)


def po_osnovaniyu(db: Session, basis_id: int) -> list[Document]:
    """Накладные, выписанные на основании этой бумаги.

    Нужен экрану заказа («по нему отгружено вот этим») и проверке двойной
    отгрузки. Отсюда же индекс по `basis_id`.
    """
    return documents_repo.po_osnovaniyu(db, basis_id)


# --- создание и правка черновика ---------------------------------------------


def create(db: Session, data: dict, author: User) -> Document:
    """Завести черновик накладной.

    Склад выбирается ЗДЕСЬ, а не при проведении, и это не удобство. По нему
    считается нехватка ещё на черновике: кладовщик должен видеть «этого на
    складе нет» пока собирает, а не в момент, когда уже подписал бумагу.
    Записанное намерение к тому же делает расхождение «списалось не с того
    склада» обнаружимым запросом — тот же довод, что у `next_stage` у акта.
    """
    kind = data.get("kind") or KIND_WAYBILL_OUT
    if kind not in WAYBILL_KINDS:
        raise errors.ValidationError(f"Unknown waybill kind: {kind}", code="unknown_kind")

    basis = _osnovanie(db, data.get("basis_id"))
    client_id = references.client(db, data.get("client_id"))
    deal_id = references.deal(db, data.get("deal_id"))
    # Основание знает и клиента, и заявку. Не назвали своих — берём его: набивать
    # руками то, что уже написано на заказе, значит однажды набить иначе.
    if basis is not None:
        client_id = client_id or basis.client_id
        deal_id = deal_id or basis.deal_id

    warehouse = None
    if modules_service.is_enabled(db, "warehouse"):
        if basis is not None and basis.kind in WAYBILL_KINDS:
            # Сторно наследует склад исходной ЦЕЛИКОМ, включая его отсутствие.
            # Иначе так: заказ закрыт при выключенном складе (бумага без склада,
            # движений нет), склад включают, жмут «отменить» — и сторно получало
            # основной склад и писало приход на товар, который никуда не уезжал.
            warehouse = (
                warehouse_service.get_warehouse(
                    db, basis.warehouse_id, include_deleted=True
                )
                if basis.warehouse_id
                else None
            )
        else:
            # Не назвали — основной. Молча подставлять его при НЕСКОЛЬКИХ складах
            # нельзя (списание однажды снимет деталь не оттуда, где её взяли), и
            # `resolve_warehouse` этим и занимается.
            warehouse = warehouse_service.resolve_warehouse(db, data.get("warehouse_id"))

    payload = json.dumps(
        {
            "client": _snimok_klienta(db, client_id, basis),
            "company": _snimok_firmy(db, data, basis, deal_id),
            "deal": _snimok_zayavki(db, deal_id, basis),
            "note": str(data.get("note") or "").strip()[:document_service.MAX_TEXT],
        },
        ensure_ascii=False,
    )

    waybill = document_service._insert_with_free_number(
        db,
        status=STATUS_DRAFT,
        kind=kind,
        locale=data.get("locale") or "ru",
        client_id=client_id,
        deal_id=deal_id,
        basis_id=basis.id if basis else None,
        warehouse_id=warehouse.id if warehouse else None,
        payload=payload,
        created_by=author.id,
    )
    _zapisat_perehod(db, waybill, "", STATUS_DRAFT, author, "")
    # Заведение ЧЕРНОВИКА в журнал действий не пишется намеренно.
    #
    # Журнал отвечает на вопросы вида «кто отпустил товар» и «кто дал бухгалтеру
    # доступ». Заведённый и брошенный черновик не отвечает ни на один: по нему
    # ничего не произошло. Записывать его значит утопить настоящие записи в
    # шуме — а журнал читают ровно тогда, когда листать некогда. След при этом
    # не теряется: переход в черновик лежит в истории самой бумаги.
    return waybill


def add_line(db: Session, document_id: int, data: dict, author: User) -> DocumentLine:
    """Добавить позицию. Только в черновик.

    Название и цена фиксируются снимком здесь и сейчас — товар переименуют,
    прайс поменяют, а в накладной обязано остаться то, что вправду уехало.
    """
    waybill = get(db, document_id)
    _tolko_chernovik(waybill)

    quantity = warehouse_service.parse_quantity(data.get("quantity"))
    if quantity is None or quantity <= 0:
        raise errors.ValidationError(
            "Quantity must be greater than zero", code="bad_line_quantity"
        )

    product = None
    product_id = data.get("product_id")
    if product_id is not None:
        product = warehouse_service.get_product(db, product_id)
        if product.is_service:
            # Услугу нельзя ни отгрузить, ни принять: остатка у неё нет и быть
            # не может. У заказа услуга законна (её продают), у накладной — нет:
            # накладная про физическое перемещение вещи.
            raise errors.ValidationError(
                "A service cannot be shipped", code="service_has_no_stock"
            )

    name = (data.get("name") or (product.name if product else "")).strip()
    if not name:
        raise errors.ValidationError("Line needs a name", code="line_name_required")

    price = data.get("price")
    if price is None and product is not None:
        price = product.price_minor

    line = DocumentLine(
        document_id=waybill.id,
        product_id=product.id if product else None,
        name_snapshot=name[:MAX_LINE_NAME],
        quantity_milli=quantity,
        price_minor=price,
        # Себестоимость снимается при проведении, а не сейчас: до отгрузки она
        # ещё может измениться закупкой, и снимок «на момент набора» соврал бы.
        cost_minor=None,
        sort_order=documents_repo.next_sort_order(db, waybill.id),
    )
    documents_repo.add_line(db, line)
    return line


def update_line(db: Session, document_id: int, line_id: int, data: dict) -> DocumentLine:
    waybill = get(db, document_id)
    _tolko_chernovik(waybill)
    line = _stroka(db, waybill.id, line_id)

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
    waybill = get(db, document_id)
    _tolko_chernovik(waybill)
    documents_repo.drop_line(db, _stroka(db, waybill.id, line_id))


def vid_po_zakazu(kind_zakaza: str) -> str:
    """Какой накладной оформляется этот заказ.

    Заказ ПОКУПАТЕЛЯ отгружается — расходной. Заказ ПОСТАВЩИКУ принимается —
    приходной. Перепутать эти два значит двинуть склад в обратную сторону, и
    заметить это по остатку нельзя: он сойдётся сам с собой.
    """
    return KIND_WAYBILL_OUT if kind_zakaza == KIND_SALES_ORDER else KIND_WAYBILL_IN


def po_zakazu(
    db: Session, basis_id: int, author: User, warehouse_id: int | None = None
) -> Document:
    """Черновик накладной, заполненный позициями заказа.

    Кладовщику незачем перебивать руками то, что уже набрано в заказе, — а
    перебитое руками однажды разойдётся с заказом на одну позицию, и заметит
    это клиент.

    Услуги при переносе отбрасываются: их нельзя отгрузить, и в накладной они
    были бы строками, которые ничего не двигают.

    **Вид накладной берётся у заказа, а не подставляется расходной.** Прежде
    здесь стояло `KIND_WAYBILL_OUT` безусловно, и из заказа ПОСТАВЩИКУ выходила
    бумага на отгрузку: проведи её — и товар уехал бы со склада вместо того,
    чтобы прийти. Остаток при этом сходился бы сам с собой, а расхождение с
    действительностью всплыло бы на инвентаризации.

    Основанием принимается только ЗАКАЗ. Прежде брался любой бланк —
    квитанция, акт, другая накладная, — и получалась накладная «по основанию»,
    которое основанием быть не может: ни двойная отгрузка по нему не
    сторожится, ни закрытие заказа его не видит.
    """
    basis = document_service.get(db, basis_id)
    if basis.kind not in ORDER_KINDS:
        raise errors.ValidationError(
            f"Document {basis.number} is not an order", code="basis_is_not_order"
        )
    stroki = documents_repo.lines_of(db, basis.id)
    if not stroki:
        raise errors.ValidationError("This order has no lines", code="order_is_empty")

    waybill = create(
        db,
        {
            "kind": vid_po_zakazu(basis.kind),
            "basis_id": basis.id,
            "client_id": basis.client_id,
            "deal_id": basis.deal_id,
            # Склад приходит от вызывающего: закрытие заказа знает, с какого
            # склада отгружают, а молча подставленный основной однажды снимет
            # деталь не оттуда, где её взяли.
            "warehouse_id": warehouse_id,
        },
        author,
    )
    for row in stroki:
        # Накладная — о товаре: услуги и разовые позиции («упаковка») в неё не
        # едут, как и у черновика-зеркала; иначе одна и та же бумага выходила
        # бы с «упаковкой» или без неё в зависимости от настройки автоматики.
        if row.product_id is None:
            continue
        product = warehouse_service.get_product(db, row.product_id, include_deleted=True)
        if product.is_service:
            continue
        documents_repo.add_line(
            db,
            DocumentLine(
                document_id=waybill.id,
                product_id=row.product_id,
                name_snapshot=row.name_snapshot,
                quantity_milli=row.quantity_milli,
                price_minor=row.price_minor,
                cost_minor=None,
                sort_order=row.sort_order,
            ),
        )
    return waybill


# --- черновик по заказу сам --------------------------------------------------


def chernovik_po_zakazu(db: Session, basis_id: int) -> Document | None:
    """Непроведённая накладная по заказу, если есть. Одна: заводит её зеркало."""
    for waybill in documents_repo.po_osnovaniyu(db, basis_id):
        if waybill.status == STATUS_DRAFT:
            return waybill
    return None


def avto(waybill: Document) -> bool:
    """Заведена ли бумага сама, по заказу, а не руками."""
    return bool(payload_of(waybill).get("auto"))


def zerkalo_po_zakazu(db: Session, order: Document, author: User | None) -> None:
    """Черновик накладной повторяет заказ, пока не проведён.

    Владелец просил (05.09.2026), чтобы бумага появлялась сама, а не по кнопке
    в конце: кладовщик видит, что собирать, с первой строки заказа, и правка
    заказа не расходится с накладной на одну позицию. Выключается настройкой
    `auto_waybill`; при выключенном блоке накладных сюда не приходят вовсе.

    Заказ без товарных строк — черновик не нужен: заведённый сами убираем,
    заведённый руками оставляем человеку.
    """
    if settings_service.get_all(db).get("auto_waybill", "1") != "1":
        return
    if order.status not in OPEN_ORDER_STATUSES:
        return
    tovarnye = [
        row for row in documents_repo.lines_of(db, order.id)
        if row.product_id is not None
        and not warehouse_service.get_product(db, row.product_id, include_deleted=True).is_service
    ]
    chernovik = chernovik_po_zakazu(db, order.id)
    if not tovarnye:
        if chernovik is not None and avto(chernovik):
            documents_repo.drop(db, chernovik)
        return
    if chernovik is None:
        if author is None:
            return
        # По заказу уже есть накладная, которая жила: проведённая (товар уехал,
        # второй черновик — вторая отгрузка) или отменённая руками (человек
        # сказал «не надо», и заводить снова после каждой правки — спорить).
        if documents_repo.po_osnovaniyu(db, order.id):
            return
        chernovik = po_zakazu(db, order.id, author, None)
        snimok = payload_of(chernovik)
        snimok["auto"] = True
        chernovik.payload = json.dumps(snimok, ensure_ascii=False)
        db.flush()
        # Сказать и автору: бумага появилась не по его нажатию, и без слов он
        # узнает о ней из списка накладных, куда не собирался.
        notification_service.notify(
            db,
            notification_service.adresaty(db, "waybills") ,
            "auto_waybill",
            {"number": chernovik.number, "order": order.number},
            f"/waybills/{chernovik.id}",
        )
        return
    # Строки переписываются целиком: считать разницу построчно значило бы
    # держать два перечня согласованными по номерам строк, а не по сути.
    for row in documents_repo.lines_of(db, chernovik.id):
        documents_repo.drop_line(db, row)
    for row in tovarnye:
        documents_repo.add_line(
            db,
            DocumentLine(
                document_id=chernovik.id,
                product_id=row.product_id,
                name_snapshot=row.name_snapshot,
                quantity_milli=row.quantity_milli,
                price_minor=row.price_minor,
                cost_minor=None,
                sort_order=row.sort_order,
            ),
        )


def ubrat_avto_chernovik(db: Session, order: Document) -> None:
    """Заказ отменён — черновик, заведённый по нему сам, уходит вместе с ним."""
    chernovik = chernovik_po_zakazu(db, order.id)
    if chernovik is not None and avto(chernovik):
        documents_repo.drop(db, chernovik)


# --- проведение ---------------------------------------------------------------


def provesti(
    db: Session,
    document_id: int,
    author: User,
    confirm_negative: bool = False,
    *,
    po_zakrytiyu_zakaza: bool = False,
) -> Document:
    """Провести накладную: товар уехал, остаток обязан упасть.

    **Одной транзакцией**: статус и движения склада едут вместе. Половина
    проведения — это списанный товар при накладной, которая числится черновиком,
    или наоборот; разбирать такое потом придётся по журналу вручную. Транзакция
    открыта не здесь, а в `web/api/deps.py:get_db`.

    Двойное нажатие ловится условной сменой статуса, а не проверкой: проверка
    гоняется, условный UPDATE — нет.

    Нехватка на складе ОСТАНАВЛИВАЕТ. У ручного движения принято «разрешаем с
    предупреждением» (товар отдали, приход занести забыли), у накладной этого
    мало: отгрузить нечего физически. Остановка и явное подтверждение, которое
    записывается в историю бумаги.
    """
    waybill = get(db, document_id)
    _tolko_chernovik(waybill)

    rows = documents_repo.lines_of(db, waybill.id)
    if not rows:
        # Провести пустую накладную значит закрыть её, ничего не отгрузив, — и
        # обнаружить это, когда клиент приедет за товаром.
        raise errors.ValidationError("This waybill has no lines", code="waybill_is_empty")

    if not po_zakrytiyu_zakaza:
        # Обычный путь: накладную по закрытому заказу проводить нельзя —
        # товар уехал бы дважды. Но когда накладную выписывает САМО закрытие
        # заказа, запрет сработал бы на своей же бумаге: статус заказа к
        # этому мгновению уже сменён (иначе двое, нажавшие разом, отгрузили
        # бы каждый своей накладной).
        #
        # Флаг именованный и только по ключевому слову: позиционным его
        # однажды передали бы вместо `confirm_negative`, и запрет на двойную
        # отгрузку снялся бы молча.
        _proverit_dvoynuyu_otgruzku(db, waybill)

    ishodyashchaya = waybill.kind == KIND_WAYBILL_OUT
    sklad_vklyuchen = modules_service.is_enabled(db, "warehouse")
    goods = [row for row in rows if row.product_id is not None]

    # Склад бумаги решается при заведении и потом не меняется. НЕТ склада —
    # бумага остатка не касается: при выключенном блоке она такой и родилась.
    # `resolve_warehouse` подставил бы основной, и сторно такой накладной писало
    # бы приход на товар, который никуда не уезжал: остаток рос из ничего.
    dvigaet_sklad = sklad_vklyuchen and waybill.warehouse_id is not None

    warehouse = None
    if dvigaet_sklad:
        warehouse = warehouse_service.get_warehouse(
            db, waybill.warehouse_id, include_deleted=True
        )
        # Занимаем товары ДО проверки нехватки. Между вопросом «сколько на
        # складе» и записью движения есть окно, и в него попадают двое: две
        # накладные на последние две единицы проходят обе, со склада уходит
        # четыре, и подтверждения не даёт никто. Замерено дуэлью на заказах —
        # разбор в `order_service.close`.
        #
        # Порядок по id обязателен: двое, берущие одни и те же товары в разном
        # порядке, встают друг против друга насмерть.
        for product_id in sorted({row.product_id for row in goods}):
            warehouse_repo.zapert_tovar(db, product_id)

        if ishodyashchaya and goods and not confirm_negative:
            short = warehouse_service.shortages(db, goods, warehouse.id)
            if short:
                raise errors.ValidationError(
                    "Not enough stock: " + ", ".join(short), code="not_enough_stock"
                )

    previous = waybill.status
    if not documents_repo.take_status(db, waybill, expected=previous, status=STATUS_ISSUED):
        raise errors.ConflictError(
            "The waybill has already been processed by someone else",
            code="document_status_changed",
        )

    if dvigaet_sklad:
        for row in goods:
            product = warehouse_service.get_product(db, row.product_id, include_deleted=True)
            row.cost_minor = product.cost_minor
            warehouse_service.add_move(
                db,
                {
                    "product_id": row.product_id,
                    "kind": _vid_dvizheniya(db, waybill, ishodyashchaya),
                    "quantity": warehouse_service.format_quantity(row.quantity_milli),
                    "warehouse_id": warehouse.id,
                    "deal_id": waybill.deal_id,
                    "comment": (
                        f"{'shipped' if ishodyashchaya else 'received'} "
                        f"by waybill {waybill.number}"
                    ),
                    "document_id": waybill.id,
                },
                author,
                # Движения молчаливые: в ленту клиента идёт одна строка про
                # накладную (`core/subscriptions.py`), а не по строке на позицию.
                announce=False,
            )

    # Деньги — событием, а не прямым вызовом финансов: накладные обязаны
    # работать при выключенном блоке денег, и `core/modules.py` связь
    # «waybills → finance» не объявляет.
    event_bus.emit(
        WAYBILL_POSTED,
        db=db,
        actor=author,
        reason=f"waybill {waybill.number} posted",
        source=SOURCE_MANUAL,
        source_ref=waybill.number,
        waybill=waybill,
        lines=rows,
        warehouse=warehouse,
        from_status=previous,
    )

    if not sklad_vklyuchen:
        primechanie = "warehouse module off, no stock moves"
    elif waybill.warehouse_id is None:
        primechanie = "no warehouse on this paper, no stock moves"
    else:
        primechanie = ""
    if confirm_negative:
        # Подтверждение нехватки записывается в историю бумаги, а не проходит
        # молча: «отгрузили в минус» — это решение человека, и через месяц
        # спросят, кто его принял.
        primechanie = "posted with a confirmed shortage"
    _zapisat_perehod(db, waybill, previous, STATUS_ISSUED, author, primechanie)
    audit_service.record(
        db,
        action=audit_service.ACTION_WAYBILL_POSTED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=waybill.id,
        entity_label=waybill.number,
        before=previous,
        after=STATUS_ISSUED,
    )
    if not po_zakrytiyu_zakaza and waybill.basis_id is not None:
        osnovanie = documents_repo.get(db, waybill.basis_id)
        if osnovanie is not None and osnovanie.kind in ORDER_KINDS:
            # Заказ ввозит накладные на уровне модуля — обратный ввоз только здесь.
            from core.services import order_service

            order_service.zakryt_po_nakladnoy(db, osnovanie, waybill, author)
    return waybill


def podtverdit(db: Session, document_id: int, author: User, note: str = "") -> Document:
    """Получатель подтвердил приёмку.

    Ничего не двигает — и в этом смысл отдельного состояния. «Отгружено» и
    «принято» разные факты, и спор «вы нам не привозили» решается записью с
    временем о втором, а не текущим состоянием. До этой отметки накладная висит
    в списке «отгружено, не подтверждено» — том самом, по которому обзванивают.
    """
    waybill = get(db, document_id)
    if waybill.status != STATUS_ISSUED:
        raise errors.ValidationError(
            "Only a posted waybill can be confirmed", code="waybill_not_posted"
        )
    if not documents_repo.take_status(
        db, waybill, expected=STATUS_ISSUED, status=STATUS_CLOSED
    ):
        raise errors.ConflictError(
            "The waybill has already been changed by someone else",
            code="document_status_changed",
        )
    _zapisat_perehod(db, waybill, STATUS_ISSUED, STATUS_CLOSED, author, note)
    return waybill


def otmenit(db: Session, document_id: int, author: User, note: str = "") -> Document:
    """Отменить черновик. Проведённую — нельзя, для неё сторнирование.

    Разница не формальная. Отменённый черновик — это бумага, по которой НИЧЕГО
    не происходило: товар с полки не уезжал. Отменить проведённую значило бы
    объявить несостоявшимся то, что состоялось, и оставить остаток
    неисправленным.
    """
    waybill = get(db, document_id)
    _tolko_chernovik(waybill, deystvie="отменить")
    if not documents_repo.take_status(
        db, waybill, expected=STATUS_DRAFT, status=STATUS_CANCELLED
    ):
        raise errors.ConflictError(
            "The waybill has already been changed by someone else",
            code="document_status_changed",
        )
    _zapisat_perehod(db, waybill, STATUS_DRAFT, STATUS_CANCELLED, author, note)
    return waybill


def stornirovat(db: Session, document_id: int, author: User) -> Document:
    """Исправить проведённую — обратной накладной, а не правкой.

    **Почему не правка.** Правка стёрла бы факт: шесть штук физически покидали
    склад, и вопрос «куда делась одна» остался бы без ответа. Склад обязан
    помнить, что уходило и что вернулось; сотри мы движение — остаток сойдётся,
    а ошибка станет невидимой. Тот же довод — у возврата покупателя.

    Сторнирующая накладная рождается ЧЕРНОВИКОМ, а не проведённой сразу. Это
    намеренно: возврат тоже бывает частичным («вернули четыре из шести»), и
    провести его целиком за человека значит решить за него, чего он не говорил.
    Позиции переносятся полностью, лишнее он удалит.
    """
    ishodnaya = get(db, document_id)
    if ishodnaya.status not in (STATUS_ISSUED, STATUS_CLOSED):
        raise errors.ValidationError(
            "Only a posted waybill can be reversed", code="waybill_not_posted"
        )
    # Второе сторно по той же бумаге — почти всегда двойное нажатие, а не
    # замысел. Настоящий частичный возврат в два захода делается правкой строк
    # одного черновика, а не двумя сторно.
    for sosed in po_osnovaniyu(db, ishodnaya.id):
        if sosed.status != STATUS_CANCELLED:
            raise errors.ValidationError(
                f"Waybill {ishodnaya.number} is already reversed by {sosed.number}",
                code="waybill_already_reversed",
            )

    obratnyy = (
        KIND_WAYBILL_IN if ishodnaya.kind == KIND_WAYBILL_OUT else KIND_WAYBILL_OUT
    )
    storno = create(
        db,
        {
            "kind": obratnyy,
            "basis_id": ishodnaya.id,
            "client_id": ishodnaya.client_id,
            "deal_id": ishodnaya.deal_id,
            "warehouse_id": ishodnaya.warehouse_id,
            # По-английски, как всё, что пишет сама система: примечание
            # уезжает НА БУМАГУ (`waybill_print.html`), и англоязычная
            # накладная выходила бы с русской строкой посреди листа.
            "note": f"reversal of waybill {ishodnaya.number}",
        },
        author,
    )
    for row in documents_repo.lines_of(db, ishodnaya.id):
        documents_repo.add_line(
            db,
            DocumentLine(
                document_id=storno.id,
                product_id=row.product_id,
                name_snapshot=row.name_snapshot,
                quantity_milli=row.quantity_milli,
                price_minor=row.price_minor,
                cost_minor=None,
                sort_order=row.sort_order,
            ),
        )
    audit_service.record(
        db,
        action=audit_service.ACTION_WAYBILL_REVERSED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=ishodnaya.id,
        entity_label=ishodnaya.number,
        before=ishodnaya.status,
        after=f"reversed by {storno.number}",
    )
    return storno


# --- внутреннее ---------------------------------------------------------------


def _tolko_chernovik(waybill: Document, deystvie: str = "изменить") -> None:
    """Проведённую накладную не правят. Это и есть неизменяемость.

    Проверка стоит в СЛУЖБЕ, а не только в интерфейсе, и это существенно:
    спрятанная кнопка закрывает экран, но не закрывает API. Правило обязано
    держаться там, куда ведут все пути, а не там, где ходит большинство.
    """
    if waybill.status != STATUS_DRAFT:
        raise errors.ValidationError(
            f"A posted waybill cannot be changed ({deystvie}); issue a reversal instead",
            code="waybill_is_final",
        )


def _osnovanie(db: Session, basis_id) -> Document | None:
    if basis_id is None:
        return None
    return document_service.get(db, basis_id)


def _stroka(db: Session, document_id: int, line_id: int) -> DocumentLine:
    line = documents_repo.get_line(db, document_id, line_id)
    if line is None:
        raise errors.NotFoundError("Line not found", code="line_not_found")
    return line


def _vid_dvizheniya(db: Session, waybill: Document, ishodyashchaya: bool) -> str:
    """Как назвать движение склада: обычным приходом-расходом или возвратом.

    Обратное движение обязано называться тем, чем оно является, иначе журнал
    склада перестаёт читаться словами: «приход» у сторнированной отгрузки
    выглядит как новая поставка, а это возврат.

    Правило записано у прежней отмены заказа (её больше нет — есть возврат,
    docs/22), и при переезде закрытия на накладную оно чуть не потерялось: сторно писало бы
    голые `in`/`out`, и различить в журнале поставку от возврата стало бы
    нечем. Поймано CI — старая проверка отмены искала в движениях `return`.

    Сторно узнаётся по основанию: у обычной накладной это заказ или пусто, у
    сторнирующей — другая накладная.
    """
    if waybill.basis_id is None:
        return MOVE_OUT if ishodyashchaya else MOVE_IN
    osnovanie = documents_repo.get(db, waybill.basis_id)
    if osnovanie is None:
        return MOVE_OUT if ishodyashchaya else MOVE_IN
    # Приходная по возврату покупателя — тоже возврат, а не поставка.
    if osnovanie.kind == KIND_RETURN:
        return MOVE_RETURN
    if osnovanie.kind not in WAYBILL_KINDS:
        return MOVE_OUT if ishodyashchaya else MOVE_IN
    # Возврат на склад — `return`, снятие с него — `writeoff`.
    return MOVE_WRITEOFF if ishodyashchaya else MOVE_RETURN


def _proverit_dvoynuyu_otgruzku(db: Session, waybill: Document) -> None:
    """К остатку ведёт ровно один путь. Здесь это и обеспечивается.

    **В чём опасность.** Заказ двигает склад при закрытии
    (`order_service.close`), накладная — при проведении. Если по одному заказу
    сделают оба, товар уедет со склада дважды, а остаток покажет минус, которого
    никто не объяснит.

    **Почему проверка, а не переписывание `close`.** Правильное решение —
    заставить закрытие заказа выписывать накладную, и тогда путь останется один
    физически. Это переписывание живого проведения заказов на работающем
    сервере, и оно заслуживает отдельного захода с отдельной проверкой. До тех
    пор инвариант держится взаимным запретом: накладная не проводится по
    закрытому заказу, а заказ не закрывается при проведённой накладной (вторая
    половина — в `order_service.close`).

    Взаимный запрет слабее физического: он живёт в коде, а не в устройстве. Но
    он полный — обойти его можно только мимо обеих служб, то есть мимо всего
    приложения.
    """
    if waybill.basis_id is None:
        return
    osnovanie = documents_repo.get(db, waybill.basis_id)
    if osnovanie is None:
        return
    # Основание-накладная — это сторно, и там проверка не нужна: обратная
    # бумага и должна выписываться по проведённой.
    if osnovanie.kind in WAYBILL_KINDS:
        return
    if osnovanie.status == STATUS_CLOSED:
        raise errors.ValidationError(
            f"Order {osnovanie.number} is already closed and has moved the stock; "
            f"posting this waybill would ship the goods twice",
            code="basis_already_shipped",
        )


def _snimok_klienta(db: Session, client_id, basis: Document | None) -> dict:
    """Имя и телефон получателя снимком.

    Тот же довод, что у квитанции: у человека на руках бумага, и она обязана
    совпадать с записью, даже если клиента потом переименовали. Берём из
    основания, если оно есть, — там снимок уже сделан и повторять его вычисление
    значит завести второй ответ на тот же вопрос.
    """
    if basis is not None:
        snimok = document_service.payload_of(basis).get("client")
        if snimok:
            return snimok
    if client_id is None:
        return {"name": "", "phone": "", "email": ""}
    from database.repositories import clients as clients_repo

    client = clients_repo.get(db, client_id)
    if client is None:
        return {"name": "", "phone": "", "email": ""}
    return {"name": client.name, "phone": client.phone or "", "email": client.email or ""}


def _snimok_zayavki(db: Session, deal_id, basis: Document | None) -> dict:
    """Название заявки снимком. Тот же довод, что у клиента: заявку переименуют.

    Пустой словарь у накладной без заявки — строка на бумаге тогда не печатается
    вовсе; писать «Заявка: —» значит занимать место ответом «ничего».
    """
    if basis is not None:
        snimok = document_service.payload_of(basis).get("deal")
        if snimok and snimok.get("title"):
            return snimok
    if deal_id is None:
        return {}
    from database.repositories import deals as deals_repo

    deal = deals_repo.get(db, deal_id)
    return {"id": deal.id, "title": deal.title} if deal else {}


def _snimok_firmy(db: Session, data: dict, basis: Document | None, deal_id) -> dict:
    """Реквизиты фирмы снимком, в том виде, в каком уйдут на бумагу.

    Довод дословно тот же, что у квитанции (`document_service._company_snapshot`):
    фирма сменит банк, и перепечатанная через полгода накладная покажет новый
    счёт там, где у получателя на руках лежит бумага со старым.
    """
    if basis is not None:
        snimok = document_service.payload_of(basis).get("company")
        if snimok:
            return snimok
    from database.repositories import deals as deals_repo

    deal = deals_repo.get(db, deal_id) if deal_id else None
    company = company_service.for_document(db, data.get("company_id"), deal)
    return document_service._company_snapshot(company, settings_service.get_all(db))


def _zapisat_perehod(
    db: Session,
    waybill: Document,
    previous: str,
    status: str,
    author: User,
    note: str,
) -> None:
    documents_repo.add_event(
        db,
        DocumentEvent(
            document_id=waybill.id,
            from_status=previous,
            to_status=status,
            note=(note or "")[:document_service.MAX_NOTE],
            author_id=author.id,
            # Имя СНИМКОМ, а не только ссылкой. `author_id` объявлен SET NULL, и
            # после увольнения кладовщика история накладной отвечала бы «кто-то»
            # на вопрос, кто отпустил товар, — причём задним числом, по всем
            # прошлым записям сразу.
            author_name=(author.name or "")[:120],
        ),
    )


def payload_of(waybill: Document) -> dict:
    return document_service.payload_of(waybill)
