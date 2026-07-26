from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import User, UserSession


def get_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def count_by_role(db: Session, role: str) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == role)) or 0


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


def get_root(db: Session) -> User | None:
    return db.scalar(select(User).where(User.role == "root"))


def list_staff(db: Session, status: str | None = None) -> list[User]:
    q = select(User).order_by(User.created_at.desc())
    if status:
        q = q.where(User.status == status)
    return list(db.scalars(q))


def create_session(db: Session, user_id: int, token_hash: str, expires_at: datetime) -> UserSession:
    s = UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    db.add(s)
    db.flush()
    return s


def get_session_by_hash(db: Session, token_hash: str) -> UserSession | None:
    return db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))


def delete_session(db: Session, session: UserSession) -> None:
    db.delete(session)


def delete_sessions_for_user(db: Session, user_id: int) -> None:
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        db.delete(s)


def delete_other_sessions(db: Session, user_id: int, keep_token_hash: str) -> None:
    """Отзывает все сессии пользователя, кроме текущей (по хэшу токена)."""
    for s in db.scalars(
        select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.token_hash != keep_token_hash,
        )
    ):
        db.delete(s)


def purge_expired_sessions(db: Session) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for s in db.scalars(select(UserSession).where(UserSession.expires_at < now)):
        db.delete(s)
