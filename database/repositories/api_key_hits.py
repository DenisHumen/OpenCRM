"""Обращения по ключам сайта: счётчик на (ключ, час, область), выборки для графиков."""

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models.api_key import ApiKeyHit


def zapisat(db: Session, key_id: int, bucket_at: datetime, category: str, *, rejected: bool) -> None:
    """Прибавить единицу к строке часа; строки нет — завести.

    Два первых обращения одного часа приходят одновременно, и оба не находят
    строки: вставка проигравшего упирается в `uq_api_key_hit`. Поэтому вставка
    под точкой сохранения — откатывается она одна, а не вся сессия запроса.
    """
    stolbets = ApiKeyHit.rejected if rejected else ApiKeyHit.count
    stmt = (
        update(ApiKeyHit)
        .where(ApiKeyHit.api_key_id == key_id, ApiKeyHit.bucket_at == bucket_at, ApiKeyHit.category == category)
        .values({stolbets: stolbets + 1})
    )
    if db.execute(stmt).rowcount:
        return
    try:
        with db.begin_nested():
            db.add(
                ApiKeyHit(
                    api_key_id=key_id,
                    bucket_at=bucket_at,
                    category=category,
                    count=0 if rejected else 1,
                    rejected=1 if rejected else 0,
                )
            )
            db.flush()
    except IntegrityError:
        db.execute(stmt)


def stroki(db: Session, key_id: int, since: datetime) -> list[ApiKeyHit]:
    """Все часовые строки ключа с момента `since` — для графиков по дням и часам."""
    return list(
        db.scalars(
            select(ApiKeyHit)
            .where(ApiKeyHit.api_key_id == key_id, ApiKeyHit.bucket_at >= since)
            .order_by(ApiKeyHit.bucket_at)
        )
    )


def itogi_po_klyucham(db: Session, key_ids, since: datetime) -> dict[int, int]:
    """Сколько обращений у каждого ключа с момента `since` — числом в списке."""
    key_ids = list(key_ids)
    if not key_ids:
        return {}
    rows = db.execute(
        select(ApiKeyHit.api_key_id, func.sum(ApiKeyHit.count))
        .where(ApiKeyHit.api_key_id.in_(key_ids), ApiKeyHit.bucket_at >= since)
        .group_by(ApiKeyHit.api_key_id)
    ).all()
    return {key_id: int(summa or 0) for key_id, summa in rows}


def purge_older_than(db: Session, before: datetime) -> int:
    return int(db.execute(delete(ApiKeyHit).where(ApiKeyHit.bucket_at < before)).rowcount or 0)
