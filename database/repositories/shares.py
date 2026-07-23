from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import ShareLink, ShareView


def get(db: Session, share_id: int) -> ShareLink | None:
    return db.get(ShareLink, share_id)


def get_by_token(db: Session, token: str) -> ShareLink | None:
    return db.scalar(select(ShareLink).where(ShareLink.token == token))


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
    base = select(ShareView).where(ShareView.share_link_id == share_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = (
        base.order_by(ShareView.viewed_at.desc(), ShareView.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return list(db.scalars(stmt)), total
