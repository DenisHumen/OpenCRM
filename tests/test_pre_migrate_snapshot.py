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

#: Голова миграций по умолчанию — ДРУГАЯ ревизия, то есть накатывать есть что.
#: Это обычное состояние старта после обновления, и копия в нём обязательна.
GOLOVA = "d4e1a83c2f60"


def _polozhit(put: Path, soderzhimoe: str) -> None:
    put.write_text(soderzhimoe, encoding="utf-8", newline="\n")
    put.chmod(put.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stend(tmp_path: Path, *, db_url: str, sqlite_fayl: bool, golova: str = GOLOVA) -> dict:
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
        # `python -m alembic heads` — этим точка входа спрашивает, есть ли что
        # накатывать. Голова, совпавшая с ревизией базы, означает «нечего».
        f"    heads) echo '{golova} (head)' ;;\n"
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


# --- копия нужна ПЕРЕД МИГРАЦИЯМИ, а не на каждом старте ----------------------
#
# РАЗМЕН, названный вслух и решённый здесь. Копия стоит дорого: замерено на
# живой MySQL — 2,7 млн строк это 84,86 с и дамп на 1,2 ГБ, полный старт до
# первого 200 на `/healthz` — 89,55 с. Против этого стоит потолок ожидания у
# автообновления: 30 попыток × 4 с = 120 с, из которых копия забирает 85. Порог,
# за которым ИСПРАВНОЕ обновление начнёт откатываться само, — около 3,6 млн
# строк на быстром железе; на боевом VPS с медленным диском он ближе.
#
# Вторая половина того же размена: нехватка места ТЕПЕРЬ ГАСИТ САЙТ ВОВСЕ.
# Проверено на каталоге данных в 20 МБ — дамп падает с ENOSPC, точка входа
# выходит с кодом 1, uvicorn не стартует ни разу. До появления копии на MySQL
# нехватка места подняться не мешала. А забить диск проще всего этими самыми
# копиями.
#
# Лекарство из самого устройства: копия нужна ПЕРЕД МИГРАЦИЯМИ. Ревизия базы
# уже голова — накатывать нечего, портить нечему, возвращаться не к чему.
# Обычный перезапуск контейнера перестаёт стоить полутора минут и гигабайта, а
# копия снимается ровно на том старте, ради которого её и заводили.


@needs_sh
def test_bez_migraciy_kopiya_ne_snimaetsya(tmp_path):
    """Ревизия уже голова — накатывать нечего, и копия не нужна."""
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False, golova=REVIZIA)

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert _kopii(stend) == [], "копия снята там, где мигрировать нечего"
    assert "scripts.snapshot_db dump" not in stend["sled"].read_text(encoding="utf-8")
    assert "миграций к накату нет" in itog.stdout, itog.stdout


@needs_sh
def test_bez_migraciy_kopiya_ne_snimaetsya_i_na_sqlite(tmp_path):
    """Правило одно на обе базы, иначе оно не правило."""
    stend = _stend(
        tmp_path, db_url="sqlite:////app/data/opencrm.db", sqlite_fayl=True, golova=REVIZIA
    )

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert _kopii(stend) == [], "копия снята там, где мигрировать нечего"
    assert ".backup" not in stend["sled"].read_text(encoding="utf-8")


@needs_sh
def test_est_chto_nakatyvat_kopiya_snimaetsya(tmp_path):
    """Парная проверка: «дешевле» не значит «без копии».

    Без неё починку легко доделать до того, что копия перестанет сниматься
    вовсе, и правило номер один держалось бы на честном слове.
    """
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False, golova="sovsem-drugaya")

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert _kopii(stend), "накатывать есть что, а копии нет — откатывать нечем"


@needs_sh
def test_nevnyatnaya_golova_ne_otmenyaet_kopiyu(tmp_path):
    """Не смогли узнать голову — снимаем копию, как раньше.

    Незнание не повод пропустить копию: цена ошибки здесь несимметрична —
    лишняя копия стоит места, отсутствующая стоит данных.
    """
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)
    # alembic, который ничего не ответил (нет каталога версий, чужая ошибка).
    _polozhit(
        Path(stend["env"]["PATH"].split(os.pathsep)[0]) / "python",
        "#!/bin/sh\n"
        'case "$3" in\n'
        f"    revision) echo {REVIZIA} ;;\n"
        "    heads) exit 1 ;;\n"
        '    dump) echo "дамп" > "$4" ;;\n'
        "esac\n"
        "exit 0\n",
    )

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert _kopii(stend), "голову прочитать не смогли — а копию всё равно пропустили"


@needs_sh
def test_dve_golovy_ne_otmenyayut_kopiyu(tmp_path):
    """Незакрытое ветвление миграций: «уже голова» в таком дереве не определить."""
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)
    _polozhit(
        Path(stend["env"]["PATH"].split(os.pathsep)[0]) / "python",
        "#!/bin/sh\n"
        'case "$3" in\n'
        f"    revision) echo {REVIZIA} ;;\n"
        f"    heads) printf '%s (head)\n%s (head)\n' {REVIZIA} drugaya ;;\n"
        '    dump) echo "дамп" > "$4" ;;\n'
        "esac\n"
        "exit 0\n",
    )

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert _kopii(stend), "при двух головах копию пропустили"


@needs_sh
def test_ogryzok_dampa_ot_proshloy_revizii_ubiraetsya(tmp_path):
    """Оборванный дамп прошлой ревизии не убирал никто.

    Чистка ищет `mysql.pre-migrate-*.sql` — под маску `.part` огрызок не
    попадает, а имя у него своё, так что следующий старт его и не перезапишет.
    Проверено: 200 МБ мусора пережили перезапуск контейнера.
    """
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)
    ogryzok = stend["data"] / "mysql.pre-migrate-starayarevizia.sql.part"
    ogryzok.write_text("оборванный дамп", encoding="utf-8")

    _zapustit(stend)

    assert not ogryzok.exists(), "огрызок прошлой ревизии лежит вечно и ест диск"


# --- метка конца копии названа в двух местах и обязана совпадать --------------


def test_metka_kopii_odna_i_ta_zhe_v_oboih_mestah():
    """Дамп признаётся годным по хвосту, и проверяют его двое.

    `scripts/snapshot_db.py` пишет метку и проверяет её сразу после снятия;
    `deploy/updater.py` проверяет ту же метку у копии, которую держит на хосте.
    Импортировать первый из второго нельзя — пакет `deploy` работает вне
    контейнера и не тянет код приложения, — поэтому строка удвоена нарочно.
    Разъехавшись, эти двое дали бы худшее: годная копия, объявленная негодной,
    и остановленное из-за этого обновление.
    """
    from deploy.updater import SNAPSHOT_MARK
    from scripts.snapshot_db import METKA

    assert METKA == SNAPSHOT_MARK, (
        "метка конца копии разъехалась между scripts/snapshot_db.py и deploy/updater.py"
    )


# --- гонка старта: приложение поднимается раньше базы -------------------------
#
# `depends_on: condition: service_healthy` действует только на `compose up`. Ни
# `docker restart`, ни перезагрузка машины, ни `restart: unless-stopped` его не
# соблюдают — контейнеры поднимаются разом.
#
# Поймано живьём на стенде с MySQL: приложение стартовало первым, получало
# `(2003, "Can't connect to MySQL server on 'db' (Connection refused)")`, и точка
# входа принимала это за «копию снять не удалось» — печатала «дальше идти
# нельзя» и выходила с кодом 1. Контейнер уходил в петлю перезапусков, сайт лежал
# всё это время. Обычная перезагрузка машины превращалась в аварию, и только на
# MySQL: у файловой базы такой гонки нет вовсе.


def _stend_s_ozhidaniem(tmp_path: Path, otvetit_s_popytki: int) -> dict:
    """Стенд, где `ping` отвечает не сразу: база «поднимается» не мгновенно."""
    stend = _stend(tmp_path, db_url=MYSQL_URL, sqlite_fayl=False)
    schyot = tmp_path / "ping.schyot"
    schyot.write_text("0", encoding="utf-8")
    _polozhit(
        Path(stend["env"]["PATH"].split(os.pathsep)[0]) / "python",
        "#!/bin/sh\n"
        f'echo "python $*" >> "{stend["sled"].as_posix()}"\n'
        'case "$3" in\n'
        "    ping)\n"
        f'        n=$(cat "{schyot.as_posix()}")\n'
        "        n=$((n + 1))\n"
        f'        echo "$n" > "{schyot.as_posix()}"\n'
        f"        [ \"$n\" -ge {otvetit_s_popytki} ] || exit 1\n"
        "        ;;\n"
        f"    revision) echo {REVIZIA} ;;\n"
        f"    heads) echo '{GOLOVA} (head)' ;;\n"
        '    dump) echo "дамп" > "$4" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    stend["schyot"] = schyot
    return stend


@needs_sh
def test_zhdyot_bazu_a_ne_umiraet(tmp_path):
    """База ответила с третьей попытки — старт обязан продолжиться, а не упасть."""
    stend = _stend_s_ozhidaniem(tmp_path, otvetit_s_popytki=3)

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert "жду, пока база начнёт отвечать" in itog.stdout, itog.stdout
    assert _kopii(stend), "копия не снялась — старт свернул не туда после ожидания"
    # И главное: «не удалось снять копию» не печаталось вовсе.
    assert "не удалось снять копию" not in itog.stdout + itog.stderr


@needs_sh
def test_bazu_sprashivayut_do_lyubykh_resheniy(tmp_path):
    """Порядок важен: ревизию спрашивают ПОСЛЕ того, как база ответила.

    Иначе `revision` падает на отказе соединения, `CURRENT` становится `none`,
    и копия снимается под неверным именем — на каждом таком старте заново.
    """
    stend = _stend_s_ozhidaniem(tmp_path, otvetit_s_popytki=2)

    _zapustit(stend)

    sled = stend["sled"].read_text(encoding="utf-8")
    assert sled.index("ping") < sled.index("revision"), (
        "ревизию спрашивают раньше, чем база ответила"
    )
    assert "snapshot_db revision" in sled


@needs_sh
def test_baza_kotoraya_ne_podnyalas_vovse_govorit_ob_etom(tmp_path):
    """Парная проверка: ждать вечно нельзя, молчать о лежащей базе — тем более."""
    stend = _stend_s_ozhidaniem(tmp_path, otvetit_s_popytki=999)
    stend["env"]["OPENCRM_DB_WAIT_TRIES"] = "2"

    itog = _zapustit(stend)

    assert itog.returncode == 1
    assert "база не ответила" in itog.stderr, itog.stderr
    assert "logs db" in itog.stderr, "человеку не сказано, куда смотреть"
    assert _kopii(stend) == [], "копия снималась при неотвечающей базе"


@needs_sh
def test_na_sqlite_ozhidaniya_net(tmp_path):
    """У файловой базы гонки нет вовсе — и ждать нечего.

    Лишнее ожидание на SQLite стоило бы двух секунд на каждом старте и
    появилось бы ровно там, где проблемы не существует.
    """
    stend = _stend(tmp_path, db_url="sqlite:////app/data/opencrm.db", sqlite_fayl=True)

    itog = _zapustit(stend)

    assert itog.returncode == 0, itog.stderr
    assert "жду, пока база" not in itog.stdout
    assert "snapshot_db ping" not in stend["sled"].read_text(encoding="utf-8")
