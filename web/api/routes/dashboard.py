from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.services import board_service
from core.utils import now_utc
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import stats as stats_repo
from web.api import schemas
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    now = now_utc()
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    clients_total, clients_this_month = stats_repo.clients_totals(db)
    boards_total, boards_published = stats_repo.boards_totals(db)
    views_7d = stats_repo.views_in_range(db, week_ago, now)
    views_prev_7d = stats_repo.views_in_range(db, two_weeks_ago, week_ago)
    last_view = stats_repo.last_view_at(db)

    recent_boards, _total = boards_repo.search(db, page=1, per_page=4)
    boards_payload = []
    for board in recent_boards:
        data = schemas.board_out(board, works_count=boards_repo.count_works(db, board.id))
        cover = board_service.cover_work(db, board)
        data["cover"] = schemas.work_out(cover)["media"] if cover else None
        data["views_count"] = stats_repo.board_views_count(db, board.id)
        data.update(stats_repo.board_share_flags(db, board.id))
        boards_payload.append(data)

    recent_clients, _total = clients_repo.search(db, page=1, per_page=5)

    return {
        "clients_total": clients_total,
        "clients_this_month": clients_this_month,
        "boards_total": boards_total,
        "boards_published": boards_published,
        "views_7d": views_7d,
        "views_prev_7d": views_prev_7d,
        "views_by_day": stats_repo.views_by_day(db, 7),
        "last_view_at": last_view.isoformat() if last_view else None,
        "recent_boards": boards_payload,
        "recent_clients": [schemas.client_out(c) for c in recent_clients],
    }
