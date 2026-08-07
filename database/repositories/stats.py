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


def unique_viewers_in_range(db: Session, start: datetime, end: datetime) -> int:
    """Сколько разных людей открывали витрины за период.

    Считаем по `ip_hash` — тому же признаку, по которому уникальных показывает
    карточка доски, иначе два числа рядом означали бы разное. Это оценка сверху
    по домохозяйству и снизу по мобильной сети: точнее без слежки за человеком
    не выйдет, а её мы себе не позволяем.
    """
    return (
        db.scalar(
            select(func.count(func.distinct(ShareView.ip_hash))).where(
                ShareView.viewed_at >= start, ShareView.viewed_at < end
            )
        )
        or 0
    )


def views_window(days: int = 7, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Окно графика просмотров: `days` календарных суток, последние — сегодняшние.

    Одно окно на плитку и на столбики под ней. Плитка считала скользящие 168
    часов, график — семь календарных суток, и два числа об одном и том же на
    одном экране не сходились: столбик за седьмой день назад показывал весь
    день, а плитка брала от него только хвост после текущего часа.

    Правильным считаем календарь: столбики человек проверяет глазами и
    складывает, а скользящее окно проверить нечем. Поэтому плитка приводится к
    графику, а не наоборот.

    Границы — полуоткрытый интервал [начало; начало+дни), как у периодов
    отчётов: конец окна — полночь ПОСЛЕ сегодняшнего дня, иначе просмотр
    сегодняшнего вечера выпал бы из плитки, оставшись в столбике.

    `now` параметром, а не только из `now_utc()`: окно считается один раз и
    раздаётся всем запросам сводки. Посчитанное дважды, оно разъехалось бы на
    полуночи — редко, зато ровно тем расхождением, ради которого всё это и
    писалось.
    """
    moment = now or now_utc()
    start = (moment - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=days)


def views_by_day(db: Session, start: datetime, end: datetime) -> list[dict]:
    """Просмотры по суткам окна [start; end).

    Пустые дни остаются в списке: провал видно только тогда, когда он нарисован
    нулём, а не пропущен.
    """
    rows = db.execute(
        select(func.date(ShareView.viewed_at), func.count())
        .where(ShareView.viewed_at >= start, ShareView.viewed_at < end)
        .group_by(func.date(ShareView.viewed_at))
    ).all()
    counts = {str(day): int(count or 0) for day, count in rows}
    result = []
    day = start
    while day < end:
        label = day.strftime("%Y-%m-%d")
        result.append({"date": label, "count": counts.get(label, 0)})
        day += timedelta(days=1)
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


def views_by_board(db: Session, board_ids: list[int]) -> dict[int, int]:
    """Просмотры каждой доски — одним запросом на весь список."""
    if not board_ids:
        return {}
    rows = db.execute(
        select(ShareLink.board_id, func.count())
        .select_from(ShareView)
        .join(ShareLink, ShareLink.id == ShareView.share_link_id)
        .where(ShareLink.board_id.in_(board_ids))
        .group_by(ShareLink.board_id)
    ).all()
    return {board_id: count for board_id, count in rows}


def share_flags_by_board(db: Session, board_ids: list[int]) -> dict[int, dict]:
    """Флаги ссылок сразу для списка досок. Правило то же, что и поштучно."""
    if not board_ids:
        return {}
    now = now_utc()
    flags: dict[int, dict] = {}
    for link in db.scalars(select(ShareLink).where(ShareLink.board_id.in_(board_ids))):
        state = flags.setdefault(
            link.board_id, {"has_links": False, "has_active_link": False, "has_pin": False}
        )
        state["has_links"] = True
        if link.is_active and (link.expires_at is None or link.expires_at > now):
            state["has_active_link"] = True
            if link.pin_hash is not None:
                state["has_pin"] = True
    return flags


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
