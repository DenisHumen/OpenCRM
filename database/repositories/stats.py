"""Агрегаты для дашборда CRM."""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.utils import now_utc
from database.models import Board, Client, ShareLink, ShareView


def clients_totals(db: Session) -> tuple[int, int]:
    base = select(func.count()).select_from(Client).where(Client.deleted_at.is_(None))
    total = db.scalar(base) or 0
    month_start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = db.scalar(base.where(Client.created_at >= month_start)) or 0
    return total, this_month


def boards_totals(db: Session) -> tuple[int, int]:
    base = select(func.count()).select_from(Board).where(Board.deleted_at.is_(None))
    total = db.scalar(base) or 0
    published = db.scalar(base.where(Board.is_published.is_(True))) or 0
    return total, published


def views_in_range(db: Session, start: datetime, end: datetime) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(ShareView)
            .where(ShareView.viewed_at >= start, ShareView.viewed_at < end)
        )
        or 0
    )


def views_by_day(db: Session, days: int = 7) -> list[dict]:
    """Просмотры по дням за последние `days` дней (включая сегодня)."""
    now = now_utc()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(func.date(ShareView.viewed_at), func.count())
        .where(ShareView.viewed_at >= start)
        .group_by(func.date(ShareView.viewed_at))
    ).all()
    counts = {str(day): count for day, count in rows}
    result = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        result.append({"date": day.strftime("%Y-%m-%d"), "count": counts.get(day.strftime("%Y-%m-%d"), 0)})
    return result


def last_view_at(db: Session) -> datetime | None:
    return db.scalar(select(func.max(ShareView.viewed_at)))


def board_views_count(db: Session, board_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(ShareView)
            .join(ShareLink, ShareLink.id == ShareView.share_link_id)
            .where(ShareLink.board_id == board_id)
        )
        or 0
    )


def board_share_flags(db: Session, board_id: int) -> dict:
    """Флаги ссылок доски: есть ли активная, есть ли PIN, были ли ссылки вообще."""
    links = db.scalars(select(ShareLink).where(ShareLink.board_id == board_id)).all()
    now = now_utc()
    active = [
        l for l in links
        if l.is_active and (l.expires_at is None or l.expires_at > now)
    ]
    return {
        "has_links": len(links) > 0,
        "has_active_link": len(active) > 0,
        "has_pin": any(l.pin_hash is not None for l in active),
    }
