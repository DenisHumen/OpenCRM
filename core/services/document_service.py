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
from core.services import company_service, settings_service
from core.utils import now_utc
from database.models import Client, Company, Deal, Document, DocumentEvent, User
from database.models.document import (
    DOCUMENT_KINDS,
    DOCUMENT_LOCALES,
    DOCUMENT_STATUSES,
    KIND_INTAKE,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_ISSUED,
)
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo

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


def _insert_with_free_number(db: Session, **fields) -> Document:
    """Вставить бланк, заняв следующий свободный номер.

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
        lambda: Document(number=next_number(db), status=STATUS_ISSUED, **fields),
        taken=lambda row: documents_repo.number_exists(db, row.number),
        message="Could not take a free number, try again",
        code="document_number_taken",
    )


def create(db: Session, data: dict, author: User) -> Document:
    kind = data.get("kind") or KIND_INTAKE
    if kind not in DOCUMENT_KINDS:
        raise errors.ValidationError(f"Unknown document kind: {kind}", code="unknown_kind")

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


def search(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Document], int]:
    return documents_repo.search(
        db, q=q, status=status, client_id=client_id, deal_id=deal_id, page=page, per_page=per_page
    )


def payload_of(document: Document) -> dict:
    try:
        return json.loads(document.payload or "{}")
    except ValueError:
        # Битый снимок не должен ронять печать всего бланка: показываем что есть.
        return {}
