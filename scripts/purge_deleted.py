"""Окончательная очистка мягко удалённого (см. docs/03-database.md).

Мягкое удаление (deleted_at) защищает от случайностей; этот скрипт
безвозвратно удаляет записи и их файлы спустя карантин (по умолчанию 30 дней),
освобождая место на диске. Та же логика доступна root'у в «Настройках сайта».

Запуск (cron, раз в сутки):
    python scripts/purge_deleted.py [--days 30] [--dry-run]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.services import maintenance_service, storage_service  # noqa: E402
from database.session import get_session  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="карантин в днях")
    parser.add_argument("--dry-run", action="store_true", help="только показать, ничего не удалять")
    args = parser.parse_args()

    db = get_session()
    try:
        result = maintenance_service.purge_soft_deleted(
            db, older_than_days=args.days, dry_run=args.dry_run
        )
        if not args.dry_run:
            db.commit()
        prefix = "[dry-run] " if args.dry_run else ""
        print(
            f"{prefix}досок: {result['boards']} (работ: {result['works']}), "
            f"клиентов: {result['clients']} (файлов: {result['client_files']})"
        )
        print(f"{prefix}освобождено: {storage_service.format_bytes(result['freed_bytes'])}")

        usage = storage_service.disk_usage()
        print(
            f"диск: занято {usage['percent_used']}%, "
            f"свободно {storage_service.format_bytes(usage['free_bytes'])}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
