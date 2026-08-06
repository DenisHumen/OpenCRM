"""Менеджер файлов (root): обзор всех медиафайлов работ на диске.

Показывает реальный размер на диске (оригинал + превью), дату загрузки и дату
последнего просмотра доски, чтобы находить и удалять то, что занимает место.
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import media_service, storage_service
from core.utils import now_utc
from database.repositories import boards as boards_repo
from database.repositories import shares as shares_repo


def list_media_files(db: Session) -> list[dict]:
    """Все работы живых досок с размером на диске, датами загрузки и просмотра."""
    last_viewed = shares_repo.last_view_by_board(db)
    rows = boards_repo.works_with_board_title(db)

    items: list[dict] = []
    for work, board_title in rows:
        media = media_service.work_media_urls(work) if work.status == "ready" else {}
        items.append(
            {
                "id": work.id,
                "board_id": work.board_id,
                "board_title": board_title,
                "kind": work.kind,
                "status": work.status,
                "original_name": work.original_name,
                "mime": work.mime,
                "size_bytes": storage_service.dir_size(media_service.work_dir(work.work_uid)),
                "created_at": work.created_at,
                "last_viewed_at": last_viewed.get(work.board_id),
                "thumb": media.get("thumb"),
            }
        )
    return items


def delete_media_file(db: Session, work_id: int) -> None:
    """Удаляет одну работу вместе с файлами (для менеджера файлов, root)."""
    work = boards_repo.get_work_by_id(db, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    board = boards_repo.get(db, work.board_id, include_deleted=True)
    if board is not None:
        if board.cover_work_id == work.id:
            board.cover_work_id = None
        board.updated_at = now_utc()
    uid = work.work_uid
    db.delete(work)
    db.flush()
    media_service.delete_work_files(uid)
    storage_service.invalidate_size_cache()
