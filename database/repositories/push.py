"""Подписки Web Push: по отпечатку адреса, по сотруднику, отметка доставки."""

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from database.models import PushSubscription


def po_hashu(db: Session, endpoint_hash: str) -> PushSubscription | None:
    return db.scalar(select(PushSubscription).where(PushSubscription.endpoint_hash == endpoint_hash))


def dlya(db: Session, user_ids) -> list[PushSubscription]:
    user_ids = list(set(user_ids))
    if not user_ids:
        return []
    return list(
        db.scalars(
            select(PushSubscription)
            .where(PushSubscription.user_id.in_(user_ids))
            .order_by(PushSubscription.created_at.desc(), PushSubscription.id.desc())
        )
    )


def dobavit(db: Session, podpiska: PushSubscription) -> PushSubscription:
    db.add(podpiska)
    db.flush()
    db.refresh(podpiska)
    return podpiska


def ubrat(db: Session, podpiska_id: int) -> None:
    db.execute(delete(PushSubscription).where(PushSubscription.id == podpiska_id))


def ubrat_svoyu(db: Session, user_id: int, endpoint_hash: str) -> int:
    return db.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.endpoint_hash == endpoint_hash
        )
    ).rowcount


def ubrat_vse(db: Session, user_id: int) -> int:
    return db.execute(delete(PushSubscription).where(PushSubscription.user_id == user_id)).rowcount


def dostavleno(db: Session, podpiska_id: int, kogda: datetime) -> None:
    db.execute(update(PushSubscription).where(PushSubscription.id == podpiska_id).values(last_ok_at=kogda))
