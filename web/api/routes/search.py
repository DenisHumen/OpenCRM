from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.services import board_service, modules_service
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import stats as stats_repo
from web.api import schemas
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/search", tags=["search"])

# палитра показывает короткий список: длинные выдачи листают на своих экранах
GROUP_LIMIT = 6

def _boards_group(db: Session, query: str | None) -> dict:
    boards, total = boards_repo.search(db, q=query, page=1, per_page=GROUP_LIMIT)
    items = []
    for board in boards:
        data = schemas.board_out(board, works_count=boards_repo.count_works(db, board.id))
        cover = board_service.cover_work(db, board)
        data["cover"] = schemas.work_out(cover)["media"] if cover else None
        data["views_count"] = stats_repo.board_views_count(db, board.id)
        data.update(stats_repo.board_share_flags(db, board.id))
        if board.client_id:
            client = clients_repo.get(db, board.client_id)
            data["client_name"] = client.name if client else None
        else:
            data["client_name"] = None
        items.append(data)
    return {"items": items, "total": total}


@router.get("")
def global_search(
    q: str = Query(default="", max_length=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Общий поиск для командной палитры (Ctrl+K).

    Пустой запрос — недавние записи, чтобы палитра открывалась не пустой.

    Группа выключенного блока приходит пустой, а не отсутствует: форма ответа
    одна при любом наборе блоков, и клиенту не приходится знать, какие ключи
    сегодня бывают. Само выключение здесь обязательно — иначе выключенные доски
    продолжали бы находиться поиском и уводить в раздел, которого нет ни в меню,
    ни в маршрутах, а на сервере закрыт `require_module`.
    """
    query = q.strip() or None

    clients, clients_total = clients_repo.search(db, q=query, page=1, per_page=GROUP_LIMIT)

    return {
        "query": q,
        "clients": {
            "items": [schemas.client_out(c) for c in clients],
            "total": clients_total,
        },
        "boards": (
            _boards_group(db, query)
            if modules_service.is_enabled(db, "boards")
            else {"items": [], "total": 0}
        ),
    }
