"""Годна ли резервная копия к восстановлению. Ничего не восстанавливает.

**Зачем это существует.** Копии, которые никто не проверял, — это не копии, а
надежда. Обычная история: скрипт год пишет файлы, диск заполняется, дамп
начинает обрываться на полпути, а узнают об этом в день, когда база
понадобилась. Проверка стоит секунду и превращает «надеюсь, есть копия» в
«копия открывается, в ней столько-то клиентов».

Копия одного вида — дамп `db-ГГГГ-ММ-ДД.sql`. Файл от прежней установки может
ещё лежать в том же каталоге, и он называется негодным: читать чужой формат
так, будто он свой, — способ узнать правду в самый неподходящий день.

Проверяем ровно то, из-за чего копия оказывается негодной:

1. **Копия дописана до конца.** Признак у дампа один и плохо заметный: это
   обычный текст, и оборванный ничем не отличается от целого, кроме
   отсутствующего хвоста `-- Dump completed`. Залитый до места обрыва, он
   оставит половину таблиц.
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
import re
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

#: Хвост, которым mysqldump заканчивает работу. Его отсутствие — единственный
#: признак оборванного дампа: текстовый файл, обрезанный на полуслове, читается
#: и выглядит совершенно обычно.
#:
#: Строка появляется, пока дамп снимается с комментариями (по умолчанию);
#: `--skip-comments` её убирает, поэтому в scripts/backup.sh этого флага нет и
#: быть не должно.
KHVOST_DUMPA = "-- Dump completed"

#: `CREATE TABLE \`users\` (` — имя таблицы в дампе всегда в обратных кавычках.
_SOZDANIE_TABLICY = re.compile(r"^CREATE TABLE `([^`]+)`", re.IGNORECASE)
#: `INSERT INTO \`users\` VALUES (...),(...);` — а с `--complete-insert` между
#: именем таблицы и словом VALUES стоит ещё и список столбцов.
_VSTAVKA = re.compile(r"^INSERT INTO `([^`]+)`.*?\bVALUES\b", re.IGNORECASE)


class _SchyotchikVstavki:
    """Считает строки в одном операторе `INSERT ... VALUES (..),(..);`.

    Считаем скобки, а не запятые: значения — это текст заметок и адресов, и
    `),(` внутри такого текста встречается ровно тогда, когда меньше всего
    ждёшь. Внутри строкового литерала скобки не считаются вовсе, экранирование
    `\\'` учитывается — mysqldump экранирует кавычки именно так.

    Кормить можно по кусочку, и это не про удобство. **Оператор INSERT не
    обязан помещаться в одну строку файла.** mysqldump от Oracle пишет его
    одной строкой, а mariadb-dump (именно он ставится на Debian и Ubuntu под
    именем `mysqldump`) переносит значения на следующие строки. Поймано живьём:
    построчный разбор насчитал ноль строк во всех таблицах и объявил заведомо
    годную копию негодной — а ложная тревога про копии почти так же вредна, как
    молчание, потому что после неё проверке перестают верить.
    """

    def __init__(self) -> None:
        self.stroki = 0
        self.zakonchen = False
        self._glubina = 0
        self._v_stroke = False
        self._ekran = False

    def dobavit(self, kusok: str) -> None:
        for znak in kusok:
            if self._ekran:
                self._ekran = False
            elif self._v_stroke:
                if znak == "\\":
                    self._ekran = True
                elif znak == "'":
                    self._v_stroke = False
            elif znak == "'":
                self._v_stroke = True
            elif znak == "(":
                self._glubina += 1
            elif znak == ")":
                self._glubina -= 1
                if self._glubina == 0:
                    self.stroki += 1
            elif znak == ";" and self._glubina == 0:
                # Точка с запятой вне строки и вне кортежа — конец оператора.
                self.zakonchen = True
                return


def _prochitat_dump(path: Path) -> tuple[set[str], dict[str, int], str | None, str]:
    """Разбирает дамп: какие таблицы есть, сколько в них строк, ревизия, хвост.

    Читаем построчно, а не целиком: дамп рабочей базы — это сотни мегабайт, и
    проверка копии не имеет права требовать под себя столько же памяти.
    """
    tablicy: set[str] = set()
    stroki: dict[str, int] = {}
    revizia: str | None = None
    poslednyaya = ""

    # Оператор INSERT, который сейчас разбираем: его имя, счётчик и — только для
    # alembic_version — накопленный текст. Копить текст всех вставок подряд
    # значило бы прочитать дамп в память целиком, ради чего всё это и не делается.
    imya: str | None = None
    schyotchik: _SchyotchikVstavki | None = None
    otmetka_revizii: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as fayl:
        for stroka in fayl:
            stroka = stroka.rstrip("\n")
            if stroka.strip():
                poslednyaya = stroka

            if imya is None:
                sozdanie = _SOZDANIE_TABLICY.match(stroka)
                if sozdanie:
                    tablicy.add(sozdanie.group(1))
                    continue
                vstavka = _VSTAVKA.match(stroka)
                if not vstavka:
                    continue
                imya = vstavka.group(1)
                # Вставка бывает и в таблицу, чьего CREATE TABLE в дампе нет
                # (частичный дамп); тогда таблица всё равно считается имеющейся.
                tablicy.add(imya)
                schyotchik = _SchyotchikVstavki()
                kusok = stroka[vstavka.end():]
            else:
                kusok = stroka

            assert schyotchik is not None
            schyotchik.dobavit(kusok)
            if imya == "alembic_version":
                otmetka_revizii.append(kusok)

            if schyotchik.zakonchen:
                stroki[imya] = stroki.get(imya, 0) + schyotchik.stroki
                if imya == "alembic_version" and revizia is None:
                    nayden = re.search(r"\('([^']+)'\)", "".join(otmetka_revizii))
                    if nayden:
                        revizia = nayden.group(1)
                imya = None
                schyotchik = None

    # Оператор, оборвавшийся на середине файла, до сюда не досчитан — и это
    # верно: строки, которых в файле нет, восстановлению не помогут.
    if imya is not None and schyotchik is not None:
        stroki[imya] = stroki.get(imya, 0) + schyotchik.stroki

    return tablicy, stroki, revizia, poslednyaya


def _proverit_dump(db_path: Path, report: dict, fail) -> None:
    """Дамп базы: дочитан ли до конца, отмечен ли миграцией, есть ли данные."""
    tablicy, stroki, revizia, poslednyaya = _prochitat_dump(db_path)

    if KHVOST_DUMPA not in poslednyaya:
        # Оборванный дамп — обычный текстовый файл: он открывается, читается и
        # выглядит целым. Единственное, чем он себя выдаёт, — отсутствие хвоста,
        # который mysqldump дописывает последним действием.
        fail("дамп не дописан до конца — снятие копии оборвалось")

    if "alembic_version" not in tablicy:
        fail("копия не отмечена миграцией — новый код не будет знать, чем её доводить")
    elif revizia is None:
        fail("таблица alembic_version в дампе есть, а ревизии в ней нет")
    else:
        report["revision"] = revizia

    for table in COUNT_TABLES:
        if table in tablicy:
            report["counts"][table] = stroki.get(table, 0)

    for table in MUST_HAVE_ROWS:
        if table not in tablicy:
            fail(f"в копии нет таблицы {table}")
        elif not report["counts"].get(table):
            # Дамп со схемой, но без данных — ровно то, что получается при
            # `--no-data` или при дампе не той базы. Восстанавливают такую копию
            # и обнаруживают, что войти в систему некому.
            fail(f"таблица {table} пуста — копия бесполезна")


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

    # Вид копии — по расширению, которое ставит scripts/backup.sh. Заглядывать
    # внутрь незачем: имя файла здесь и есть решение, принятое при снятии.
    #
    # Чужой формат называем негодным, а не читаем «как получится»: файл от
    # прежней установки лежит в том же каталоге, и выбрать его можно по ошибке.
    if db_path.suffix.lower() != ".sql":
        report["engine"] = "unknown"
        fail(f"{db_path.name}: это не дамп базы — копии называются db-ГГГГ-ММ-ДД.sql")
        return report
    report["engine"] = "mysql"
    _proverit_dump(db_path, report, fail)

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
        print("использование: verify_backup.py <db|dump.sql> [storage.tar.gz] [secret.env]")
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
        print(f"копия годна · {report.get('engine', '?')} · {counts or 'пусто'}")
        return 0
    print("КОПИЯ НЕГОДНА:")
    for problem in report["problems"]:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
