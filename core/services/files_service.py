"""Уборка медиафайлов работ (root).

Обзор диска переехал в блок «Файлы» (`core/services/fayly_service.py`): он
показывает деревом всё, что лежит на диске, а не только работы досок. Здесь
осталось удаление — оно сносит работу С ДОСКИ, и правом блока «Файлы»
закрываться не должно.
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import media_service, storage_service
from core.utils import now_utc
from database.repositories import boards as boards_repo


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
