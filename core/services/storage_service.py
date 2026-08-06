"""Контроль свободного места на сервере.

Работает без прав администратора: `shutil.disk_usage` под POSIX читает statvfs,
где `free` — это f_bavail, то есть место, доступное именно непривилегированному
процессу (у ext4 часть блоков зарезервирована за root и www-data их не получит).
Одинаково ведёт себя на Ubuntu и Fedora, дополнительных утилит не требует.
"""

import shutil
import time
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import get_settings
from core.services import media_service
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

# полный обход каталога с медиа стоит дорого — держим результат недолго в памяти
_SIZE_CACHE_TTL = 60.0
_size_cache: dict[str, tuple[float, int]] = {}


def dir_size(path: Path, cache_key: str | None = None) -> int:
    if cache_key:
        cached = _size_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _SIZE_CACHE_TTL:
            return cached[1]
    total = 0
    if path.exists():
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue  # файл могли удалить во время обхода
    if cache_key:
        _size_cache[cache_key] = (time.monotonic(), total)
    return total


def invalidate_size_cache() -> None:
    _size_cache.clear()


def disk_usage() -> dict:
    """Место на разделе, где лежит storage. Без прав root."""
    settings = get_settings()
    target = settings.storage_dir
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    # total включает зарезервированные за root блоки, поэтому used + free < total
    percent_used = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent_used": percent_used,
    }


def level_for(free_bytes: int, percent_used: float) -> str:
    settings = get_settings()
    if (
        percent_used >= settings.disk_critical_percent
        or free_bytes <= settings.disk_min_free_mb * 1024 * 1024
    ):
        return LEVEL_CRITICAL
    if percent_used >= settings.disk_warning_percent:
        return LEVEL_WARNING
    return LEVEL_OK


def reclaimable(db: Session) -> dict:
    """Сколько занимают мягко удалённые доски и клиенты — это можно освободить."""
    # Всё спрашивается наперёд, по разу на весь список. Запрос на каждую доску и
    # на каждого клиента давал здесь по обращению к базе на строку корзины — а
    # заглядывают сюда ровно тогда, когда корзина большая.
    boards = boards_repo.deleted_boards(db)
    works = boards_repo.works_by_board(db, [board.id for board in boards])
    works_count = 0
    board_bytes = 0
    for board_works in works.values():
        for work in board_works:
            works_count += 1
            board_bytes += dir_size(media_service.work_dir(work.work_uid))

    clients = clients_repo.deleted(db)
    files = clients_repo.files_of(db, [client.id for client in clients])
    files_count = len(files)
    client_bytes = sum(file.size_bytes or 0 for file in files)

    return {
        "bytes": board_bytes + client_bytes,
        "boards": len(boards),
        "works": works_count,
        "clients": len(clients),
        "client_files": files_count,
    }


def status(db: Session | None = None) -> dict:
    """Сводка для дашборда, сайдбара и настроек."""
    settings = get_settings()
    usage = disk_usage()
    data = {
        **usage,
        "level": level_for(usage["free_bytes"], usage["percent_used"]),
        "warning_percent": settings.disk_warning_percent,
        "critical_percent": settings.disk_critical_percent,
        "min_free_mb": settings.disk_min_free_mb,
        "storage_bytes": dir_size(settings.storage_dir, cache_key="storage"),
        "uploads_blocked": not has_room_for(0),
    }
    if db is not None:
        data["reclaimable"] = reclaimable(db)
    return data


def has_room_for(incoming_bytes: int) -> bool:
    """Хватит ли места на файл с сохранением аварийного запаса."""
    settings = get_settings()
    reserve = settings.disk_min_free_mb * 1024 * 1024
    return disk_usage()["free_bytes"] - incoming_bytes > reserve


def format_bytes(value: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
