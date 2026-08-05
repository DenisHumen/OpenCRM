from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Client, MailAccount, MailMessage


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


def find_by_message_id(db: Session, message_id: str) -> MailMessage | None:
    """Ключ идемпотентности синхронизации: это письмо у нас уже есть?"""
    return db.scalar(select(MailMessage).where(MailMessage.message_id == message_id))


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
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                MailMessage.subject.ilike(like),
                MailMessage.from_addr.ilike(like),
                MailMessage.to_addrs.ilike(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = (
        stmt.order_by(MailMessage.sent_at.desc(), MailMessage.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return list(db.scalars(stmt)), total


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
