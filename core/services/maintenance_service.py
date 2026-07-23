"""Освобождение места: окончательное удаление мягко удалённых записей.

Мягкое удаление защищает от случайной потери данных, но занятые файлы висят
на диске. Эта логика чистит их — вызывается и вручную из настроек (root),
и по cron через scripts/purge_deleted.py.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.services import client_service, media_service, storage_service
from core.utils import now_utc
from database.models import Board, Client, ClientFile, Work
from database.repositories import users as users_repo


def purge_soft_deleted(db: Session, older_than_days: int = 0, dry_run: bool = False) -> dict:
    """Удаляет доски и клиентов, помеченных удалёнными раньше карантина.

    `older_than_days=0` — вычистить всё помеченное (кнопка «Очистить» в UI).
    """
    cutoff = now_utc() - timedelta(days=older_than_days)
    freed_bytes = 0
    removed_boards = 0
    removed_works = 0
    removed_clients = 0
    removed_files = 0

    boards = list(
        db.scalars(
            select(Board).where(Board.deleted_at.is_not(None), Board.deleted_at <= cutoff)
        )
    )
    for board in boards:
        works = list(db.scalars(select(Work).where(Work.board_id == board.id)))
        for work in works:
            directory = media_service.work_dir(work.work_uid)
            freed_bytes += storage_service.dir_size(directory)
            removed_works += 1
            if not dry_run:
                media_service.delete_work_files(work.work_uid)
        removed_boards += 1
        if not dry_run:
            db.delete(board)  # works и share_links уходят каскадом

    clients = list(
        db.scalars(
            select(Client).where(Client.deleted_at.is_not(None), Client.deleted_at <= cutoff)
        )
    )
    for client in clients:
        files = list(db.scalars(select(ClientFile).where(ClientFile.client_id == client.id)))
        for file in files:
            path = client_service.file_path_on_disk(file)
            if path.exists():
                freed_bytes += path.stat().st_size
            removed_files += 1
            if not dry_run:
                path.unlink(missing_ok=True)
        removed_clients += 1
        if not dry_run:
            db.delete(client)  # notes и files уходят каскадом

    if not dry_run:
        users_repo.purge_expired_sessions(db)
        db.flush()
        storage_service.invalidate_size_cache()

    return {
        "freed_bytes": freed_bytes,
        "boards": removed_boards,
        "works": removed_works,
        "clients": removed_clients,
        "client_files": removed_files,
        "dry_run": dry_run,
    }
