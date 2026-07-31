from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Board, Work


def get(db: Session, board_id: int, include_deleted: bool = False) -> Board | None:
    board = db.get(Board, board_id)
    if board is None:
        return None
    if board.deleted_at is not None and not include_deleted:
        return None
    return board


def search(
    db: Session,
    q: str | None = None,
    client_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Board], int]:
    stmt = select(Board).where(Board.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Board.title.ilike(like), Board.description.ilike(like)))
    if client_id:
        stmt = stmt.where(Board.client_id == client_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(Board.updated_at.desc()).offset((page - 1) * per_page).limit(per_page)
    return list(db.scalars(stmt)), total


def list_works(db: Session, board_id: int, only_ready: bool = False) -> list[Work]:
    stmt = select(Work).where(Work.board_id == board_id)
    if only_ready:
        stmt = stmt.where(Work.status == "ready")
    stmt = stmt.order_by(Work.sort_order.asc(), Work.id.asc())
    return list(db.scalars(stmt))


def get_work(db: Session, board_id: int, work_id: int) -> Work | None:
    work = db.get(Work, work_id)
    if work is None or work.board_id != board_id:
        return None
    return work


def next_sort_order(db: Session, board_id: int) -> int:
    current = db.scalar(
        select(func.max(Work.sort_order)).where(Work.board_id == board_id)
    )
    return (current or 0) + 10


def count_works(db: Session, board_id: int) -> int:
    return db.scalar(select(func.count()).where(Work.board_id == board_id).select_from(Work)) or 0
