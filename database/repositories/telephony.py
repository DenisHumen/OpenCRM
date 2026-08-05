from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Client, PhoneCall


def get(db: Session, call_id: int) -> PhoneCall | None:
    return db.get(PhoneCall, call_id)


def get_by_external_id(db: Session, external_id: str) -> PhoneCall | None:
    """Звонок по идентификатору АТС — точка склейки повторных событий."""
    return db.scalar(select(PhoneCall).where(PhoneCall.external_id == external_id))


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
        .order_by(Client.updated_at.desc())
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
        like = f"%{number}%"
        stmt = stmt.where(
            or_(PhoneCall.from_number_norm.like(like), PhoneCall.to_number_norm.like(like))
        )
    if since is not None:
        stmt = stmt.where(PhoneCall.started_at >= since)
    if until is not None:
        stmt = stmt.where(PhoneCall.started_at <= until)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = (
        stmt.order_by(PhoneCall.started_at.desc(), PhoneCall.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return list(db.scalars(stmt)), total
