from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import ShareLink, ShareView
from database.query import page_of


def last_view_by_board(db: Session) -> dict[int, datetime]:
    """Когда витрину каждой доски смотрели в последний раз.

    Одним запросом на все доски, а не по разу на доску: этим пользуется менеджер
    файлов, где строк тысячи, и запрос в цикле превратил бы открытие экрана в
    минуты. Доски, которую не смотрели ни разу, в ответе нет — «никогда»
    подставляет спрашивающий.
    """
    rows = db.execute(
        select(ShareLink.board_id, func.max(ShareView.viewed_at))
        .join(ShareView, ShareView.share_link_id == ShareLink.id)
        .group_by(ShareLink.board_id)
    ).all()
    return {board_id: seen for board_id, seen in rows if seen is not None}


def get(db: Session, share_id: int) -> ShareLink | None:
    return db.get(ShareLink, share_id)


def get_by_token(db: Session, token: str) -> ShareLink | None:
    return db.scalar(select(ShareLink).where(ShareLink.token == token))


def live_for_board(db: Session, board_id: int, now: datetime) -> list[ShareLink]:
    """Ссылки доски, которые прямо сейчас работают: включённые и не просроченные.

    Отдельно от `list_for_board`: тот показывает хозяину все ссылки, включая
    отозванные и истёкшие, — ему надо видеть, что он выдавал. Здесь спрашивают
    другое: «можно ли по этой доске что-нибудь показать снаружи», и отозванная
    ссылка на такой вопрос отвечает «нет».
    """
    return list(
        db.scalars(
            select(ShareLink).where(
                ShareLink.board_id == board_id,
                ShareLink.is_active.is_(True),
                (ShareLink.expires_at.is_(None)) | (ShareLink.expires_at >= now),
            )
        )
    )


def list_for_board(db: Session, board_id: int) -> list[ShareLink]:
    return list(
        db.scalars(
            select(ShareLink)
            .where(ShareLink.board_id == board_id)
            .order_by(ShareLink.created_at.desc())
        )
    )


def views_count(db: Session, share_id: int) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(ShareView).where(ShareView.share_link_id == share_id)
        )
        or 0
    )


def unique_views_count(db: Session, share_id: int) -> int:
    return (
        db.scalar(
            select(func.count(func.distinct(ShareView.ip_hash))).where(
                ShareView.share_link_id == share_id
            )
        )
        or 0
    )


def last_view(db: Session, share_id: int) -> ShareView | None:
    return db.scalar(
        select(ShareView)
        .where(ShareView.share_link_id == share_id)
        .order_by(ShareView.viewed_at.desc(), ShareView.id.desc())
        .limit(1)
    )


def list_views(
    db: Session, share_id: int, page: int = 1, per_page: int = 50
) -> tuple[list[ShareView], int]:
    stmt = (
        select(ShareView)
        .where(ShareView.share_link_id == share_id)
        .order_by(ShareView.viewed_at.desc(), ShareView.id.desc())
    )
    return page_of(db, stmt, page=page, per_page=per_page)
