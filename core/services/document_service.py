"""Бланки: приём вещи в работу, печать в двух экземплярах, поиск сканом."""

import json

from sqlalchemy.orm import Session

# Под псевдонимом: ниже в этом же модуле есть функция `events()` — история
# бланка для его экрана. Импорт под собственным именем она бы перекрыла, причём
# молча и только в момент вызова.
from core import events as event_bus
from core import exceptions as errors
from core import references
from core import uniqueness
from core.services import audit_service, company_service, settings_service
from core.utils import now_utc
from database.models import Client, Company, Deal, Document, DocumentEvent, User
from database.models.audit import SOURCE_MANUAL
from database.models.document import (
    DOCUMENT_KINDS,
    DOCUMENT_LOCALES,
    DOCUMENT_STATUSES,
    KIND_INTAKE,
    ORDER_KINDS,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_ISSUED,
    WAYBILL_KINDS,
    statuses_for,
)
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo
from database.repositories import finance as finance_repo
from database.repositories import warehouse as warehouse_repo

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

#: Бланк выпущен — бумага ушла клиенту. Подробности: `document`.
DOCUMENT_ISSUED = "document.issued"

#: Бланк дошёл до конца: вещь отдали или бумагу аннулировали. Подробности:
#: `document`, `from_status`, `to_status`.
#:
#: Промежуточные состояния («в работе», «готово») событием не объявляются
#: намеренно. Они и так лежат в `document_events` и рисуются на экране бланка,
#: а в ленте заявки превратились бы в четыре строки об одной бумаге вместо
#: двух. В ленту идёт то, что человек назвал бы событием ПО ЗАЯВКЕ: выдали
#: квитанцию, закрыли квитанцию. Путь бумаги по состояниям — её собственное дело.
DOCUMENT_CLOSED = "document.closed"

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


def _company_snapshot(company: Company | None, site: dict) -> dict:
    """Реквизиты фирмы в том виде, в каком они уходят на бумагу.

    Это самое важное место во всём бланке. Реквизиты обязаны попасть в снимок
    именно СЕЙЧАС, при выпуске, а не подтягиваться на печать: фирма сменит банк
    или счёт — и старый акт, перепечатанный через полгода, покажет новый счёт
    там, где у клиента на руках лежит бумага со старым. Спорить после этого не
    о чем: обе стороны держат «оригинал», и они не совпадают.

    Фирмы может не быть — справочник не заполнен или блок выключен. Тогда
    падаем на настройки сайта, как было до появления юрлиц: бланк без вывески
    хуже, чем бланк без реквизитов.
    """
    if company is not None:
        snapshot = company_service.requisites(company)
        # Телефон и почта фирмы бывают не заполнены, а в шапке они нужны:
        # клиент звонит по тому номеру, что напечатан. Добираем из настроек
        # сайта — это те же контакты, просто записанные в другом месте.
        snapshot["phone"] = snapshot.get("phone") or site.get("contact_phone") or ""
        snapshot["email"] = snapshot.get("email") or site.get("contact_email") or ""
        return snapshot
    return {
        "name": site.get("brand_name") or "",
        "phone": site.get("contact_phone") or "",
        "email": site.get("contact_email") or "",
    }


def _payload(
    data: dict,
    client: Client | None,
    deal: Deal | None,
    site: dict,
    company: Company | None,
) -> dict:
    """Снимок того, что напечатано.

    Ссылками не обойтись: у человека на руках бумага, и она обязана совпадать с
    записью в базе, даже если клиента потом переименовали, телефон поправили, а
    сделку удалили. Иначе спор «что вы у меня приняли» решать нечем.
    """
    return {
        "company": _company_snapshot(company, site),
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
    last = documents_repo.max_number(db, prefix)
    counter = int(last.split("-")[1]) + 1 if last else 1
    return f"{prefix}{counter:06d}"


def _insert_with_free_number(db: Session, status: str = STATUS_ISSUED, **fields) -> Document:
    """Вставить бланк, заняв следующий свободный номер.

    `status` — параметром, а не жёстко «выдан». До накладной все виды бумаги
    рождались выданными, и разницы не было; накладная рождается ЧЕРНОВИКОМ, её
    собирают по складу со сканером. Номер она при этом занимает сразу, вместе с
    черновиком, и это решение: кладовщик называет номер по рации («грузим по
    сто восемнадцатой») задолго до того, как бумага проведена, а номер, выданный
    при проведении, до этого момента не существовал бы.

    Цена решения — дыры в нумерации от брошенных черновиков. Она принята
    сознательно: сквозной номер здесь нужен для РАЗГОВОРА, а не для отчётности
    перед кем-то, кто спросит, куда делся сто семнадцатый.

    Номер считается как «максимум по году плюс один», и между счётом и вставкой
    есть окно. У стойки в него попадают: двое приёмщиков выдают бумагу
    одновременно, оба видят один и тот же максимум. Уникальный индекс данные
    спасал — двух бланков с одним номером не появлялось, — но проигравший
    получал 500. Проверено живым прогоном: из двадцати одновременных выдач
    тринадцать отвечали ошибкой сервера. Приёмщик при этом жмёт «выдать» снова,
    попадает в ту же гонку и решает, что сломалась программа.

    Номер считается заново перед каждой попыткой — уже с учётом того, кто успел
    раньше. Общий приём и разбор трёх видов «проиграл» — в `core/uniqueness.py`.
    """
    return uniqueness.insert_retrying(
        db,
        lambda: Document(number=next_number(db), status=status, **fields),
        taken=lambda row: documents_repo.number_exists(db, row.number),
        message="Could not take a free number, try again",
        code="document_number_taken",
    )


def create(db: Session, data: dict, author: User) -> Document:
    kind = data.get("kind") or KIND_INTAKE
    if kind not in DOCUMENT_KINDS:
        raise errors.ValidationError(f"Unknown document kind: {kind}", code="unknown_kind")
    # Накладная заводится СВОИМ путём, и здесь ей отказ.
    #
    # **Только накладная, хотя склад двигают и заказы.** Заказ приходит сюда
    # законно: `order_service.create` дособирает поля и зовёт эту же функцию —
    # бумага у них общая, и второй раз писать выдачу номера незачем. Накладная
    # так не делает: у неё свой статус при рождении (черновик), свой склад и
    # своё основание, и `_insert_with_free_number` она зовёт сама.
    #
    # Первая версия этой проверки отвергала `SKLADSKIE_KINDS` целиком — то есть
    # и заказы, — и создание заказов перестало работать вовсе. Поймано набором
    # тестов заказов, а не глазами: разница между «двигает склад» и «заводится
    # своим путём» на вид невелика, а последствия у неё разные.
    if kind in WAYBILL_KINDS:
        raise errors.ValidationError(
            f"{kind} has its own creation path", code="kind_has_own_path"
        )

    locale = data.get("locale") or "ru"
    if locale not in DOCUMENT_LOCALES:
        raise errors.ValidationError(f"Unknown locale: {locale}", code="unknown_locale")

    # Указанное должно существовать. Раньше несуществующий клиент превращался в
    # None и уезжал в общую проверку ниже — человек получал «укажите клиента» на
    # запрос, где клиент как раз указан, просто такого нет. Искать причину по
    # такому ответу невозможно: он говорит не о том, что случилось.
    client_id = references.client(db, data.get("client_id"))
    deal_id = references.deal(db, data.get("deal_id"))
    client = clients_repo.get(db, client_id) if client_id else None
    deal = deals_repo.get(db, deal_id) if deal_id else None
    if deal is not None and client is None:
        client = clients_repo.get(db, deal.client_id) if deal.client_id else None
    if client is None and not (data.get("client_name") or "").strip():
        # А вот это — настоящий случай «не назвали никого»: бланк прохожему без
        # карточки законен, но имя на бумаге должно стоять хоть какое-то.
        raise errors.ValidationError(
            "Document needs a client or at least a name", code="client_required"
        )
    if not (data.get("item") or "").strip():
        raise errors.ValidationError("Item is required", code="item_required")

    # От чьего имени выдаём. Выбранная руками фирма важнее фирмы заявки: бланк
    # печатают у стойки, и там иногда виднее, чем при заведении заявки.
    company = company_service.for_document(db, data.get("company_id"), deal)

    payload = json.dumps(
        _payload(data, client, deal, settings_service.get_all(db), company),
        ensure_ascii=False,
    )
    document = _insert_with_free_number(
        db,
        kind=kind,
        locale=locale,
        client_id=client.id if client else None,
        deal_id=deal.id if deal else None,
        payload=payload,
        created_by=author.id,
    )
    documents_repo.add_event(
        db,
        DocumentEvent(
            document_id=document.id, from_status="", to_status=STATUS_ISSUED,
            author_id=author.id,
        ),
    )
    # Событие поднимается из сервиса, а не из роута: путь сюда сегодня один, но
    # завтра бланк начнут выпускать пачкой или из скрипта, и подписчик не должен
    # зависеть от того, каким путём пришли. Ровно как у смены этапа.
    event_bus.emit(
        DOCUMENT_ISSUED,
        db=db,
        actor=author,
        # Причина у выпуска одна: бумагу печатают, чтобы отдать её человеку.
        # Разнообразия здесь взять негде, и выдумывать его — значит написать в
        # ленте что-то, чего никто не выбирал.
        reason="handed to the client",
        document=document,
    )
    return document


def get(db: Session, document_id: int) -> Document:
    document = documents_repo.get(db, document_id)
    if document is None:
        raise errors.NotFoundError("Document not found", code="document_not_found")
    return document


def tolko_blank(db: Session, document_id: int) -> Document:
    """Бланк по номеру записи. Заказ — отказ, и это не придирка.

    **Заказы и бланки живут в ОДНОЙ таблице** (`documents`, `kind`), и ручки
    бланков брали запись по номеру, ни о чём не спрашивая. То есть `POST
    /documents/{id}/status` с правом `documents.issue` закрывал ЗАКАЗ: статус
    менялся на «выдан», история перехода писалась, а склад не двигался вовсе.
    Заказ отгружается своим путём (`orders.issue`), где считается нехватка,
    списываются остатки и снимается резерв, — здесь ничего этого нет.

    Получалось две вещи разом: обход права `orders.issue` правом
    `documents.issue` и заказ, закрытый без единого движения по складу. Второе
    хуже: остаток остаётся на месте, товар уезжает, и расхождение всплывает на
    инвентаризации через месяц, когда концов уже не найти.

    Внутренние вызовы это не задевает: `order_service` зовёт `set_status` сам и
    напрямую — правило стоит на ГРАНИЦЕ, у ручек бланков, а не в общей смене
    статуса, которой пользуются оба.
    """
    document = get(db, document_id)
    if document.kind in ORDER_KINDS:
        raise errors.ValidationError(
            "This is an order, not a document. Use the orders section.",
            code="document_is_an_order",
        )
    # Накладная попала сюда сразу, а не после такого же случая, как заказ.
    # Довод дословно тот же: общая смена статуса меняет статус и пишет переход,
    # а склада не касается. Пропусти она накладную — бумага закрылась бы, товар
    # остался бы на полке, и расхождение всплыло бы на инвентаризации через
    # месяц. Проводится накладная своим путём (`waybill_service.provesti`), где
    # считается нехватка и пишутся движения.
    if document.kind in WAYBILL_KINDS:
        raise errors.ValidationError(
            "This is a waybill, not a document. Use the waybills section.",
            code="document_is_a_waybill",
        )
    return document


def udalit(db: Session, document_id: int, author: User, kinds) -> dict:
    """Удалить бумагу, заведённую по ошибке, пока она ничего не сделала.

    Правило «бланк не удаляется» смягчено владельцем 05.09.2026: не удаляется
    бумага, которая ЖИЛА — двигала склад, принимала деньги, стала основанием
    накладной или была проведена. Заведённая случайно и нетронутая висела бы в
    списке вечно, и отмена этого не лечит: отменённая остаётся в списке.
    Дырку в нумерации объясняет журнал: номер и вид записаны в `document.deleted`.
    """
    document = get(db, document_id)
    if document.kind not in kinds:
        raise errors.ValidationError(
            "This paper belongs to another section", code="document_wrong_section"
        )
    if document.status == STATUS_CLOSED or (
        document.kind in WAYBILL_KINDS and document.status not in (STATUS_DRAFT, STATUS_CANCELLED)
    ):
        raise errors.ValidationError(
            f"{document.number} has been carried out and cannot be deleted",
            code="document_in_use",
        )
    prichiny = []
    if warehouse_repo.moves_of_document(db, document.id):
        prichiny.append("stock moves")
    if finance_repo.operations_of_document(db, document.id):
        prichiny.append("money operations")
    # Черновик накладной по заказу уходит вместе с ним: он повторяет заказ и без
    # него не значит ничего. Проведённая — держит заказ: товар по ней уехал.
    chernoviki = []
    for waybill in documents_repo.po_osnovaniyu(db, document.id):
        if waybill.status == STATUS_DRAFT:
            chernoviki.append(waybill)
        else:
            prichiny.append("waybills based on it")
            break
    if prichiny:
        raise errors.ValidationError(
            f"{document.number} is in use: " + ", ".join(prichiny), code="document_in_use"
        )
    number, kind = document.number, document.kind
    for waybill in chernoviki:
        documents_repo.drop(db, waybill)
    documents_repo.drop(db, document)
    audit_service.record(
        db,
        action=audit_service.ACTION_DOCUMENT_DELETED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=document_id,
        entity_label=number,
        before=kind,
        after="deleted",
    )
    return {"id": document_id, "number": number, "deleted": True}


def by_number(db: Session, number: str) -> Document:
    """Поиск сканом. Номер приходит со штрихкода или из адреса в QR."""
    document = documents_repo.get_by_number(db, number)
    if document is None:
        raise errors.NotFoundError("Document not found", code="document_not_found")
    return document


def set_status(db: Session, document_id: int, status: str, author: User, note: str = "") -> Document:
    if status not in DOCUMENT_STATUSES:
        raise errors.ValidationError(f"Unknown status: {status}", code="unknown_status")

    document = get(db, document_id)
    # Набор статусов у каждого вида свой, и до появления накладной это было
    # только описанием: словарь `KIND_STATUSES` стоял в модели и не читался
    # НИКЕМ, а проверка выше сверялась со всеми статусами разом. Пока наборы
    # совпадали, разницы не было.
    #
    # С черновиком она появилась: `draft` попал в общий набор, и без этой
    # проверки квитанцию можно было бы отправить в черновик — состояние, для неё
    # бессмысленное, ничем не предусмотренное и не имеющее обратного пути.
    if status not in statuses_for(document.kind):
        raise errors.ValidationError(
            f"Status {status} does not apply to {document.kind}",
            code="status_not_for_kind",
        )
    if document.status in (STATUS_CLOSED, STATUS_CANCELLED) and status != document.status:
        # Закрытый бланк — уже отданная вещь. Открывать его заново нельзя:
        # иначе история перестаёт отвечать на вопрос «когда клиент забрал».
        raise errors.ValidationError(
            "This document is already finished", code="document_finished"
        )
    if status == document.status:
        return document

    previous = document.status
    # Статус меняем условием «пока он тот, что мы прочитали», как этап заявки, и
    # ровно по той же причине: у бланка есть своя история переходов, и двое,
    # нажавшие «готово» и «выдано» разом, оставили бы в ней два перехода из
    # одного состояния. Данные целы, а история перестаёт отвечать на вопрос
    # «когда клиент забрал» — тот единственный, ради которого её и ведут.
    if not documents_repo.take_status(db, document, expected=previous, status=status):
        raise errors.ConflictError(
            "The document status has already been changed by someone else",
            code="document_status_changed",
        )
    comment = (note or "").strip()[:MAX_NOTE]
    documents_repo.add_event(
        db,
        DocumentEvent(
            document_id=document.id,
            from_status=previous,
            to_status=status,
            note=comment,
            author_id=author.id,
        ),
    )
    if status in (STATUS_CLOSED, STATUS_CANCELLED):
        event_bus.emit(
            DOCUMENT_CLOSED,
            db=db,
            actor=author,
            # Приписка оператора и есть причина: «клиент забрал 12.08» объясняет
            # закрытие лучше любой формулировки от кода. Не написал — ставим
            # общую: пустую причину лента показывать не должна.
            reason=comment
            or ("voided at the desk" if status == STATUS_CANCELLED else "the item was handed over"),
            document=document,
            from_status=previous,
            to_status=status,
        )
    return document


def events(db: Session, document_id: int) -> list[DocumentEvent]:
    return documents_repo.events(db, document_id)


def lines(db: Session, document_id: int) -> list:
    """Позиции бланка — без оглядки на его вид.

    У заказа и у акта есть свои входы (`order_service.lines`, `act_service.lines`),
    и оба сначала проверяют вид бумаги: отгружать квитанцию нечем, проводить
    заказ как акт нельзя. Этот вход отвечает на вопрос попроще — «по какой сумме
    эта бумага», — и задают его те, кому вид безразличен: врезка «Деньги»
    считает остаток к оплате одинаково у заказа и у акта.
    """
    return documents_repo.lines_of(db, document_id)


def search(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
    sort: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Document], int]:
    return documents_repo.search(
        db,
        q=q,
        status=status,
        client_id=client_id,
        deal_id=deal_id,
        kinds=kinds,
        sort=sort,
        page=page,
        per_page=per_page,
    )


def schyot_po_vidam(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    sredi: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Сколько бумаг каждого вида при этом отборе — для заголовков категорий."""
    return documents_repo.schyot_po_vidam(
        db, q=q, status=status, client_id=client_id, deal_id=deal_id, sredi=sredi
    )


def total_minor(rows: list) -> int:
    """Сумма строк бланка в минорных единицах.

    Живёт здесь, а не у заказа, потому что строки (`document_lines`) — общие:
    ими пользуются и заказ, и акт. Свой счёт у каждого означал бы **два места,
    где считаются деньги**, — ровно то, от чего предостерегает докстрока модели
    `DocumentLine`: разойдутся они сначала в округлении, потом в скидках, потом
    в налоге, и какая из двух сумм верна, спросить будет не у кого.

    Считаем целыми: количество в тысячных, цена в минорных — произведение делим
    на тысячу **один раз в конце**, с округлением к ближайшему. Делить на каждой
    строке значит копить ошибку округления по числу позиций.
    """
    return _okruglit(sum(row.quantity_milli * (row.price_minor or 0) for row in rows))


def _okruglit(scaled: int) -> int:
    """Тысячные в минорные единицы, к ближайшему, симметрично около нуля."""
    return (scaled + 500) // 1000 if scaled >= 0 else -((-scaled + 500) // 1000)


def line_totals(rows: list) -> list[int]:
    """Суммы строк, которые СКЛАДЫВАЮТСЯ В ИТОГ. По одной на строку.

    Нужно ровно для печати: на бумаге стоит колонка «Сумма» и под ней «Итого»,
    и заказчик их складывает. До этой функции колонка считалась вызовом
    `total_minor` по ОДНОЙ строке, то есть с округлением на каждой, а итог — по
    всем сразу, с округлением один раз. Складывалось это не всегда.

    Пример из двух строк: количество 0,5 и цена 12 345 у каждой. Строка:
    500 × 12 345 = 6 172 500 → 6173 → «61.73». Итог: 12 345 000 → 12345 →
    «123.45». На подписываемом листе 61.73 + 61.73 = 123.46 против «Итого
    123.45».

    Чинить это переносом округления на строки НЕЛЬЗЯ: правило «округлять на
    итоге, а не на каждой строке» записано в `total_minor` и в
    `finance_service` про налог, и держится оно затем, чтобы ошибка не копилась
    по числу позиций. Итог остаётся прежним — сходиться обязаны строки.

    Отсюда счёт нарастающим итогом: сумма строки — это округлённый итог ПО
    НЕЁ ВКЛЮЧИТЕЛЬНО минус округлённый итог по предыдущую. Тогда сумма всех
    строк равна округлённому итогу тождественно, а каждая строка отличается от
    своего собственного округления не больше чем на минорную единицу.

    Заодно исчезает вторая беда: у заказа была своя копия этого счёта
    (`_line_sum`) без ветки для отрицательных, и строка со скидочной ценой
    округлялась в разные стороны в колонке и в итоге.
    """
    itogi = []
    nakoplennoe = 0
    bylo = 0
    for row in rows:
        nakoplennoe += row.quantity_milli * (row.price_minor or 0)
        stalo = _okruglit(nakoplennoe)
        itogi.append(stalo - bylo)
        bylo = stalo
    return itogi


def payload_of(document: Document) -> dict:
    try:
        return json.loads(document.payload or "{}")
    except ValueError:
        # Битый снимок не должен ронять печать всего бланка: показываем что есть.
        return {}
