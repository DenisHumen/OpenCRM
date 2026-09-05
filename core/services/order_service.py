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

# Под псевдонимом, как в `act_service`: у заказа есть свои «события» в смысле
# истории бумаги, и путать их с механизмом связи блоков нельзя.
from core import events as event_bus
from core import exceptions as errors
from core.services import (
    audit_service,
    client_service,
    document_service,
    deal_service,
    modules_service,
    reserve_service,
    warehouse_service,
    waybill_service,
)
from core.utils import normalize_phone, now_utc
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
from database.models.warehouse import MOVE_IN, MOVE_OUT
from database.repositories import clients as clients_repo
from database.repositories import deal_lines as lines_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo
from database.repositories import warehouse as warehouse_repo

MAX_LINE_NAME = 200

#: Заказ проведён: отгружен покупателю или принят от поставщика.
#:
#: Подробности: `order`, `lines`, `from_status`.
#:
#: Событие поднимается ПОСЛЕ условной смены статуса и ПОСЛЕ движений склада, но
#: ДО того, как операция признана состоявшейся. Все три границы жёсткие:
#:
#: - не раньше `take_status`: до захвата статуса подписчиков позвал бы и
#:   проигравший гонку «двое нажали разом», то есть расходы записались бы дважды
#:   по одному заказу. Условный UPDATE — единственная защита, и деньги обязаны
#:   стоять за ней;
#: - не раньше движений склада: отказ «не хватает на складе» обязан прийти до
#:   того, как кто-то тронул деньги;
#: - не позже возврата из функции: после того как операция признана
#:   состоявшейся, отказ участника уже ничего не отменит.
ORDER_CLOSED = "order.closed"

#: Строки заказа изменились (добавили, поправили, убрали). Слушает блок накладных:
#: черновик по заказу обязан повторять заказ, пока не проведён.
ORDER_LINES_CHANGED = "order.lines_changed"
#: Заказ отменён руками. Черновик накладной по нему больше не нужен.
ORDER_CANCELLED = "order.cancelled"

#: Сколько кандидатов показываем, когда клиент нашёлся не один.
#:
#: Шесть — как у командной палитры: список, в котором нельзя выбрать глазами,
#: это не выбор, а вторая задача. Больше шести означает, что искали не по тому.
MAX_CLIENT_CANDIDATES = 6


# --- обещания складу ----------------------------------------------------------


# --- заказ --------------------------------------------------------------------


def create(db: Session, data: dict, author: User) -> tuple[Document, bool]:
    """Завести заказ. Позиции добавляются отдельно — их может не быть сразу.

    Пустой заказ законен: у стойки сначала заводят бумагу, потом набивают
    позиции сканером. Отказать в пустом значит заставить набирать первую позицию
    раньше, чем заказ вообще появился.

    Отвечает парой: сам заказ и «завели ли под него новую карточку клиента».
    Второе — не украшение ответа, а обязательство перед экраном: карточка,
    заведённая втихую, через месяц заводится второй раз.
    """
    kind = data.get("kind")
    if kind not in ORDER_KINDS:
        raise errors.ValidationError(f"Not an order kind: {kind}", code="not_an_order")

    fields = dict(data)
    # Квитанция требует описания вещи, заказу оно не нужно: у него есть строки.
    fields["item"] = data.get("item") or _title(kind)
    fields["client_id"], created_client = _resolve_client(db, data, author)
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
    if not fields.get("client_id"):
        fields["client_name"] = (data.get("client_name") or "").strip() or _title(kind)
    return document_service.create(db, fields, author), created_client


def sozdat_iz_zayavki(db: Session, deal, author: User) -> Document:
    """Завести заказ покупателя по заявке, перенеся в него её товары.

    **Свои траты не переносятся** (решение владельца 30.08.2026): по заказу
    кладовщик собирает коробки, и строка «упаковка» ему мешает — сборка
    показывала бы «собрано 0 из 1», пока её не отметят руками. Сумма упаковки
    остаётся в заявке и попадает в итог сделки.

    Услуги не переносятся по той же причине: собирать нечего.

    Бронь при этом НЕ удваивается: заказ перенимает её у заявки, потому что
    считается она вычитанием (`reserve_service`), а не сложением двух списков.
    """
    if deal.closed_at is not None:
        raise errors.ValidationError("The deal is closed", code="deal_closed")
    # Кнопку нажимают дважды. Второй заказ повторил бы те же строки, и `promised`
    # посчитал бы их обоими: три штуки в заявке стали бы шестью в брони, и
    # продавец отказал бы покупателю, глядя на товар, лежащий на полке.
    # Очередь на заявку: между проверкой и вставкой есть окно, и в него
    # попадают два нажатия «Собрать заказ». Разбор — `deals_repo.zapert_zayavku`.
    deals_repo.zapert_zayavku(db, deal.id)
    if documents_repo.est_nezakrytaya(db, deal.id, KIND_SALES_ORDER, OPEN_ORDER_STATUSES):
        raise errors.ConflictError(
            "This deal already has an open order", code="deal_order_exists"
        )
    stroki = [s for s in lines_repo.list_for_deal(db, deal.id) if s.product_id is not None]
    tovarnye = []
    for stroka in stroki:
        tovar = warehouse_repo.get_product(db, stroka.product_id)
        if tovar is not None and not tovar.is_service:
            tovarnye.append((stroka, tovar))
    if not tovarnye:
        raise errors.ValidationError(
            "The deal has no product lines to order", code="no_product_lines"
        )

    zakaz, _ = create(
        db,
        {"kind": KIND_SALES_ORDER, "client_id": deal.client_id, "deal_id": deal.id},
        author,
    )
    for stroka, _tovar in tovarnye:
        add_line(
            db,
            zakaz.id,
            {
                "product_id": stroka.product_id,
                "quantity": warehouse_service.format_quantity(stroka.quantity_milli),
                "price": stroka.price_minor,
            },
            author,
        )
    return zakaz


def prikrepit_k_zayavke(
    db: Session, document_id: int, deal_id: int | None, only_manager_id: int | None = None
) -> Document:
    """Прицепить заказ к заявке или отцепить (`deal_id=None`).

    Отцеплять надо: заказ цепляют не к той заявке так же часто, как и к той, а
    прицепленный заказ ПЕРЕНИМАЕТ её бронь — оставить чужую связь значит держать
    товар под чужим покупателем.
    """
    zakaz = get(db, document_id)
    if deal_id is None:
        zakaz.deal_id = None
    else:
        deal = deals_repo.get(db, deal_id)
        if deal is None:
            raise errors.NotFoundError("Deal not found", code="deal_not_found")
        if deal.closed_at is not None:
            raise errors.ValidationError("The deal is closed", code="deal_closed")
        # Область видимости спрашивается и здесь: прицепленный заказ ПЕРЕНИМАЕТ
        # бронь заявки, то есть менеджер «только со своими» иначе уводил бы
        # товар из-под чужого покупателя, ни разу не открыв его заявку.
        deal_service.ensure_visible(db, deal, only_manager_id)
        zakaz.deal_id = deal.id
    db.flush()
    return zakaz


def privyazat_klienta(db: Session, document_id: int, client_id: int | None) -> Document:
    """Привязать заказ к клиенту или отвязать (`client_id=None`) — пока открыт.

    Клиент у заказа необязателен: у стойки его часто негде взять. Но проведённый
    заказ записан — строки списаны, накладная выписана, — и менять, для кого
    это было, значит переписывать историю (владелец, 05.09.2026).
    """
    zakaz = get(db, document_id)
    _assert_open(zakaz)
    if client_id is not None and clients_repo.get(db, client_id) is None:
        raise errors.NotFoundError("Client not found", code="client_not_found")
    zakaz.client_id = client_id
    db.flush()
    return zakaz


def sobran_po_strokam(stroki) -> bool:
    """Собран ли заказ целиком: по каждой строке отмечено не меньше заказанного.

    Принимает СТРОКИ, а не номер заказа: и список, и карточка уже грузят их
    пачкой одним запросом, и второе определение «собранности» рядом с первым
    разошлось бы с ним при первой же правке.

    Считается по строкам, а не по статусу: статус «готов» ставит человек, а
    вопрос заявки физический — коробки собраны или нет. Пустой заказ собранным
    не считается: собирать нечего.
    """
    # Разовая позиция («упаковка») сканом не собирается — у неё нет карточки
    # товара. Считаем по товарным строкам: иначе заказ с одной такой строкой
    # не стал бы «собран» никогда (владелец, 05.09.2026).
    tovarnye = [s for s in stroki if s.product_id is not None]
    return bool(tovarnye) and all(s.picked_milli >= s.quantity_milli for s in tovarnye)


def otkrytye_po_zayavke(db: Session, deal_id: int) -> list[Document]:
    """Открытые заказы заявки — те, что ещё ждут отгрузки или приёмки."""
    zakazy, _ = documents_repo.search(db, deal_id=deal_id, kinds=ORDER_KINDS, page=1, per_page=200)
    return [z for z in zakazy if z.status in OPEN_ORDER_STATUSES]


def _title(kind: str) -> str:
    return "Sales order" if kind == KIND_SALES_ORDER else "Purchase order"


# --- клиент заказа ------------------------------------------------------------


def _resolve_client(db: Session, data: dict, author: User) -> tuple[int | None, bool]:
    """Найти клиента по номеру, почте или имени — или завести нового.

    Отвечает парой: `(client_id, завели ли карточку)`. **Молчаливых исходов нет
    ни одного**: заказ либо привязан к той карточке, на которую указали ВСЕ
    названные приметы, либо остановлен вопросом, либо заводит новую карточку — и
    в последнем случае говорит об этом вслух.

    Приметы делятся на два сорта, и в этом всё устройство:

    - **точные** — номер и почта. Совпадение по ним означает «это он»;
    - **имя** — подстрока, то есть догадка. «Иванов» находит Петра Иванова, у
      которого совсем другой телефон.

    Отсюда правило: **привязываем только к той карточке, на которую указывают и
    точная примета, и названный номер**.

    1. Ровно одна карточка совпала точными приметами, и названный номер указывает
       именно на неё — привязываем. Карточку при этом НЕ
       ПЕРЕЗАПИСЫВАЕМ: правило перенесено из `lead_service.receive` дословно —
       «подтянулись данные» и «затёрлись данные» обязаны быть разными
       действиями.
    2. Точных совпадений несколько — отказ `client_ambiguous` со списком
       кандидатов. Молча взять первого нельзя: `find_client_by_number` берёт
       того, кого правили позже, и для анонимной формы с сайта это терпимо, а
       для заказа у стойки означает, что деньги и товар уедут на чужую карточку.
    3. Карточка одна, но названный номер указывает не на неё — отказ
       `client_phone_mismatch`. Привязать значит выбросить номер, о котором
       человек сказал вслух; а номер, противоречащий карточке, — это либо другой
       человек, либо смена номера, и решает это человек, а не сервер.
    4. Совпало только ИМЯ — отказ `client_name_only`. Подстрока не примета:
       именно на этом исходе заказ и уезжал на чужую карточку.
    5. Не совпало ничего — заводим карточку.

    Отказы (2–4) снимаются вторым запросом: либо названным `client_id` — человек
    посмотрел кандидатов и выбрал, — либо `client_create_new`, «ни один из них не
    тот». Тот же приём, что `confirm_negative` у отгрузки: остановка и явное
    подтверждение вместо догадки сервера.

    **Имя новой карточки — номер, если имени не назвали.** Заказчик сказал
    дословно: «указать клиента можно по номеру или имени, если клиента не было он
    создается». Довод против («карточка с именем +380… нечитаема, и через месяц
    того же человека заведут второй раз») бьёт мимо: второй раз его не заведут
    как раз потому, что номер теперь лежит в карточке и находится по `phone_norm`
    — это проверено отдельным тестом. А молчаливое 201 с ничейным заказом теряло
    введённое без следа и без отказа, и это хуже неудобного имени, которое
    переименовывается одним полем.

    Назвали `client_id` — поиск не делается вовсе: человек уже выбрал.

    **Только у заказа покупателя.** Заказ поставщику адресован поставщику, а
    поставщик отдельной сущностью в системе не заведён: имя на такой бумаге —
    это «ООО Поставщик», и заводить под него КАРТОЧКУ КЛИЕНТА значит засорять
    справочник теми, кто ничего у нас не покупал.
    """
    if data.get("client_id"):
        # Проверку существования делает `document_service.create` через
        # `references.client`: второй такой проверкой мы бы завели второй ответ
        # на один вопрос.
        return data.get("client_id"), False
    if data.get("kind") != KIND_SALES_ORDER:
        return None, False

    name = (data.get("client_name") or "").strip()
    email = (data.get("client_email") or "").strip()
    phone = (data.get("client_phone") or "").strip()
    if not (name or email or phone):
        return None, False

    phone_norm = _phone_norm(db, phone)
    found = clients_repo.find_candidates(
        db, email=email, phone_norm=phone_norm, name=name, limit=MAX_CLIENT_CANDIDATES
    )
    # Совпавшие точной приметой стоят первыми (`find_candidates` сортирует именно
    # так), но разделяем их здесь ещё раз: порядок отвечает за то, что точное не
    # выпадет за предел выдачи, а этот отбор — за то, кого можно привязать.
    exact = [row for row in found if _matched_exactly(row, email, phone_norm)]
    say_new = bool(data.get("client_create_new"))

    if len(exact) == 1 and _phone_agrees(exact[0], phone_norm):
        return exact[0].id, False
    if not say_new:
        if len(exact) > 1:
            raise errors.ConflictError(
                "More than one client matches — say which one",
                code="client_ambiguous",
                details={"candidates": [_candidate(row) for row in exact]},
            )
        if exact:
            raise errors.ConflictError(
                "The phone number does not match this client — say which one",
                code="client_phone_mismatch",
                details={"candidates": [_candidate(row) for row in exact]},
            )
        if found:
            raise errors.ConflictError(
                "Only the name matches — say which client, or ask for a new one",
                code="client_name_only",
                details={"candidates": [_candidate(row) for row in found]},
            )

    client = client_service.create_client(
        db,
        {
            # Имени нет — карточку называет номер: он и есть то, чем человека
            # назвали. Пустого имени `create_client` не примет, а заказ без
            # карточки терял бы введённое без следа.
            "name": name or phone or email,
            "phone": phone,
            "email": email,
            "manager_id": author.id,
        },
        author,
    )
    # Журнал отвечает на вопрос «откуда взялся этот клиент» — вместо догадки.
    audit_service.record(
        db,
        action=audit_service.ACTION_CLIENT_CREATED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_CLIENT,
        entity_id=client.id,
        entity_label=client.name,
    )
    return client.id, True


def _matched_exactly(client, email: str, phone_norm: str) -> bool:
    """Совпал ли клиент ТОЧНОЙ приметой — номером или почтой.

    Имя сюда не входит и не войдёт: оно ищется подстрокой, и «Иванов» находит
    Петра Иванова с чужим телефоном. Подстрока отвечает на вопрос «кто похож», а
    привязка заказа задаёт другой — «кто это».
    """
    if phone_norm and (client.phone_norm or "") == phone_norm:
        return True
    return bool(email) and (client.email or "").strip().lower() == email.strip().lower()


def _phone_agrees(client, phone_norm: str) -> bool:
    """Указывает ли названный номер на эту карточку.

    Совпадения одной приметы мало: почта сошлась, номер — нет, и это либо другой
    человек, либо смена номера. Привязав по почте, мы бы выбросили номер, о
    котором человек сказал вслух, — молча и без следа, потому что найденную
    карточку мы не перезаписываем.

    **Решает здесь номер, а не почта, и это выбор.** У стойки человека опознают
    по телефону: его называют вслух, по нему звонят, по нему находит АТС. Почта в
    карточке живёт годами и устаревает молча, и требовать её совпадения значило
    бы останавливать обычный заказ постоянного покупателя, сменившего почту, —
    ради приметы, по которой ему никто не позвонит. Названная почта, не совпавшая
    с карточкой, поэтому НЕ повод для отказа: карточку она всё равно не
    перезаписывает.

    Номер не назвали — требовать нечего.
    """
    return not phone_norm or (client.phone_norm or "") == phone_norm


def _phone_norm(db: Session, phone: str) -> str:
    """Номер в сравнимом виде — той же функцией, что у клиентов и телефонии.

    Своей копии здесь нет намеренно: разойдясь с `client_service`, она давала бы
    заказ, не находящий карточку, которую по тому же номеру находит звонок.
    Ровно она же делает «067…» и «+380 67…» одним человеком — при заполненной
    настройке `default_country_code`, как и у звонка.
    """
    from core.services import settings_service

    code = settings_service.get_all(db).get("default_country_code", "")
    return normalize_phone(phone, code)[:32]


def _candidate(client) -> dict:
    """Кандидат для экрана «Кого из них?».

    Номер маскируем: список кандидатов приходит на запрос, где человек назвал
    ОДИН номер, и отдавать в ответ чужие целиком значит превратить форму заказа
    в справочник телефонов.
    """
    return {
        "id": client.id,
        "name": client.name,
        "company": client.company,
        "phone_masked": _mask(client.phone),
        "updated_at": client.updated_at.isoformat() if client.updated_at else None,
    }


def _mask(phone: str) -> str:
    value = (phone or "").strip()
    if len(value) <= 4:
        return value
    return "…" + value[-4:]


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
    _stroki_izmenilis(db, order, author)
    return line


def _stroki_izmenilis(db: Session, order: Document, author: User | None) -> None:
    """Сказать слушателям, что состав заказа другой. Кто слушает — не наше дело."""
    event_bus.emit(
        ORDER_LINES_CHANGED,
        db=db,
        actor=author,
        reason=f"order {order.number} lines changed",
        source_ref=order.number,
        order=order,
    )


def update_line(
    db: Session, document_id: int, line_id: int, data: dict, author: User | None = None
) -> DocumentLine:
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
    _stroki_izmenilis(db, order, author)
    return line


def remove_line(
    db: Session, document_id: int, line_id: int, author: User | None = None
) -> None:
    order = get(db, document_id)
    _assert_open(order)
    documents_repo.drop_line(db, _line(db, order.id, line_id))
    _stroki_izmenilis(db, order, author)


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

    Код без штрихкода пробуется как артикул: артикул печатается на наклейке
    текстом, и у товара без своего штрихкода это единственное, что можно
    набрать с коробки.
    """
    from core.services import barcode_service

    order = get(db, document_id)
    _assert_open(order)
    try:
        product = barcode_service.scan(db, code)
    except errors.NotFoundError:
        product = warehouse_repo.get_by_sku(db, code.strip()) or warehouse_repo.get_by_sku(
            db, code.strip().upper()
        )
        if product is None or product.deleted_at is not None:
            raise

    line = documents_repo.line_by_product(db, order.id, product.id)
    if line is None:
        raise errors.NotFoundError(
            f"{product.name} is not in this order", code="product_not_in_order"
        )
    line.picked_milli += quantity_milli
    db.flush()
    return line


# --- проведение ---------------------------------------------------------------


def _dvinut_sklad_naprjamuyu(db, order, goods, warehouse, outgoing, author) -> None:
    """Движения склада без накладной. Когда накладные выключены, а склад — нет.

    Это прежний путь заказа целиком, вынесенный отдельно, — чтобы в `close`
    было видно РАЗВИЛКУ, а не два перемешанных способа. Обе половины не могут
    сработать разом: развилка одна, и она по состоянию блока.

    Двойной отгрузки здесь быть не может: накладной по этому заказу не
    существует и завести её нечем — выключенный блок закрыт целиком, вместе
    с ручками.
    """
    for row in goods:
        warehouse_service.add_move(
            db,
            {
                "product_id": row.product_id,
                "kind": MOVE_OUT if outgoing else MOVE_IN,
                "quantity": _as_text(row.quantity_milli),
                "warehouse_id": warehouse.id,
                "deal_id": order.deal_id,
                "comment": f"{'shipped' if outgoing else 'received'} for order {order.number}",
                "document_id": order.id,
            },
            author,
        )


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

    # К остатку ведёт ровно один путь — вторая половина взаимного запрета.
    #
    # Заказ двигает склад здесь, накладная — при своём проведении. Пройди по
    # одному заказу оба, и товар уедет со склада дважды, а остаток покажет
    # минус, которого никто не объяснит. Первая половина запрета — в
    # `waybill_service._proverit_dvoynuyu_otgruzku`, там же и разбор, почему
    # пока запрет, а не переписывание этой функции на накладную.
    #
    # Проверка стоит ДО занятия замков и до смены статуса: отказать дешевле,
    # чем откатывать.
    otgruzheno = [w.number for w in otgruzheno_nakladnymi(db, order.id)]
    if otgruzheno:
        raise errors.ValidationError(
            "Stock has already moved by waybill " + ", ".join(otgruzheno)
            + "; closing the order would ship the goods twice",
            code="already_shipped_by_waybill",
        )

    # Услуги отбрасываем сразу: остатка у них нет, и в проверке нехватки они
    # всегда выглядели бы недостачей — «доставки на складе ноль». Отгрузка
    # заказа, где есть хоть одна услуга, вставала бы намертво.
    goods = [row for row in rows if row.product_id is not None and not _is_service(db, row)]
    outgoing = order.kind == KIND_SALES_ORDER

    # Выключенный склад не распоряжается закрытием ВООБЩЕ.
    #
    # Развилка ниже — по блоку НАКЛАДНЫХ, и проверка склада досталась только её
    # половине. Мимо шли не одни движения: `resolve_warehouse` отказывал
    # `no_warehouse`, а нехватка — остатком, которого никто не видит.
    sklad_vklyuchen = modules_service.is_enabled(db, "warehouse")
    warehouse = None
    if sklad_vklyuchen:
        # Склад выбирается явно, а не подставляется молча: списание с основного
        # однажды снимет деталь не оттуда, где её взяли. Не назвали — основной.
        warehouse = warehouse_service.resolve_warehouse(db, warehouse_id)

        # Занимаем товары ДО проверки нехватки. Устройство то же, что у
        # переезда: `_shortages` спрашивает остаток, движения пишутся ниже, и
        # между шагами окно. Замерено дуэлью — два заказа на последние две
        # единицы проходили оба, со склада уходило четыре, и подтверждения не
        # давал никто.
        #
        # Порядок по id обязателен: двое, берущие одни и те же товары в разном
        # порядке, встают друг против друга насмерть. Разбор замка — в докстроке
        # `warehouse_repo.zapert_tovar`.
        for row in sorted({row.product_id for row in goods}):
            warehouse_repo.zapert_tovar(db, row)

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

    # Себестоимость на строках ЗАКАЗА — снимок для его собственной карточки.
    # Накладная снимет свой при проведении; это не дублирование производного,
    # а два снимка одного числа в одно мгновение. Убери его — и карточка
    # заказа перестанет отвечать, во сколько обошлась отгрузка.
    #
    # При выключенном складе снимка НЕ ДЕЛАЕМ, а не выбрасываем его совсем:
    # включат склад обратно — и он снова снимается на каждом закрытии.
    if sklad_vklyuchen:
        for row in goods:
            product = warehouse_service.get_product(db, row.product_id, include_deleted=True)
            row.cost_minor = product.cost_minor

    if goods and modules_service.is_enabled(db, "waybills"):
        # К остатку ведёт ОДИН путь, и он через накладную.
        #
        # Прежде заказ двигал склад сам, прямым вызовом, и это стоило трёх
        # бед разом. Первая: два пути к одному остатку — по заказу можно было
        # выписать ещё и накладную, и товар уезжал дважды; держался запрет
        # взаимной проверкой в коде, а не устройством. Вторая: прямой вызов
        # шёл мимо `is_enabled(db, "warehouse")` — он никуда не делся, живёт
        # ниже и с 02.09.2026 стоит под `sklad_vklyuchen`. Третья: у
        # отгрузки не оставалось бумаги — на вопрос «по чему отдали» ответить
        # было нечем.
        #
        # Проведение накладной делает всё то же самое и в том же порядке:
        # замки по товарам, проверка нехватки, движения, снимок
        # себестоимости, — только через один вход и с бумагой на выходе.
        #
        # Статус заказа сменён ВЫШЕ, до этого: иначе двое, нажавшие «закрыть»
        # разом, успели бы выписать каждый свою накладную. Проигравший
        # спотыкается на `take_status`, и его транзакция откатывается целиком
        # — вместе с движениями склада, потому что транзакция одна.
        #
        # Склад может быть выключен — тогда его нет и у накладной: бумага
        # выписывается без него, а `provesti` не пишет ни одного движения.
        # Черновик по заказу уже есть — его и проводим: он и заведён ради
        # этого, а его количества кладовщик мог поправить руками («собрано
        # четыре»). Склад — тот, что выбрали при закрытии: черновик получил
        # основной, а отгружают с названного.
        nakladnaya = waybill_service.chernovik_po_zakazu(db, order.id)
        if nakladnaya is None:
            nakladnaya = waybill_service.po_zakazu(
                db, order.id, author, warehouse.id if warehouse else None
            )
        else:
            nakladnaya.warehouse_id = warehouse.id if warehouse else None
            db.flush()
        waybill_service.provesti(
            db,
            nakladnaya.id,
            author,
            confirm_negative=confirm_negative,
            po_zakrytiyu_zakaza=True,
        )
    elif sklad_vklyuchen:
        # Накладные выключены (или отгружать нечего — один услуги). Блок,
        # которого нет, не может выписать бумагу: правило «выключенный блок
        # исчезает целиком» сильнее желания единообразия. Двойной отгрузки
        # здесь быть не может по той же причине — накладной по этому заказу
        # не существует и завести её нечем.
        _dvinut_sklad_naprjamuyu(db, order, goods, warehouse, outgoing, author)

    # Деньги — здесь, событием, а не прямым вызовом финансов: заказы обязаны
    # работать при выключенном блоке денег, а `core/modules.py` связь
    # «orders → finance» не объявляет. Выключен блок — подписчика просто не
    # зовут, и отгрузка проходит целиком: статус, движения склада, журнал.
    #
    # Транзакция одна и открыта не здесь, а в `web/api/deps.py:get_db`: статус,
    # движения склада и денежные операции коммитятся вместе — либо всё, либо
    # ничего.
    event_bus.emit(
        ORDER_CLOSED,
        db=db,
        actor=author,
        reason=f"order {order.number} closed",
        source=SOURCE_MANUAL,
        # Чем именно вызвано — номером бумаги, а не номером записи: по журналу
        # ищут «двести двадцать третий», а не `document_id=417`.
        source_ref=order.number,
        order=order,
        lines=rows,
        from_status=previous,
    )

    # Молчание о невыполненном читается как «выполнено»: без этой строки
    # закрытие при выключенном складе выглядит в истории обычной отгрузкой.
    # Слова те же, что у накладной, — две истории обязаны читаться одинаково.
    _record(
        db, order, previous, STATUS_CLOSED, author,
        "" if sklad_vklyuchen else "warehouse module off, no stock moves",
    )
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


def cancel(db: Session, document_id: int, author: User, note: str = "") -> Document:
    """Отменить непроведённый заказ. Склада не касается — резерв снимется сам."""
    order = get(db, document_id)
    _assert_open(order)
    # Товар уже уехал накладной — «отменён» соврал бы про отгрузку. Такому
    # заказу путь один: он закрыт накладной, а откат — через сторно.
    uekhalo = [w.number for w in otgruzheno_nakladnymi(db, order.id)]
    if uekhalo:
        raise errors.ValidationError(
            "Stock has already moved by waybill " + ", ".join(uekhalo)
            + "; reverse the waybill instead of cancelling the order",
            code="already_shipped_by_waybill",
        )
    order = document_service.set_status(db, order.id, STATUS_CANCELLED, author, note)
    event_bus.emit(
        ORDER_CANCELLED,
        db=db,
        actor=author,
        reason=f"order {order.number} cancelled",
        source_ref=order.number,
        order=order,
    )
    return order


def mark_ready(db: Session, document_id: int, author: User) -> Document:
    """Собран и ждёт отгрузки. Резерв при этом держится: товар ещё наш."""
    order = get(db, document_id)
    if order.status != STATUS_ISSUED:
        raise errors.ValidationError("Only a new order can be marked ready", code="order_not_new")
    return document_service.set_status(db, order.id, STATUS_READY, author)


# --- мелочи -------------------------------------------------------------------


def otgruzheno_nakladnymi(db: Session, order_id: int) -> list[Document]:
    """Проведённые накладные заказа, не снятые проведённым сторно.

    Сторно — обратная бумага по основанию накладной; проведённое сторно значит
    «товар вернулся», и исходная накладная больше не считается отгрузкой:
    иначе заказ после возврата нельзя было бы ни закрыть, ни откатить.
    """
    itog = []
    for nakladnaya in documents_repo.po_osnovaniyu(db, order_id):
        if nakladnaya.status not in (STATUS_ISSUED, STATUS_CLOSED):
            continue
        snyato = any(
            storno.status in (STATUS_ISSUED, STATUS_CLOSED)
            for storno in documents_repo.po_osnovaniyu(db, nakladnaya.id)
        )
        if not snyato:
            itog.append(nakladnaya)
    return itog


def zakryt_po_nakladnoy(db: Session, order: Document, waybill: Document, author: User) -> None:
    """Проведённая руками накладная закрывает заказ.

    Раньше заказ оставался открытым и закрыть его было нельзя вовсе
    (`already_shipped_by_waybill`): товар уехал, а бумага висела «принято»
    навсегда. Владелец 05.09.2026 попросил, чтобы статус менялся во всех
    блоках; это отменяет отказ в docs/21 §3. Склад не трогаем — его двинула
    накладная; остальное (деньги, уведомления, лента) — как у закрытия.
    """
    if order.status not in OPEN_ORDER_STATUSES:
        return
    rows = documents_repo.lines_of(db, order.id)
    # Частичная отгрузка («привезли половину») заказ не закрывает: он открыт,
    # пока по каждому товару накладные не покрыли заказанное. Остаток при этом
    # честно остаётся в резерве (`reserve_service`).
    nuzhno: dict[int, int] = {}
    for row in rows:
        if row.product_id is not None and not _is_service(db, row):
            nuzhno[row.product_id] = nuzhno.get(row.product_id, 0) + row.quantity_milli
    uekhalo: dict[int, int] = {}
    for nakladnaya in otgruzheno_nakladnymi(db, order.id):
        for row in documents_repo.lines_of(db, nakladnaya.id):
            if row.product_id is not None:
                uekhalo[row.product_id] = uekhalo.get(row.product_id, 0) + row.quantity_milli
    if any(uekhalo.get(product_id, 0) < skolko for product_id, skolko in nuzhno.items()):
        return
    previous = order.status
    if not documents_repo.take_status(db, order, expected=previous, status=STATUS_CLOSED):
        raise errors.ConflictError(
            "The order has already been processed by someone else",
            code="document_status_changed",
        )
    if modules_service.is_enabled(db, "warehouse"):
        for row in rows:
            if row.product_id is not None and not _is_service(db, row):
                product = warehouse_service.get_product(db, row.product_id, include_deleted=True)
                row.cost_minor = product.cost_minor
    event_bus.emit(
        ORDER_CLOSED,
        db=db,
        actor=author,
        reason=f"order {order.number} closed by waybill {waybill.number}",
        source=SOURCE_MANUAL,
        source_ref=waybill.number,
        order=order,
        lines=rows,
        from_status=previous,
    )
    _record(db, order, previous, STATUS_CLOSED, author, f"shipped by waybill {waybill.number}")
    audit_service.record(
        db,
        action=audit_service.ACTION_ORDER_CLOSED,
        actor=author,
        source=SOURCE_MANUAL,
        source_ref=waybill.number,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=order.id,
        entity_label=order.number,
        before=previous,
        after=STATUS_CLOSED,
    )


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
