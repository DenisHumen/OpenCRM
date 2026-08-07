from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Client, MailAccount, MailMessage
from database.query import contains, page_of


def list_accounts(db: Session) -> list[MailAccount]:
    return list(db.scalars(select(MailAccount).order_by(MailAccount.id)))


def get_account(db: Session, account_id: int) -> MailAccount | None:
    return db.get(MailAccount, account_id)


def first_active_account(db: Session) -> MailAccount | None:
    """Ящик, из которого уходит письмо, если отправитель не выбран явно."""
    return db.scalar(
        select(MailAccount).where(MailAccount.is_active.is_(True)).order_by(MailAccount.id).limit(1)
    )


def get_message(db: Session, message_id: int) -> MailMessage | None:
    return db.get(MailMessage, message_id)


def find_by_message_id(db: Session, account_id: int, message_id: str) -> MailMessage | None:
    """Ключ идемпотентности синхронизации: это письмо в ЭТОМ ящике уже есть?

    Пара (ящик, Message-ID), а не один Message-ID. Письмо, доставленное сразу на
    два ящика фирмы, — это две записи: у каждого ящика своё зеркало и свой
    `last_uid`, и вторая копия не «дубль», а единственное, из чего у второго
    ящика вообще складывается `max(uid)`. Пока ключ был глобальным, второй ящик
    не сохранял такое письмо, оставался без строк и тянул себя с начала каждую
    синхронизацию.

    Задвоение ленты клиента этим не возвращается: запись ленты порождает только
    письмо, попавшее в базу впервые ДЛЯ СВОЕГО ящика, а два ящика — это и правда
    два письма, полученных фирмой.
    """
    return db.scalar(
        select(MailMessage).where(
            MailMessage.account_id == account_id,
            MailMessage.message_id == message_id,
        )
    )


def save_last_error(db: Session, account: MailAccount, text: str, at: datetime) -> None:
    """Записать последнюю ошибку ящика так, чтобы она пережила откат запроса.

    Синхронизация и отправка заканчиваются исключением (`ValidationError` →
    422), а исключение откатывает транзакцию запроса целиком — вместе с
    записанной в неё ошибкой. Ровно там, где ошибку и надо показать, она и
    пропадала: root открывал настройки и видел чистый ящик, хотя почта не
    забиралась вторую неделю. Отсюда `commit` посреди запроса — приём в проекте
    не новый (так же поступает выгрузка работы на доску перед планированием
    фоновой задачи, `web/api/routes/boards.py`), но здесь он живёт в
    репозитории: правило «запросы к базе — только в database/» распространяется
    и на управление её транзакцией.

    **Почему не отдельная короткая сессия, как для фоновой задачи.** База на
    боевом сервере — SQLite, и писать в неё одновременно может только один. К
    моменту отказа транспорта транзакция запроса обычно уже пишет: любой запрос
    обновляет сотруднику «последний раз в сети» (`auth_service`), и эта правка
    висит незафиксированной. Второе соединение упирается в её блокировку.
    Проверено на этой базе: отдельная сессия ждала 5,5 секунды и отвечала
    `database is locked`, ошибка не сохранялась вовсе — то есть починка не
    работала бы именно в том случае, ради которого написана.

    Фиксируется при этом всё, что в транзакции накопилось. На всех трёх дорогах
    сюда (проверка ящика, синхронизация, отправка) накопиться успевает только
    отметка присутствия: письма ещё не сохранялись, отказ случился раньше.
    Появится между началом запроса и этим местом что-то ещё — оно тоже уедет в
    базу, и это стоит помнить, дописывая сюда шаги.
    """
    account.last_error = text
    account.last_error_at = at
    try:
        db.commit()
    except Exception:
        # После неудавшейся фиксации сессия непригодна, пока её не откатили, а
        # вызывающему ещё отвечать человеку — он должен получить свою ошибку, а
        # не `PendingRollbackError` поверх неё.
        db.rollback()
        raise


def last_uid(db: Session, account_id: int) -> int | None:
    """С какого места продолжать выборку с сервера. None — ящик ещё не читали."""
    return db.scalar(select(func.max(MailMessage.uid)).where(MailMessage.account_id == account_id))


def search_messages(
    db: Session,
    account_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    direction: str | None = None,
    unread: bool | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[MailMessage], int]:
    """Список писем. Тела не читаются — они `deferred` в модели."""
    stmt = select(MailMessage)
    if account_id:
        stmt = stmt.where(MailMessage.account_id == account_id)
    if client_id:
        stmt = stmt.where(MailMessage.client_id == client_id)
    if deal_id:
        stmt = stmt.where(MailMessage.deal_id == deal_id)
    if direction:
        stmt = stmt.where(MailMessage.direction == direction)
    if unread is not None:
        stmt = stmt.where(MailMessage.is_read == (not unread))
    if q:
        needle = q.strip()
        stmt = stmt.where(
            or_(
                contains(MailMessage.subject, needle),
                contains(MailMessage.from_addr, needle),
                contains(MailMessage.to_addrs, needle),
            )
        )
    stmt = stmt.order_by(MailMessage.sent_at.desc(), MailMessage.id.desc())
    return page_of(db, stmt, page=page, per_page=per_page)


def find_client_by_email(db: Session, address: str) -> Client | None:
    """Клиент с таким адресом почты. Удалённые не считаются.

    Сравнение через `lower()`, а не `ilike`: в адресах законно встречаются `_` и
    `%`, а для LIKE это шаблонные символы — «a_b@x.com» нашёл бы «axb@x.com» и
    привязал переписку к чужой карточке. `lower` в SQLite подменён на
    Unicode-версию (database/session.py), так что регистр учитывается верно.
    """
    normalized = (address or "").strip().lower()
    if not normalized:
        return None
    return db.scalar(
        select(Client)
        .where(Client.deleted_at.is_(None), func.lower(Client.email) == normalized)
        .order_by(Client.id)
        .limit(1)
    )
