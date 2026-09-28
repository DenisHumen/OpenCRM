"""Токены сотрудников: поиск по отпечатку вместе с хозяином, список, отметка обращения."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from database.models import User, UserToken


def get(db: Session, token_id: int) -> UserToken | None:
    return db.get(UserToken, token_id)


def s_hozyainom(db: Session, token_hash: str) -> tuple[UserToken, User] | None:
    """Токен и его сотрудник одним запросом — это путь каждого обращения агента."""
    stroka = db.execute(
        select(UserToken, User)
        .join(User, User.id == UserToken.user_id)
        .where(UserToken.token_hash == token_hash)
    ).first()
    return (stroka[0], stroka[1]) if stroka else None


def spisok(db: Session) -> list[tuple[UserToken, User]]:
    return [
        (tok, user)
        for tok, user in db.execute(
            select(UserToken, User)
            .join(User, User.id == UserToken.user_id)
            .order_by(UserToken.created_at.desc(), UserToken.id.desc())
        ).all()
    ]


def dobavit(db: Session, token: UserToken) -> UserToken:
    db.add(token)
    db.flush()
    db.refresh(token)  # created_at ставит база
    return token


def otmetit(db: Session, token_id: int, kogda: datetime) -> None:
    """Мимо ORM, как присутствие сотрудника: грязная строка заперла бы токен на весь запрос."""
    db.execute(update(UserToken).where(UserToken.id == token_id).values(last_used_at=kogda))


def zhivyh(db: Session, now: datetime) -> int:
    return sum(
        1
        for tok in db.scalars(select(UserToken).where(UserToken.revoked_at.is_(None)))
        if tok.expires_at is None or tok.expires_at > now
    )
