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

from sqlalchemy.orm import Session

from core.services import client_service, media_service, storage_service, task_service
from core.utils import now_utc
from database.repositories import purge as purge_repo
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
    removed_task_files = 0
    removed_deals = 0
    kept_with_revenue = 0

    # Всё, что нужно про доски и клиентов, спрашивается наперёд — по разу на
    # весь список, а не по разу на запись; почему именно так, разобрано в шапке
    # `database/repositories/purge.py`.
    boards = purge_repo.boards_to_purge(db, cutoff)
    works_of = purge_repo.works_of_doomed_boards(db, cutoff)

    for board in boards:
        for work in works_of.get(board.id, ()):
            directory = media_service.work_dir(work.work_uid)
            freed_bytes += storage_service.dir_size(directory)
            removed_works += 1
            if not dry_run:
                media_service.delete_work_files(work.work_uid)
        removed_boards += 1
        if not dry_run:
            db.delete(board)  # works и share_links уходят каскадом

    clients = purge_repo.clients_to_purge(db, cutoff)
    # Выигранная заявка — полученные деньги. Такого клиента не трогаем.
    with_revenue = purge_repo.clients_with_revenue(db, cutoff)
    deals_of = purge_repo.deals_count_by_client(db, cutoff)
    files_of = purge_repo.files_of_doomed_clients(db, cutoff)
    # Вложения напоминаний уходят каскадом от клиента и от заявки, а файлы на
    # диске — ни от чего: снимаем их здесь, до `db.delete(client)`, пока по
    # ним ещё можно спросить базу.
    vlozheniya_zadach = purge_repo.task_files_of_doomed_clients(db, cutoff)

    for client in clients:
        if client.id in with_revenue:
            kept_with_revenue += 1
            continue

        removed_deals += int(deals_of.get(client.id) or 0)
        for file in files_of.get(client.id, ()):
            path = client_service.file_path_on_disk(file)
            if path.exists():
                freed_bytes += path.stat().st_size
            removed_files += 1
            if not dry_run:
                path.unlink(missing_ok=True)
        removed_clients += 1
        if not dry_run:
            db.delete(client)  # notes и files уходят каскадом

    for vlozhenie in vlozheniya_zadach:
        path = task_service.file_path_on_disk(vlozhenie)
        if path.exists():
            freed_bytes += path.stat().st_size
        removed_task_files += 1
        if not dry_run:
            path.unlink(missing_ok=True)

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
        "task_files": removed_task_files,
        "deals": removed_deals,
        "clients_kept_with_revenue": kept_with_revenue,
        "dry_run": dry_run,
    }
