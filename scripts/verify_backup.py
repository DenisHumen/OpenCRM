"""Годна ли резервная копия к восстановлению. Ничего не восстанавливает.

**Зачем это существует.** Копии, которые никто не проверял, — это не копии, а
надежда. Обычная история: скрипт год пишет файлы, диск заполняется, `sqlite3
.backup` начинает падать на полпути, а узнают об этом в день, когда база
понадобилась. Проверка стоит секунду и превращает «надеюсь, есть копия» в
«копия открывается, в ней столько-то клиентов».

Проверяем ровно то, из-за чего копия оказывается негодной:

1. **База открывается и цела** — `PRAGMA integrity_check`. Оборванный на
   середине `.backup` даёт файл, который выглядит как база, а читается до
   первой битой страницы.
2. **Схема отмечена миграцией.** База без `alembic_version` не поднимется
   новым кодом: приложение не знает, чем её доводить (см.
   `database/schema_check.py`).
3. **Данные на месте.** Пустая копия — самый коварный случай: файл есть, размер
   правдоподобный, а внутри одни пустые таблицы.
4. **Архив storage читается** — `tar -tzf` по списку, без распаковки.
5. **Ключ шифрования сохранён.** Без `OPENCRM_SECRET_KEY` пароли почтовых
   ящиков в восстановленной базе не расшифровать НИКОГДА: ключ не выводится из
   данных, и потеря его необратима.

Запускается сразу после снятия копии (`scripts/backup.sh`) и отдельно — рукой
или из `./opencrm.sh doctor`.

Код возврата: 0 — копия годна, 1 — нет. Отчёт печатается всегда и кладётся
рядом с копиями в `last-check.json`, чтобы «когда проверяли в последний раз»
имело ответ.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Таблицы, пустота которых означает, что копия бесполезна.
#:
#: Ровно две: без пользователей в систему не войти вообще, без настроек она не
#: та, что была. Проверять «клиенты не пусты» нельзя — свежая установка
#: законно пуста, и такая проверка ругалась бы на верную копию.
MUST_HAVE_ROWS = ("users", "site_settings")

#: Что осмысленно показать человеку: «в копии 48 клиентов» отвечает на вопрос
#: «та ли это копия» лучше, чем размер файла.
#:
#: Таблицы из `MUST_HAVE_ROWS` обязаны быть здесь же: проверка «не пусто»
#: смотрит именно в эти счётчики, и таблица, которую забыли посчитать,
#: объявлялась бы пустой всегда. Так и вышло с `site_settings` на первом же
#: прогоне — поэтому список собирается, а не набирается руками во второй раз.
COUNT_TABLES = tuple(dict.fromkeys(
    MUST_HAVE_ROWS + (
        "clients", "deals", "documents", "products", "stock_moves",
        "warehouses", "boards", "document_lines",
    )
))


def verify(db_path: Path, storage_path: Path | None, secret_path: Path | None) -> dict:
    report: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": str(db_path),
        "ok": True,
        "problems": [],
        "counts": {},
    }

    def fail(message: str) -> None:
        report["ok"] = False
        report["problems"].append(message)

    if not db_path.is_file():
        fail(f"файла базы нет: {db_path}")
        return report
    report["size"] = db_path.stat().st_size

    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        fail(f"база не открывается: {error}")
        return report

    try:
        # Оборванная копия роняет не `connect`, а первый же запрос: SQLite
        # открывает файл лениво и спотыкается о битую страницу уже при чтении.
        # Поймано первым прогоном — проверка падала с трассировкой вместо того,
        # чтобы сказать «копия негодна», а падение в скрипте бэкапа читается
        # как поломка бэкапа, а не как приговор копии.
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            fail(f"целостность: {integrity}")

        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "alembic_version" not in tables:
            fail("копия не отмечена миграцией — новый код не будет знать, чем её доводить")
        else:
            report["revision"] = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]

        for table in COUNT_TABLES:
            if table in tables:
                report["counts"][table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        for table in MUST_HAVE_ROWS:
            if table not in tables:
                fail(f"в копии нет таблицы {table}")
            elif not report["counts"].get(table):
                # Файл есть, размер правдоподобный, внутри пусто — самый
                # коварный случай: такую копию восстанавливают и обнаруживают,
                # что войти в систему некому.
                fail(f"таблица {table} пуста — копия бесполезна")
    except sqlite3.Error as error:
        fail(f"база не читается: {error}")
    finally:
        db.close()

    if storage_path is not None:
        report["storage"] = str(storage_path)
        if not storage_path.is_file():
            fail(f"архива storage нет: {storage_path}")
        else:
            # Читаем оглавление, не распаковывая: оборванный архив падает уже
            # на нём, а место под распаковку может и не найтись.
            listed = subprocess.run(
                ["tar", "-tzf", str(storage_path)], capture_output=True, text=True
            )
            if listed.returncode != 0:
                fail(f"архив storage не читается: {listed.stderr.strip()[:200]}")
            else:
                report["storage_entries"] = len(listed.stdout.splitlines())

    # Ключ шифрования. Его потеря необратима: пароли почтовых ящиков в
    # восстановленной базе не расшифровать никогда — ключ не выводится из
    # данных (см. `core/security/secretbox.py`).
    if secret_path is not None:
        report["secret"] = str(secret_path)
        if not secret_path.is_file():
            fail(
                "в копии нет ключа шифрования — восстановить получится, "
                "но пароли ящиков будут потеряны навсегда"
            )
        elif "OPENCRM_SECRET_KEY=" not in secret_path.read_text(encoding="utf-8"):
            fail("файл ключа есть, но самого ключа в нём нет")

    return report


def main(argv: list[str]) -> int:
    if not argv:
        print("использование: verify_backup.py <db> [storage.tar.gz] [secret.env]")
        return 2

    db_path = Path(argv[0])
    storage = Path(argv[1]) if len(argv) > 1 else None
    secret = Path(argv[2]) if len(argv) > 2 else None

    report = verify(db_path, storage, secret)

    # Отчёт кладём рядом с копиями: вопрос «когда проверяли в последний раз»
    # задают тогда, когда копия уже понадобилась, и ответ должен лежать на
    # диске, а не в чьей-то памяти.
    try:
        (db_path.parent.parent / "last-check.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        # Некуда записать — не повод объявить годную копию негодной.
        pass

    counts = ", ".join(f"{k}: {v}" for k, v in report["counts"].items() if v)
    if report["ok"]:
        print(f"копия годна · {counts or 'пусто'}")
        return 0
    print("КОПИЯ НЕГОДНА:")
    for problem in report["problems"]:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
