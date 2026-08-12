"""Копия базы перед миграциями — правило номер один для боевого сервера.

`docker/entrypoint.sh` до старта приложения снимает копию и гонит
`alembic upgrade head`. Копия — это то, к чему возвращаются: обновление, не
дождавшись `/healthz`, откатывает и код, и базу (`deploy/updater.py`). Без копии
откатывать нечего, и правило номер один держится на честном слове.

**Найдено на стенде.** Снимок снимался ВНУТРИ проверки «файл базы существует»,
то есть только для SQLite. На установке, работающей на MySQL, в журнале запуска
видно старую копию SQLite-файла и следом миграции по MySQL: ни `mysqldump`, ни
новых копий нет вовсе. Неудачное обновление откатывало код и оставляло базу
такой, какой её сделала полупрошедшая миграция.

Здесь проверяется точка входа целиком — настоящим `sh`, с подставными `python` и
`sqlite3`. Двойников для оболочки не бывает: беда в этом файле будет не
логической, а шелловой (кавычка, `set -e` на ровном месте, `if`, закрывший не
ту ветку), и видно её только настоящему интерпретатору.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="нужен POSIX sh")

#: Ревизия, которую «находит» подставной клиент в базе. Имя копии строится по
#: ней — так повторный старт на той же ревизии ничего не переписывает.
REVIZIA = "b2c8e4f1a396"


def _polozhit(put: Path, soderzhimoe: str) -> None:
    put.write_text(soderzhimoe, encoding="utf-8", newline="\n")
    put.chmod(put.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stend(tmp_path: Path, *, db_url: str, sqlite_fayl: bool) -> dict:
    """Каталог с подставными python/sqlite3 и следом их вызовов."""
    dannye = tmp_path / "data"
    dannye.mkdir()
    hranilishche = tmp_path / "storage"
    (hranilishche / "branding").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sled = tmp_path / "sled.txt"

    if sqlite_fayl:
        (dannye / "opencrm.db").write_text("не настоящая база", encoding="utf-8")

    # python: пишет в след свои аргументы, отвечает ревизией и «снимает» дамп,
    # создавая названный файл. Настоящий дамп здесь не нужен — проверяется
    # обвязка; сам дамп проверяется своими тестами и живым прогоном.
    _polozhit(
        bin_dir / "python",
        "#!/bin/sh\n"
        f'echo "python $*" >> "{sled.as_posix()}"\n'
        'case "$3" in\n'
        f"    revision) echo {REVIZIA} ;;\n"
        '    dump) echo "дамп" > "$4" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    # sqlite3: отдаёт ревизию и «снимает» копию, создавая файл.
    _polozhit(
        bin_dir / "sqlite3",
        "#!/bin/sh\n"
        f'echo "sqlite3 $*" >> "{sled.as_posix()}"\n'
        "case \"$2\" in\n"
        f"    *alembic_version*) echo {REVIZIA} ;;\n"
        "    .backup*)\n"
        "        put=$(echo \"$2\" | sed \"s/^.backup '//;s/'$//\")\n"
        "        echo копия > \"$put\"\n"
        "        ;;\n"
        "esac\n"
        "exit 0\n",
    )

    okruzhenie = dict(os.environ)
    okruzhenie.update(
        {
            "PATH": bin_dir.as_posix() + os.pathsep + os.environ.get("PATH", ""),
            "OPENCRM_DB_FILE": (dannye / "opencrm.db").as_posix(),
            "OPENCRM_STORAGE_DIR": hranilishche.as_posix(),
            "OPENCRM_DB_URL": db_url,
            "OPENCRM_WORKERS": "1",
            "OPENCRM_REDIS_URL": "redis://localhost:6379/0",
        }
    )
    return {"env": okruzhenie, "data": dannye, "sled": sled}


def _zapustit(stend: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", ENTRYPOINT.as_posix()],
        env=stend["env"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _kopii(stend: dict) -> list[str]:
    return sorted(p.name for p in stend["data"].iterdir() if "pre-migrate" in p.name)


MYSQL_URL = "mysql+pymysql://opencrm:parol@db:3306/opencrm?charset=utf8mb4"


@needs_sh
def test_na_mysql_kopiya_snimaetsya(tmp_path):
    """Главная проверка находки: на MySQL копии не было вовсе."""
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    kopii = _kopii(stend)
    assert kopii, (
        "на MySQL перед миграциями не снято ни одной копии — откатывать "
        "неудачное обновление нечем"
    )
    assert any(REVIZIA in imya for imya in kopii), (
        f"копия названа не по ревизии, с которой уходим: {kopii}"
    )


@needs_sh
def test_kopiya_mysql_snimaetsya_do_migracij(tmp_path):
    """Копия ПОСЛЕ миграций — это снимок уже испорченного состояния."""
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)

    _zapustit(stend)

    sled = stend["sled"].read_text(encoding="utf-8")
    snimok = sled.index("scripts.snapshot_db")
    migracii = sled.index("alembic upgrade head")
    assert snimok < migracii, "копия снимается уже после миграций"


@needs_sh
def test_povtornyy_start_ne_zatiraet_kopiyu(tmp_path):
    """То же правило, что у SQLite, и по той же причине.

    Копия, обновляемая на каждом старте, затирается перезапуском контейнера —
    в том числе тем перезапуском, который случается уже ПОСЛЕ неудачной
    миграции. К моменту, когда беду замечают, возвращаться уже не к чему.
    """
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)
    _zapustit(stend)
    kopii = _kopii(stend)
    assert len(kopii) == 1, kopii
    (stend["data"] / kopii[0]).write_text("ПЕРВАЯ КОПИЯ", encoding="utf-8")

    _zapustit(stend)

    assert _kopii(stend) == kopii, "повторный старт завёл вторую копию на той же ревизии"
    assert (stend["data"] / kopii[0]).read_text(encoding="utf-8") == "ПЕРВАЯ КОПИЯ", (
        "повторный старт затёр копию, снятую до миграции"
    )


@needs_sh
def test_neudachnaya_kopiya_ostanavlivaet_zapusk(tmp_path):
    """Без копии миграции необратимы, поэтому идти дальше нельзя.

    Молча пропустить неудавшийся снимок — значит получить обновление, которое
    выглядит обычным, а откатить его нечем. Причина при этом обязана попасть на
    страницу обслуживания: контейнер в этот момент единственный, кто её знает.
    """
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)
    # python, который не смог снять дамп.
    _polozhit(
        Path(stend["env"]["PATH"].split(os.pathsep)[0]) / "python",
        "#!/bin/sh\n"
        'case "$3" in\n'
        f"    revision) echo {REVIZIA} ;;\n"
        "    dump) echo 'нет доступа к базе' >&2; exit 3 ;;\n"
        "esac\n"
        "exit 0\n",
    )

    itog = _zapustit(stend)

    assert itog.returncode != 0, "снимок не снялся, а запуск поехал дальше"
    sostoyanie = (
        Path(stend["env"]["OPENCRM_STORAGE_DIR"]) / "branding" / "update-state.json"
    ).read_text(encoding="utf-8")
    assert '"phase":"failed"' in sostoyanie, sostoyanie


@needs_sh
def test_na_sqlite_vsyo_kak_bylo(tmp_path):
    """Парная проверка: чинили MySQL — не сломали SQLite.

    Без неё «починку» легко доделать до того, что копия на SQLite перестанет
    сниматься вовсе, и правило номер один переедет с одной базы на другую.
    """
    stend = _stend(tmp_path, db_url="sqlite:////app/data/opencrm.db", sqlite_fayl=True)

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert _kopii(stend) == [f"opencrm.db.pre-migrate-{REVIZIA}"], _kopii(stend)
    sled = stend["sled"].read_text(encoding="utf-8")
    assert ".backup" in sled, "копия SQLite снимается больше не через .backup"
    assert "scripts.snapshot_db" not in sled, (
        "на SQLite зовётся дампер MySQL — лишний путь, который никто не проверит"
    )
