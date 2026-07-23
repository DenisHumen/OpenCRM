"""Перенос данных SQLite → MySQL (см. docs/03-database.md).

Порядок:
  1. Поднять MySQL, создать пустую БД (utf8mb4).
  2. Прогнать миграции на неё:
       OPENCRM_DB_URL=mysql+pymysql://user:pass@host/opencrm python -m alembic upgrade head
  3. Перенести данные:
       python scripts/migrate_to_mysql.py --source sqlite:///data/opencrm.db \
           --target mysql+pymysql://user:pass@host/opencrm
  4. Переключить OPENCRM_DB_URL приложения на MySQL и перезапустить.

Файлы в storage/ не затрагиваются — пути в БД относительные.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, insert, select  # noqa: E402

from database import models  # noqa: E402,F401 — регистрирует таблицы
from database.session import Base  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="URL исходной БД (SQLite)")
    parser.add_argument("--target", required=True, help="URL целевой БД (MySQL)")
    args = parser.parse_args()

    source = create_engine(args.source)
    target = create_engine(args.target)

    # sorted_tables — в порядке зависимостей FK
    with source.connect() as src, target.begin() as dst:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row._mapping) for row in src.execute(select(table))]
            if not rows:
                print(f"{table.name}: пусто")
                continue
            dst.execute(insert(table), rows)
            print(f"{table.name}: перенесено {len(rows)}")

    print("Готово. Проверьте вход, пару карточек клиентов и публичную ссылку.")


if __name__ == "__main__":
    main()
