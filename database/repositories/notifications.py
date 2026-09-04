"""Уведомления: запись пачкой, список своих, счётчик, отметка о прочтении."""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database.models.notification import Notification
from database.query import page_of
from database.models.user import STATUS_ACTIVE, User


def add_many(db: Session, rows: list[Notification]) -> None:
    db.add_all(rows)
    db.flush()


def active_users(db: Session) -> list[User]:
    """Кому вообще можно писать: живые учётные записи, включая root."""
    return list(db.scalars(select(User).where(User.status == STATUS_ACTIVE).order_by(User.id)))


def list_for_user(db: Session, user_id: int, page: int, per_page: int) -> tuple[list[Notification], int]:
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    return page_of(db, stmt, page=page, per_page=per_page)


def unread_count(db: Session, user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


def mark_read(db: Session, user_id: int, ids: list[int] | None, when: datetime) -> int:
    """Отметить прочитанными свои: все или названные. Чужие не трогает по условию."""
    stmt = (
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=when)
    )
    if ids is not None:
        stmt = stmt.where(Notification.id.in_(ids))
    return int(db.execute(stmt).rowcount or 0)


def purge_older_than(db: Session, before: datetime) -> int:
    """Уборка: подсказки старше срока никому не нужны, а таблица растёт с каждым событием."""
    from sqlalchemy import delete

    return int(db.execute(delete(Notification).where(Notification.created_at < before)).rowcount or 0)
