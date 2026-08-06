import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import references
from core.services import media_service, storage_service
from core.utils import normalize_external_url, now_utc
from database.models import Board, User, Work
from database.models.board import WORK_FAILED, WORK_PROCESSING, WORK_READY
from database.repositories import boards as boards_repo
from database.session import get_session


def get_board(db: Session, board_id: int) -> Board:
    board = boards_repo.get(db, board_id)
    if board is None:
        raise errors.NotFoundError("Board not found", code="board_not_found")
    return board


def create_board(
    db: Session,
    author: User,
    title: str,
    description: str = "",
    client_id: int | None = None,
    deal_id: int | None = None,
) -> Board:
    if not title.strip():
        raise errors.ValidationError("Title is required", code="title_required")
    board = Board(
        title=title.strip(),
        description=(description or "").strip(),
        client_id=references.client(db, client_id),
        deal_id=references.deal(db, deal_id),
        created_by=author.id,
        is_published=False,
    )
    db.add(board)
    db.flush()
    return board


def update_board(db: Session, board_id: int, data: dict) -> Board:
    board = get_board(db, board_id)
    if "title" in data and data["title"] is not None:
        if not data["title"].strip():
            raise errors.ValidationError("Title is required", code="title_required")
        board.title = data["title"].strip()
    if "description" in data and data["description"] is not None:
        board.description = data["description"].strip()
    if "client_id" in data:
        board.client_id = data["client_id"]
    # Привязку к заявке можно и снять: доска переехала или создавалась не под неё.
    if "deal_id" in data:
        board.deal_id = data["deal_id"]
    if "is_published" in data and data["is_published"] is not None:
        board.is_published = bool(data["is_published"])
    if "cover_work_id" in data:
        cover_id = data["cover_work_id"]
        if cover_id is not None:
            work = boards_repo.get_work(db, board.id, cover_id)
            if work is None:
                raise errors.ValidationError("Cover work must belong to the board", code="bad_cover")
        board.cover_work_id = cover_id
    board.updated_at = now_utc()
    return board


def delete_board(db: Session, board_id: int) -> None:
    """Удаляет доску вместе с файлами работ.

    Раньше доска только помечалась удалённой (deleted_at), а медиа висело на
    диске до чистки корзины. Восстановления досок в UI нет, поэтому мягкое
    удаление лишь занимало место — теперь удаляем сразу и запись, и файлы.
    Записи works / share_links / share_views уходят каскадом (ondelete=CASCADE).
    """
    board = get_board(db, board_id)
    work_uids = [w.work_uid for w in boards_repo.list_works(db, board_id)]
    db.delete(board)
    db.flush()
    for uid in work_uids:
        media_service.delete_work_files(uid)
    storage_service.invalidate_size_cache()


def cover_work(db: Session, board: Board) -> Work | None:
    """Обложка доски: назначенная, иначе первая готовая работа."""
    if board.cover_work_id:
        work = boards_repo.get_work(db, board.id, board.cover_work_id)
        if work is not None and work.status == WORK_READY:
            return work
    ready = boards_repo.list_works(db, board.id, only_ready=True)
    return ready[0] if ready else None


# --- работы ---

def title_from_filename(original_name: str) -> str:
    """Название работы из имени файла: без расширения и без служебных знаков.

    Раньше название оставалось пустым, а на витрине подпись рисуется только при
    заполненном `title` — то есть свежезагруженная работа была безымянной, пока
    менеджер не переименует её руками. Имя файла почти всегда осмысленно, так
    что берём его: расширение убираем (`.jpg` в подписи под работой не нужен),
    подчёркивания и точки-разделители заменяем пробелами.
    """
    stem = Path(original_name).stem.strip()
    cleaned = re.sub(r"[_.]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200]


def upload_work(db: Session, board_id: int, original_name: str, content: bytes) -> Work:
    board = get_board(db, board_id)
    if not content:
        raise errors.ValidationError("File is empty", code="file_empty")
    if len(content) > get_settings().max_upload_bytes:
        raise errors.ValidationError("File is too large", code="file_too_large")
    # превью занимают ещё примерно столько же, поэтому запрашиваем двойной объём
    if not storage_service.has_room_for(len(content) * 2):
        raise errors.ValidationError(
            "Not enough free disk space on the server", code="disk_full"
        )
    detected_kind, ext, mime = media_service.detect_media(content[:512], original_name)
    kind = "image" if detected_kind == "svg" else detected_kind

    work = Work(
        board_id=board.id,
        work_uid=uuid.uuid4().hex,
        kind=kind,
        title=title_from_filename(original_name),
        status=WORK_PROCESSING,
        original_name=original_name[:255],
        mime=mime,
        size_bytes=len(content),
        sort_order=boards_repo.next_sort_order(db, board.id),
    )
    db.add(work)
    db.flush()
    media_service.save_original(work.work_uid, detected_kind, ext, content)
    board.updated_at = now_utc()
    return work


def process_work(work_id: int) -> None:
    """Фоновая задача: генерирует превью. Открывает собственную сессию БД."""
    db = get_session()
    try:
        work = db.get(Work, work_id)
        if work is None:
            return
        try:
            directory = media_service.work_dir(work.work_uid)
            if work.mime == "image/svg+xml":
                meta = {}  # SVG отдаётся как есть, превью не нужны
            elif work.kind == "image":
                ext = work.mime.split("/")[-1].replace("jpeg", "jpg")
                meta = media_service.process_image(work.work_uid, directory / f"original.{ext}")
            else:
                ext = "webm" if work.mime == "video/webm" else "mp4"
                meta = media_service.process_video(work.work_uid, directory / f"video.{ext}")
            for field in ("width", "height", "duration_sec", "blurhash"):
                if meta.get(field) is not None:
                    setattr(work, field, meta[field])
            work.status = WORK_READY
        except Exception:
            work.status = WORK_FAILED
            raise
        finally:
            db.commit()
    finally:
        db.close()


def update_work(db: Session, board_id: int, work_id: int, data: dict) -> Work:
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    if "title" in data and data["title"] is not None:
        work.title = data["title"].strip()[:200]
    if "description" in data and data["description"] is not None:
        work.description = data["description"].strip()
    if "project_url" in data and data["project_url"] is not None:
        try:
            work.project_url = normalize_external_url(data["project_url"])
        except ValueError as exc:
            raise errors.ValidationError(str(exc), code="bad_project_url") from exc
    _apply_preview_crop(work, data)
    return work


def _apply_preview_crop(work: Work, data: dict) -> None:
    """Выбранный менеджером фрагмент работы: 0 — верх картинки, 1 — низ.

    Двигать окно есть смысл только у длинной картинки — короткая помещается в
    своё место композиции целиком. `null` возвращает работу к показу от верха.
    """
    if "preview_focus" not in data:
        return
    if not media_service.is_long_image(work.width, work.height):
        raise errors.ValidationError(
            "Preview crop applies to long images only", code="not_a_long_work"
        )
    focus = data["preview_focus"]
    work.preview_focus = None if focus is None else round(max(0.0, min(1.0, float(focus))), 4)


def delete_work(db: Session, board_id: int, work_id: int) -> None:
    board = get_board(db, board_id)
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    if board.cover_work_id == work.id:
        board.cover_work_id = None
    uid = work.work_uid
    db.delete(work)
    db.flush()
    media_service.delete_work_files(uid)


def reorder_works(db: Session, board_id: int, work_ids: list[int]) -> list[Work]:
    board = get_board(db, board_id)
    works = boards_repo.list_works(db, board_id)
    existing_ids = {w.id for w in works}
    if set(work_ids) != existing_ids or len(work_ids) != len(existing_ids):
        raise errors.ValidationError(
            "work_ids must contain every work of the board exactly once", code="bad_order"
        )
    position = {work_id: (index + 1) * 10 for index, work_id in enumerate(work_ids)}
    for work in works:
        work.sort_order = position[work.id]
    board.updated_at = now_utc()
    return boards_repo.list_works(db, board_id)
