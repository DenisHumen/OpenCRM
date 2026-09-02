from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import Client, PhoneCall
from database.query import contains, page_of
from database.session import tochka_otkata


def get(db: Session, call_id: int) -> PhoneCall | None:
    return db.get(PhoneCall, call_id)


def insert_or_take(db: Session, call: PhoneCall) -> PhoneCall:
    """Вставить звонок, а если его только что вставил кто-то другой — взять тот.

    Четвёртый ответ на проигранную гонку, которого нет в `core/uniqueness.py`:
    там проигравший либо узнаёт «занято», либо берёт следующее свободное, либо
    пожимает плечами. Здесь ему нужна **именно та строка**, которую вставил
    победитель, — и найти её можно только по своему ключу, общего вида у такого
    поиска нет.

    Зачем: события одного звонка (начался → ответили → завершился) уходят
    подряд, а при разрыве связи АТС повторяет пачку. Два запроса читают «нет
    такого» одновременно, оба вставляют — и проигравший получал 500. Данные при
    этом оставались целы (уникальный индекс своё дело делал), но станция видела
    ошибку, считала событие недоставленным и слала его снова, раскручивая петлю
    повторов на ровном месте.

    База писателей не сериализует: два запроса идут одновременно
    по-настоящему, и эта гонка здесь обычное дело, а не редкость.
    """
    try:
        with tochka_otkata(db):
            db.add(call)
            db.flush()
        return call
    except IntegrityError:
        # Откат точки обычно сам выбрасывает добавленное в ней из сессии, но
        # полагаться на это нельзя: останься объект ожидающим вставки — следующий
        # flush повторил бы её и упал бы уже без спасения.
        if call in db:
            db.expunge(call)
        existing = get_by_external_id(db, call.external_id)
        if existing is None:
            # Уникальность нарушена не по external_id — значит дело не в гонке,
            # а в чём-то другом, и прятать такую ошибку нельзя.
            raise
        return existing


def insert_new(db: Session, call: PhoneCall) -> PhoneCall | None:
    """Вставить звонок или вернуть None, если такой ``external_id`` уже занят.

    Противоположность `insert_or_take` — и разница между ними не техническая, а
    смысловая. Там строку победителя берут потому, что это **тот же самый
    звонок**: события одного разговора приходят от одной станции с одним ключом.
    Здесь звонок только что родился по нажатию кнопки, и строка с таким же
    ключом в базе — заведомо **другой**, более старый разговор: свой ключ мы
    видим впервые. Присвоить её значит переписать чужие номера и оператора,
    оставив звонок висеть в карточке чужого клиента.

    Поэтому «уже занято» здесь не гонка, а отказ, и решает, что с ним делать,
    вызывающий.
    """
    try:
        with tochka_otkata(db):
            db.add(call)
            db.flush()
        return call
    except IntegrityError:
        # Как и в `insert_or_take`: объект надо выбросить из сессии руками,
        # иначе следующий flush повторит вставку уже без точки отката.
        if call in db:
            db.expunge(call)
        if get_by_external_id(db, call.external_id) is None:
            # Уникальность нарушена не по external_id — дело не в занятом ключе,
            # и прятать такую ошибку нельзя.
            raise
        return None


def get_by_external_id(db: Session, external_id: str) -> PhoneCall | None:
    """Звонок по идентификатору АТС — точка склейки повторных событий."""
    return db.scalar(select(PhoneCall).where(PhoneCall.external_id == external_id))


def vzyat_pod_pravku(db: Session, external_id: str) -> PhoneCall | None:
    """То же, но строка запирается до конца транзакции.

    Нужно ровно там, где событие станции ПРАВИТ уже существующий звонок.
    Станция повторяет пачку при разрыве связи — об этом сказано в докстроке
    обработчика, — и два события об одном разговоре приходят разом.

    Без замка получалось так: оба читают строку, оба видят `note_id = NULL`,
    оба доходят до записи в ленту. Проигравший встаёт на блокировке при
    `flush()` своего `UPDATE`, дожидается чужого коммита — и идёт дальше СО
    СВОИМ УСТАРЕВШИМ объектом: SQLAlchemy не перечитывает уже загруженную
    строку после снятия блокировки. В памяти `note_id` по-прежнему `None`,
    значит вторая запись в ленте появляется, а `call.note_id` переписывается
    на неё. В ленте клиента два входа об одном разговоре.

    `FOR UPDATE` эту дыру закрывает целиком: под READ COMMITTED он ждёт чужого
    коммита ДО чтения и отдаёт свежую версию строки — с уже проставленным
    `note_id`.

    Отдельной функцией, а не общим ужесточением `get_by_external_id`: тот же
    запрос зовётся из `insert_or_take` для проверки «а не появилась ли строка»,
    и запирать там нечего — строку только что вставил кто-то другой, и читаем
    мы её, чтобы отдать наверх, а не чтобы править.
    """
    return db.scalar(
        select(PhoneCall).where(PhoneCall.external_id == external_id).with_for_update()
    )


def find_client_by_number(db: Session, number_norm: str) -> Client | None:
    """Клиент по нормализованному номеру.

    Удалённые карточки не считаются: клиент в корзине не должен молча
    получать новые звонки в ленту. Если номер записан у двоих — берём того,
    кого правили позже: гадать всё равно не из чего, а свежая карточка
    вероятнее актуальна.
    """
    if not number_norm:
        return None
    return db.scalars(
        select(Client)
        .where(Client.phone_norm == number_norm, Client.deleted_at.is_(None))
        .order_by(Client.updated_at.desc(), Client.id.desc())
        .limit(1)
    ).first()


def list_calls(
    db: Session,
    direction: str | None = None,
    outcome: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    user_id: int | None = None,
    number: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[PhoneCall], int]:
    stmt = select(PhoneCall)
    if direction:
        stmt = stmt.where(PhoneCall.direction == direction)
    if outcome:
        stmt = stmt.where(PhoneCall.outcome == outcome)
    if client_id is not None:
        stmt = stmt.where(PhoneCall.client_id == client_id)
    if deal_id is not None:
        stmt = stmt.where(PhoneCall.deal_id == deal_id)
    if user_id is not None:
        stmt = stmt.where(PhoneCall.user_id == user_id)
    if number:
        # Ищем по нормализованному номеру: в поле поиска набирают как придётся,
        # а в базе лежит единый вид — сравнивать надо одинаково приведённые.
        stmt = stmt.where(
            or_(
                contains(PhoneCall.from_number_norm, number),
                contains(PhoneCall.to_number_norm, number),
            )
        )
    if since is not None:
        stmt = stmt.where(PhoneCall.started_at >= since)
    if until is not None:
        stmt = stmt.where(PhoneCall.started_at <= until)
    stmt = stmt.order_by(PhoneCall.started_at.desc(), PhoneCall.id.desc())
    return page_of(db, stmt, page=page, per_page=per_page)
