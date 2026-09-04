"""Ключи доступа сайта: поиск по отпечатку, области, отметка обращения."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import ApiKey, ApiKeyScope


def get(db: Session, key_id: int) -> ApiKey | None:
    return db.get(ApiKey, key_id)


def get_by_hash(db: Session, token_hash: str) -> ApiKey | None:
    """По отпечатку через уникальный индекс: ни перебора, ни постоянного времени."""
    return db.scalar(select(ApiKey).where(ApiKey.token_hash == token_hash))


def list_all(db: Session) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id.desc())))


def add(db: Session, key: ApiKey, scopes) -> ApiKey:
    db.add(key)
    db.flush()
    for scope in sorted(set(scopes)):
        db.add(ApiKeyScope(api_key_id=key.id, scope=scope))
    db.flush()
    return key


def scopes_of(db: Session, key_id: int) -> set[str]:
    return set(db.scalars(select(ApiKeyScope.scope).where(ApiKeyScope.api_key_id == key_id)))


def scopes_by_keys(db: Session, key_ids) -> dict[int, set[str]]:
    key_ids = list(key_ids)
    if not key_ids:
        return {}
    itog: dict[int, set[str]] = {}
    for key_id, scope in db.execute(
        select(ApiKeyScope.api_key_id, ApiKeyScope.scope).where(ApiKeyScope.api_key_id.in_(key_ids))
    ).all():
        itog.setdefault(key_id, set()).add(scope)
    return itog


def touch(db: Session, key: ApiKey, ip: str, at: datetime) -> None:
    key.last_used_at = at
    key.last_used_ip = ip[:45]
    db.flush()


def alive_count(db: Session, now: datetime) -> int:
    """Сколько ключей открывают систему наружу прямо сейчас."""
    zhivye = db.scalars(select(ApiKey).where(ApiKey.revoked_at.is_(None)))
    return sum(1 for k in zhivye if k.expires_at is None or k.expires_at > now)
