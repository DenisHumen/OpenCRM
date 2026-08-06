"""Освобождение места: окончательное удаление мягко удалённых записей.

Мягкое удаление защищает от случайной потери данных, но занятые файлы висят
на диске. Эта логика чистит их — вызывается и вручную из настроек (root),
и по cron через scripts/purge_deleted.py.

**Уборка убирает мусор, но никогда не деньги.** Удаление клиента уходит
каскадом в его заявки, а через них в журнал этапов и напоминания. Значит
нажатие «Очистить место» способно задним числом стереть прошлогоднюю выручку
из отчётов — и человек, освобождавший диск, об этом даже не узнает: в ответе
заявок нет, в журнал операция не пишется.

Поэтому клиент, у которого есть выигранные заявки, не удаляется вовсе и
называется отдельной строкой. Закрытая сделка — это деньги, которые фирма
действительно получила; то, что карточку клиента потом убрали, их не
отменяет. Освободить место такой клиент почти и не мешает: место занимают
файлы и доски, а не строка в таблице.
"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.services import client_service, media_service, storage_service
from core.utils import now_utc
from database.models import Board, Client, ClientFile, Deal, PipelineStage, Work
from database.models.pipeline import KIND_WON
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
    removed_deals = 0
    kept_with_revenue = 0

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
        # Выигранная заявка — полученные деньги. Такого клиента не трогаем:
        # отчёт за прошлый год не должен меняться от уборки диска.
        has_revenue = db.scalar(
            select(Deal.id)
            .join(PipelineStage, PipelineStage.key == Deal.stage)
            .where(
                Deal.client_id == client.id,
                Deal.deleted_at.is_(None),
                PipelineStage.kind == KIND_WON,
            )
            .limit(1)
        )
        if has_revenue is not None:
            kept_with_revenue += 1
            continue

        # Сколько заявок уйдёт вместе с клиентом — считаем и НАЗЫВАЕМ. Молчать
        # об этом хуже, чем удалить: человек видит «удалено 3 клиента» и не
        # знает про сорок заявок.
        removed_deals += int(
            db.scalar(
                select(func.count()).select_from(Deal).where(Deal.client_id == client.id)
            )
            or 0
        )
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
        "deals": removed_deals,
        "clients_kept_with_revenue": kept_with_revenue,
        "dry_run": dry_run,
    }
