"""Таблица лидеров змейки со страницы обслуживания."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import SnakeScore


def add(db: Session, name: str, score: int, played_ms: int, ip_hash: str) -> SnakeScore:
    row = SnakeScore(name=name, score=score, played_ms=played_ms, ip_hash=ip_hash)
    db.add(row)
    db.flush()
    return row


def top(db: Session, limit: int = 10) -> list[SnakeScore]:
    """Лучшие результаты. При равном счёте выше тот, кто добрался раньше."""
    return list(
        db.scalars(
            select(SnakeScore)
            .order_by(SnakeScore.score.desc(), SnakeScore.created_at.asc())
            .limit(limit)
        )
    )


def count_since(db: Session, ip_hash: str, since: datetime) -> int:
    """Сколько результатов пришло с этого адреса за период — для ограничения частоты."""
    return (
        db.scalar(
            select(func.count())
            .select_from(SnakeScore)
            .where(SnakeScore.ip_hash == ip_hash, SnakeScore.created_at >= since)
        )
        or 0
    )
