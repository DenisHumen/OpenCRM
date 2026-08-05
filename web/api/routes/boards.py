from fastapi import APIRouter, BackgroundTasks, Depends, Query, UploadFile
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import board_service, share_service
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import shares as shares_repo
from database.repositories import stats as stats_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_staff
from web.public import layout

router = APIRouter(
    prefix="/boards", tags=["boards"], dependencies=[Depends(require_module("boards"))]
)


@router.get("")
def list_boards(
    search: str | None = None,
    client_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    boards, total = boards_repo.search(db, q=search, client_id=client_id, page=page, per_page=per_page)
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
    return schemas.paginated(items, total, page, per_page)


@router.post("", status_code=201)
def create_board(
    payload: schemas.BoardIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board = board_service.create_board(
        db, user, payload.title, payload.description or "", payload.client_id, payload.deal_id
    )
    return schemas.board_out(board, works_count=0)


@router.get("/{board_id}")
def get_board(
    board_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board = board_service.get_board(db, board_id)
    works = boards_repo.list_works(db, board_id)
    links = shares_repo.list_for_board(db, board_id)
    data = schemas.board_out(board, works_count=len(works))
    data["works"] = [schemas.work_out(w) for w in works]
    # форма места каждой работы на витрине — редактору обрезки, чтобы рамка
    # фрагмента совпадала с тем, что увидит клиент. Композицию собирают только
    # готовые работы, поэтому необработанные места пока не занимают.
    ready = [w for w in data["works"] if w["status"] == "ready"]
    for work, ratio in zip(ready, layout.place_ratios(len(ready))):
        work["place_ratio"] = ratio
    data["shares"] = [
        schemas.share_out(link, share_service.share_stats(db, link)) for link in links
    ]
    return data


@router.patch("/{board_id}")
def update_board(
    board_id: int,
    payload: schemas.BoardPatchIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board = board_service.update_board(db, board_id, payload.model_dump(exclude_unset=True))
    return schemas.board_out(board, works_count=boards_repo.count_works(db, board.id))


@router.delete("/{board_id}")
def delete_board(
    board_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board_service.delete_board(db, board_id)
    return {"message": "Board deleted"}


# --- работы ---

@router.post("/{board_id}/works", status_code=202)
async def upload_work(
    board_id: int,
    file: UploadFile,
    background: BackgroundTasks,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    content = await file.read()
    work = board_service.upload_work(db, board_id, file.filename or "file", content)
    # коммитим до планирования фоновой задачи: она работает в своей сессии
    db.commit()
    background.add_task(board_service.process_work, work.id)
    return schemas.work_out(work)


@router.get("/{board_id}/works/{work_id}")
def get_work(
    board_id: int,
    work_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    return schemas.work_out(work)


@router.patch("/{board_id}/works/{work_id}")
def update_work(
    board_id: int,
    work_id: int,
    payload: schemas.WorkPatchIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    work = board_service.update_work(db, board_id, work_id, payload.model_dump(exclude_unset=True))
    return schemas.work_out(work)


@router.put("/{board_id}/works/order")
def reorder_works(
    board_id: int,
    payload: schemas.WorkOrderIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    works = board_service.reorder_works(db, board_id, payload.work_ids)
    return {"items": [schemas.work_out(w) for w in works]}


@router.delete("/{board_id}/works/{work_id}")
def delete_work(
    board_id: int,
    work_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board_service.delete_work(db, board_id, work_id)
    return {"message": "Work deleted"}


# --- публичные ссылки доски ---

@router.get("/{board_id}/shares")
def list_shares(
    board_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board_service.get_board(db, board_id)
    links = shares_repo.list_for_board(db, board_id)
    return {
        "items": [schemas.share_out(l, share_service.share_stats(db, l)) for l in links]
    }


@router.post("/{board_id}/shares", status_code=201)
def create_share(
    board_id: int,
    payload: schemas.ShareIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    board = board_service.get_board(db, board_id)
    link = share_service.create_share(
        db, board, user, expires_at=payload.expires_at, pin=payload.pin
    )
    return schemas.share_out(link, {"views_count": 0, "unique_views_count": 0, "last_viewed_at": None})
