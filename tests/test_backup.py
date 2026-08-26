"""Резервные копии: годна ли копия и есть ли в ней то, без чего не восстановиться.

Копии, которые никто не проверял, — это не копии, а надежда. Обычная история:
скрипт год пишет файлы, диск заполняется, `.backup` начинает обрываться на
полпути, а узнают об этом в день, когда база понадобилась.

Проверки ниже — про две беды, каждая из которых делает копию бесполезной молча:
**копия негодна** (оборвана, пуста, не отмечена миграцией) и **в копии нет
ключа шифрования** — а без него пароли почтовых ящиков не расшифровать никогда.

Копия бывает двух видов — файл SQLite и дамп MySQL, — и обе половины набора
устроены одинаково нарочно: система, переехавшая на MySQL, не имеет права
молча остаться с копиями, о годности которых никто не спрашивал.
"""

import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from scripts import verify_backup
from scripts.snapshot_db import METKA

#: Скрипты — POSIX sh, и проверять их иначе как запуском бессмысленно: беды,
#: найденные на стенде, все до одной были в поведении, а не в тексте.
nuzhen_sh = pytest.mark.skipif(
    os.name != "posix" or shutil.which("sh") is None, reason="нужен POSIX sh"
)


# Значения нарочно такие, на каких разваливается наивный подсчёт строк: скобки
# внутри текста, `),(` внутри текста, экранированный апостроф и эмодзи. Все
# четыре взяты из настоящего дампа, снятого с живого MySQL 8.0.46.
KOVARNYE_NASTROYKI = (
    (1, "currency", "RUB"),
    (2, "brand_note", "скобки внутри: (a),(b),(c) — и это одно значение"),
    (3, "quote_note", "апостроф O\\'Rourke и ещё ),( внутри"),
    (4, "emoji_note", "витрина 🌿 работает"),
)


NL = chr(10)

def good_dump(path: Path, users: int = 1, mnogostrochnyy: bool = False) -> Path:
    """Правдоподобный дамп MySQL — такой, какой выдаёт mysqldump.

    `mnogostrochnyy` — это форма от mariadb-dump: слово VALUES остаётся в конце
    строки, а сами значения переносятся на следующие. Именно так пишет клиент,
    который ставится на Debian и Ubuntu под именем `mysqldump`.
    """

    def vstavka(tablica: str, kortezhi: list[str]) -> str:
        if mnogostrochnyy:
            return f"INSERT INTO `{tablica}` VALUES\n" + ",\n".join(kortezhi) + ";"
        return f"INSERT INTO `{tablica}` VALUES " + ",".join(kortezhi) + ";"

    chasti = [
        "-- MySQL dump 10.13  Distrib 8.0.46, for Linux (x86_64)",
        "--",
        "CREATE TABLE `alembic_version` (`version_num` varchar(32) NOT NULL);",
        vstavka("alembic_version", ["('c3d9f2a71b58')"]),
        "CREATE TABLE `users` (`id` int NOT NULL, `email` varchar(255) NOT NULL);",
    ]
    if users:
        chasti.append(vstavka(
            "users", [f"({n + 1},'u{n}@example.com')" for n in range(users)]
        ))
    chasti += [
        "CREATE TABLE `site_settings` (`id` int NOT NULL, `key` varchar(64) NOT NULL);",
        vstavka(
            "site_settings",
            [f"({i},'{k}','{v}')" for i, k, v in KOVARNYE_NASTROYKI],
        ),
        "",
        "-- Dump completed on 2026-08-07  8:03:37",
        "",
    ]
    path.write_text("\n".join(chasti), encoding="utf-8")
    return path


def good_storage(path: Path) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        inside = path.parent / "note.txt"
        inside.write_text("файл витрины", encoding="utf-8")
        archive.add(inside, arcname="note.txt")
    return path


def good_secret(path: Path) -> Path:
    path.write_text("OPENCRM_SECRET_KEY=abc\nOPENCRM_IP_HASH_SALT=def\n", encoding="utf-8")
    return path


# --- копия годна --------------------------------------------------------------


def test_godnaya_kopiya_prohodit(tmp_path):
    report = verify_backup.verify(
        good_dump(tmp_path / "db.sql", users=3),
        good_storage(tmp_path / "storage.tar.gz"),
        good_secret(tmp_path / "secret.env"),
    )
    assert report["ok"], report["problems"]
    assert report["revision"] == "c3d9f2a71b58"
    # Счётчики нужны человеку, а не проверке: «в копии 3 пользователя» отвечает
    # на вопрос «та ли это копия» лучше, чем размер файла.
    assert report["counts"]["users"] == 3
    assert report["storage_entries"] == 1


# --- копия негодна ------------------------------------------------------------


def test_oborvannaya_kopiya_zamechena(tmp_path):
    """Дамп, прерванный на полном диске, — обычный текстовый файл.

    От целого он не отличается ничем, кроме отсутствующего хвоста
    `-- Dump completed`. Залитый до места обрыва, он оставит половину таблиц.
    """
    path = good_dump(tmp_path / "torn.sql", users=400)
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])

    report = verify_backup.verify(path, None, None)
    assert not report["ok"], report
    assert any(
        "дописан" in p or "не дописан" in p or "хвост" in p or "конца" in p
        for p in report["problems"]
    ), report["problems"]


def test_pustaya_kopiya_zamechena(tmp_path):
    """Самый коварный случай: файл есть, размер правдоподобный, внутри пусто.

    Такую копию восстанавливают и обнаруживают, что войти в систему некому.
    """
    report = verify_backup.verify(good_dump(tmp_path / "empty.sql", users=0), None, None)
    assert not report["ok"]
    assert any("users" in p for p in report["problems"]), report["problems"]


def test_kopiya_bez_migratsii_zamechena(tmp_path):
    """База без `alembic_version` не поднимется новым кодом: он не знает, чем
    её доводить (см. `database/schema_check.py`)."""
    path = good_dump(tmp_path / "unstamped.sql")
    # Убираем отметку прямо из текста дампа — так и выглядит копия, снятая с
    # базы, которую миграции не касались.
    bez_otmetki = [
        s for s in path.read_text(encoding="utf-8").splitlines()
        if "alembic_version" not in s
    ]
    path.write_text(NL.join(bez_otmetki) + NL, encoding="utf-8")

    report = verify_backup.verify(path, None, None)
    assert not report["ok"]
    assert any("миграцией" in p for p in report["problems"]), report["problems"]


def test_bityy_arkhiv_zamechen(tmp_path):
    """Оглавление читаем без распаковки: место под неё может и не найтись."""
    broken = tmp_path / "storage.tar.gz"
    broken.write_text("это не архив", encoding="utf-8")
    report = verify_backup.verify(good_dump(tmp_path / "db.sql"), broken, None)
    assert not report["ok"]
    assert any("storage" in p for p in report["problems"]), report["problems"]


def test_propavshiy_fayl_zamechen(tmp_path):
    report = verify_backup.verify(tmp_path / "нет-такого.sql", None, None)
    assert not report["ok"]
    assert any("нет" in p for p in report["problems"])


# --- дамп MySQL: то же самое и по тем же причинам -----------------------------


def test_godnyy_dump_prohodit(tmp_path):
    report = verify_backup.verify(
        good_dump(tmp_path / "db.sql", users=3),
        good_storage(tmp_path / "storage.tar.gz"),
        good_secret(tmp_path / "secret.env"),
    )
    assert report["ok"], report["problems"]
    assert report["engine"] == "mysql"
    assert report["revision"] == "c3d9f2a71b58"
    assert report["counts"]["users"] == 3


def test_vid_kopii_opredelyaetsya_rasshireniem(tmp_path):
    """Файл SQLite и дамп проверяются по-разному, и перепутать их нельзя.

    Дамп, отданный на проверку как база SQLite, открылся бы как «не база» и
    объявил бы годную копию негодной; файл SQLite, прочитанный как текст, — то
    же самое наоборот.
    """
    assert verify_backup.verify(good_dump(tmp_path / "a.sql"), None, None)["engine"] == "mysql"
    assert verify_backup.verify(good_dump(tmp_path / "b.sql"), None, None)["engine"] == "mysql"


def test_oborvannyy_dump_zamechen(tmp_path):
    """Оборванный дамп — обычный текстовый файл, и это худшее в нём.

    Он открывается, читается и выглядит совершенно целым. Залитый до места
    обрыва, он оставит половину таблиц — а «наполовину восстановленная база»
    обнаруживается уже после того, как прежнюю затёрли.
    """
    path = good_dump(tmp_path / "torn.sql", users=400)
    whole = path.read_text(encoding="utf-8")
    path.write_text(whole[: len(whole) // 2], encoding="utf-8")

    report = verify_backup.verify(path, None, None)
    assert not report["ok"], report
    assert any("не дописан до конца" in p for p in report["problems"]), report["problems"]


def test_dump_bez_migratsii_zamechen(tmp_path):
    """База без `alembic_version` не поднимется новым кодом: он не знает, чем
    её доводить (см. `database/schema_check.py`)."""
    path = good_dump(tmp_path / "unstamped.sql")
    path.write_text(
        "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if "alembic_version" not in line
        ),
        encoding="utf-8",
    )
    report = verify_backup.verify(path, None, None)
    assert not report["ok"]
    assert any("миграцией" in p for p in report["problems"]), report["problems"]


def test_dump_so_skhemoy_bez_dannyh_zamechen(tmp_path):
    """Схема есть, данных нет — так выглядит дамп с `--no-data` или дамп не той
    базы. Восстанавливают такую копию и обнаруживают, что войти в систему
    некому."""
    report = verify_backup.verify(good_dump(tmp_path / "empty.sql", users=0), None, None)
    assert not report["ok"]
    assert any("users" in p for p in report["problems"]), report["problems"]


def test_mnogostrochnaya_vstavka_schitaetsya_verno(tmp_path):
    """Сторож на поломку, найденную живьём.

    Оператор INSERT не обязан помещаться в одну строку файла: mysqldump от
    Oracle пишет его одной строкой, а mariadb-dump (именно он ставится на
    Debian и Ubuntu под именем `mysqldump`) переносит значения на следующие.
    Построчный разбор насчитывал ноль строк во всех таблицах и объявлял
    заведомо годную копию негодной — а ложная тревога про копии почти так же
    вредна, как молчание: после неё проверке перестают верить.

    Обе формы обязаны давать один и тот же ответ.
    """
    odnoy = verify_backup.verify(good_dump(tmp_path / "odna.sql", users=5), None, None)
    mnogo = verify_backup.verify(
        good_dump(tmp_path / "mnogo.sql", users=5, mnogostrochnyy=True), None, None
    )
    assert mnogo["ok"], mnogo["problems"]
    assert mnogo["counts"] == odnoy["counts"] == {"users": 5, "site_settings": 4}
    assert mnogo["revision"] == odnoy["revision"] == "c3d9f2a71b58"


def test_skobki_i_apostrofy_v_znacheniyah_ne_sbivayut_schyot(tmp_path):
    """Строки считаются по скобкам, а не по запятым и не по `),(`.

    В значениях лежит текст заметок и адресов, и `),(` внутри такого текста
    встречается ровно тогда, когда меньше всего ждёшь: наивный подсчёт насчитал
    бы здесь семь настроек вместо четырёх. Ошибка в бо́льшую сторону не
    безобидна — именно на этих счётчиках держится проверка «таблица не пуста».
    """
    for mnogostrochnyy in (False, True):
        report = verify_backup.verify(
            good_dump(tmp_path / f"d{mnogostrochnyy}.sql", mnogostrochnyy=mnogostrochnyy),
            None, None,
        )
        assert report["counts"]["site_settings"] == len(KOVARNYE_NASTROYKI), report["counts"]


def test_negodnyy_dump_daet_nenulevoy_kod(tmp_path):
    """Отказ проверки обязан валить весь скрипт бэкапа — на MySQL так же, как
    и на SQLite."""
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    assert verify_backup.main([str(good_dump(daily / "db.sql", users=0))]) == 1
    otchyot = (tmp_path / "backups" / "last-check.json").read_text(encoding="utf-8")
    assert '"ok": false' in otchyot


# --- ключ шифрования ----------------------------------------------------------


def test_kopiya_bez_klyucha_zamechena(tmp_path):
    """Потеря ключа необратима.

    Пароли почтовых ящиков зашифрованы `OPENCRM_SECRET_KEY`, и он не выводится
    из данных (`core/security/secretbox.py`). Ключ, оставшийся только в
    config/.env на сгоревшем сервере, — это потеря навсегда, а не неудобство.
    """
    report = verify_backup.verify(
        good_dump(tmp_path / "db.sql"), None, tmp_path / "secret.env"
    )
    assert not report["ok"]
    assert any("ключ" in p for p in report["problems"]), report["problems"]


def test_pustoy_fayl_klyucha_zamechen(tmp_path):
    """Файл есть, а ключа в нём нет — так выглядит бэкап, снятый без переменных
    окружения. Проверка обязана отличать это от «ключ на месте»."""
    empty = tmp_path / "secret.env"
    empty.write_text("# ключей не досталось\n", encoding="utf-8")
    report = verify_backup.verify(good_dump(tmp_path / "db.sql"), None, empty)
    assert not report["ok"]
    assert any("ключа в нём нет" in p for p in report["problems"]), report["problems"]


# --- отчёт --------------------------------------------------------------------


def test_otchyot_ostayotsya_na_diske(tmp_path):
    """Вопрос «когда проверяли в последний раз» задают тогда, когда копия уже
    понадобилась, и ответ должен лежать на диске, а не в чьей-то памяти."""
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    code = verify_backup.main([str(good_dump(daily / "db-2026-08-07.sql"))])
    assert code == 0

    report = (tmp_path / "backups" / "last-check.json").read_text(encoding="utf-8")
    assert "checked_at" in report
    assert '"ok": true' in report


def test_negodnaya_kopiya_daet_nenulevoy_kod(tmp_path):
    """Отказ проверки обязан валить весь скрипт бэкапа.

    Копия, о негодности которой не сказали, хуже отсутствия копии: на неё
    рассчитывают.
    """
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    assert verify_backup.main([str(good_dump(daily / "db.sql", users=0))]) == 1


def _sh_operatory(text: str) -> str:
    """Скрипт без комментариев и со склеенными переносами строк.

    И то и другое — не придирки, а условие, без которого проверки этого файла
    ничего не стерегут. Комментарии здесь плотные и содержат ровно те слова,
    которые ищет проверка: убранный из команды `--single-transaction` остался бы
    «найден» в пояснении к нему. А перенос строки обратной косой разрывает одну
    команду на несколько, и `mysqldump ... | gzip` выглядел бы как две
    безобидные строки. Обе слабости найдены нарочной поломкой сторожей.
    """
    bez_kommentariev = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    return bez_kommentariev.replace("\\\n", " ")


def test_skript_bekapa_zovyot_proverku():
    """Сторож: проверка должна остаться частью снятия копии.

    Убрать вызов при чистке легко — выглядит он как лишний шаг, — а обнаружится
    пропажа в день, когда копия понадобилась.
    """
    operatory = _sh_operatory(Path("scripts/backup.sh").read_text(encoding="utf-8"))
    assert "verify_backup" in operatory, "бэкап перестал проверять сам себя"
    assert "OPENCRM_SECRET_KEY" in operatory, "ключ шифрования перестал попадать в копию"
    # Проверяется то, что действительно сняли, а не имя, собранное второй раз:
    # разойдясь, они проверяли бы вчерашнюю копию и хвалили бы сегодняшнюю.
    vyzov = operatory[operatory.index("python -m scripts.verify_backup"):]
    vyzov = vyzov[: vyzov.index("\n")]
    assert '"$DB_COPY"' in vyzov, f"проверка смотрит не на снятую копию: {vyzov}"


def test_skript_bekapa_snimaet_damp():
    """База одна, и способ снять копию один — дамп.

    Ветка про файл ушла вместе с самой SQLite. Проверка осталась ради того же:
    система не имеет права тихо остаться без копий вовсе.
    """
    operatory = _sh_operatory(Path("scripts/backup.sh").read_text(encoding="utf-8"))
    assert "mysqldump" in operatory, "база перестала попадать в копию"
    assert ".backup" not in operatory, "вернулась ветка копирования файла"
    # --single-transaction — не украшение: без него mysqldump берёт блокировку
    # чтения на все таблицы, и сайт на время снятия копии встаёт.
    assert "--single-transaction" in operatory, "дамп снова блокирует работу сайта"


def test_dump_snimaetsya_bez_paypa():
    """Код возврата из середины пайпа в POSIX sh не достать.

    `mysqldump ... | gzip > файл` вернул бы код gzip — то есть успех всегда, — и
    упавший на полпути mysqldump выглядел бы как удачно снятая копия. Та же
    ловушка, из-за которой в opencrm.sh появился run_painted.
    """
    operatory = _sh_operatory(Path("scripts/backup.sh").read_text(encoding="utf-8"))
    for line in operatory.splitlines():
        if "mysqldump" not in line:
            continue
        # `||` — это ветвление, а не пайп; убираем его, чтобы не поймать
        # проверку наличия клиента вместо самого дампа.
        assert "|" not in line.replace("||", ""), (
            f"дамп ушёл в пайп, код возврата потерян: {line.strip()}"
        )
    assert '> "$_cel"' in operatory, "дамп больше не пишется прямым перенаправлением"


def test_parol_bazy_ne_popadaet_v_komandnuyu_stroku():
    """Аргументы процесса видит через `ps` любой пользователь машины.

    `-pПАРОЛЬ` у mysqldump раздал бы доступ к базе всем, кто оказался рядом в
    момент снятия копии, — а копия снимается каждую ночь по расписанию.
    """
    for name in ("scripts/backup.sh", "scripts/restore.sh"):
        script = Path(name).read_text(encoding="utf-8")
        if "mysqldump" not in script and "mysql " not in script:
            continue
        assert "--defaults-extra-file" in script, f"{name}: пароль идёт не через файл"
        assert "--password=" not in script, f"{name}: пароль в командной строке"
        assert '-p"$' not in script, f"{name}: пароль в командной строке"


def test_restore_umeet_vernut_dump():
    """Копия, которую нечем вернуть, — не копия."""
    script = Path("scripts/restore.sh").read_text(encoding="utf-8")
    # Смотрим именно на разбор вида копии, а не на любое упоминание `*.sql)` в
    # файле: такое же слово стоит в сообщении о сделанном, и проверка «оно
    # где-то есть» пропускала выключенную ветку целиком.
    razbor = script[script.index('case "$DB_BACKUP" in'):]
    razbor = razbor[: razbor.index("\n    esac")]
    assert "*.sql)" in razbor, "восстановление не различает дамп MySQL"
    assert "mysql --defaults-extra-file" in razbor, "дамп нечем заливать"
    # Прежнее состояние — в сторону, а не в /dev/null: то же правило, что и у
    # SQLite, только вместо переименования файла приходится снимать дамп.
    assert "db-before-restore-" in script, "прежняя база не сохраняется перед заливкой"


def test_verify_zapuskaetsya_kak_modul():
    """Так его зовёт `scripts/backup.sh` — значит так он и должен работать."""
    # Кодировку задаём явно: без неё вывод читается кодировкой консоли Windows,
    # и русский текст превращается в кашу — падал бы не код, а проверка.
    done = subprocess.run(
        ["python", "-m", "scripts.verify_backup"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert done.returncode == 2, done.stderr
    assert "использование" in done.stdout


# --- живой круг: скрипты запускаются, а не читаются ---------------------------
#
# Всё, что ниже, найдено прогоном круга «копия → порча базы → восстановление» на
# настоящей MySQL в докере. Ни одна из этих бед не видна в тексте скриптов: обе
# показывают себя только поведением, и обе молчат ровно до того дня, когда копия
# понадобилась.


def _okruzhenie(**pravki) -> dict:
    okruzhenie = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    okruzhenie.update({k: str(v) for k, v in pravki.items()})
    return okruzhenie


def _polozhit(put: Path, dney: int) -> Path:
    """Файл с возрастом. Час запаса — чтобы не сесть ровно на границу суток."""
    put.write_text(f"копия возрастом {dney} дней", encoding="utf-8")
    kogda = time.time() - dney * 86400 - 3600
    os.utime(put, (kogda, kogda))
    return put


def _snyat_kopiyu(tmp_path, *, voskresene: bool = False):
    """Гоняет НАСТОЯЩИЙ scripts/backup.sh боевым швом `OPENCRM_DB_DUMP`.

    Этим швом копию снимает `./opencrm.sh backup`: дамп делается в контейнере
    базы, где есть клиент `mysqldump`, а имя по дате, архив storage, ключ,
    ротацию и проверку годности делает этот скрипт — в одном месте. Значит и
    проверить его можно целиком, без сервера базы под боком.
    """
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    hranilishche = tmp_path / "storage"
    hranilishche.mkdir(exist_ok=True)
    (hranilishche / "fayl.txt").write_text("файл витрины", encoding="utf-8")
    good_dump(tmp_path / "incoming.sql", users=3)

    put = os.environ.get("PATH", "")
    if voskresene:
        # Подставной `date`: ветку weekly иначе видно только по воскресеньям, а
        # ждать воскресенья от набора тестов нельзя.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        nastoyashchiy = shutil.which("date") or "/bin/date"
        (bin_dir / "date").write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  +%u) echo 7 ;;\n"
            "  +%Y-%m-%d) echo 2026-08-16 ;;\n"
            f"  *) exec {nastoyashchiy} \"$@\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (bin_dir / "date").chmod(0o755)
        put = f"{bin_dir}{os.pathsep}{put}"

    zapusk = subprocess.run(
        ["sh", "scripts/backup.sh"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env=_okruzhenie(
            PATH=put,
            OPENCRM_BACKUP_DIR=backups,
            OPENCRM_STORAGE_DIR=hranilishche,
            OPENCRM_DB_DUMP=tmp_path / "incoming.sql",
            OPENCRM_SECRET_KEY="kluch-iz-nabora",
            OPENCRM_IP_HASH_SALT="sol-iz-nabora",
        ),
    )
    return zapusk, backups


def _vozrasty(katalog: Path) -> set[int]:
    return {int(p.stem.rsplit("-", 1)[1]) for p in katalog.glob("db-star-*.sql")}


#: Копия «прошлого захода», которую подставной chmod отказывается трогать.
#: Имя одно на два места — сам файл и сверку в подставе; разъедься они, проверка
#: позеленела бы, ничего не проверив.
CHUZHOY_FAYL = "db-2020-01-01.sql"


@nuzhen_sh
def test_chuzhoy_fayl_ne_sryvaet_proverku_kopii(tmp_path):
    """Гигиена прав не имеет права стоить проверки копии.

    **Живой случай, найденный по журналу боевого сервера.** В каталоге копий
    файлы двух владельцев, и это устройство, а не поломка: `./opencrm.sh backup`
    создаёт дамп НА ХОСТЕ, а `scripts/backup.sh` работает ВНУТРИ контейнера под
    `opencrm` и переносит готовый файл — перенос владельца не меняет. Со второго
    захода `chmod` встречает файлы прошлых заходов и получает отказ:

        chmod: changing permissions of '.../db-2026-08-23.sql': Operation not permitted

    Сам по себе пустяк: файлы и так лежат с правами 600. Беда в том, что было
    дальше — при `set -eu` ненулевой код `find` обрывал ВЕСЬ скрипт, а следующим
    шагом идёт проверка годности. То есть каждая копия, кроме самой первой,
    оставалась непроверенной, и в выводе не было даже строки `backup done`.

    Проверяется не «нет ошибки chmod», а то, ради чего всё: скрипт дошёл до
    конца и копию ПРОВЕРИЛ.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    nastoyashchiy = shutil.which("chmod") or "/bin/chmod"
    # Подставной `chmod` отказывает ровно на чужом файле — как на сервере, где
    # у файла другой владелец. Остальным он не мешает: их скрипт создаёт сам, и
    # там `chmod` идёт без `|| true`.
    #
    # Сверяем ИМЯ ФАЙЛА, а не путь целиком, и это не придирка: pytest называет
    # временный каталог по имени теста, а в имени теста есть слово «chuzhoy».
    # Сверка по пути отказывала и свежему дампу — скрипт умирал раньше той
    # строки, ради которой проверка написана, и краснела она по неверной
    # причине. Поймано шлюзом деплоя (локально этот тест на Windows пропускается).
    (bin_dir / "chmod").write_text(
        "#!/bin/sh" + chr(10)
        + "for _a in \"$@\"; do" + chr(10)
        + "  case \"${_a##*/}\" in" + chr(10)
        + "    " + CHUZHOY_FAYL + ")" + chr(10)
        + "      echo \"chmod: changing permissions of '$_a': Operation not permitted\" >&2" + chr(10)
        + "      exit 1 ;;" + chr(10)
        + "  esac" + chr(10)
        + "done" + chr(10)
        + f"exec {nastoyashchiy} \"$@\"" + chr(10),
        encoding="utf-8",
    )
    (bin_dir / "chmod").chmod(0o755)

    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    (backups / "weekly").mkdir(parents=True)
    chuzhoy = backups / "daily" / CHUZHOY_FAYL
    chuzhoy.write_text("копия прошлого захода", encoding="utf-8")

    hranilishche = tmp_path / "storage"
    hranilishche.mkdir(exist_ok=True)
    (hranilishche / "fayl.txt").write_text("файл витрины", encoding="utf-8")
    good_dump(tmp_path / "incoming.sql", users=3)

    zapusk = subprocess.run(
        ["sh", "scripts/backup.sh"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env=_okruzhenie(
            PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            OPENCRM_BACKUP_DIR=backups,
            OPENCRM_STORAGE_DIR=hranilishche,
            OPENCRM_DB_DUMP=tmp_path / "incoming.sql",
            OPENCRM_SECRET_KEY="kluch-iz-nabora",
            OPENCRM_IP_HASH_SALT="sol-iz-nabora",
        ),
    )

    assert zapusk.returncode == 0, (
        "скрипт оборвался на чужом файле:" + chr(10) + zapusk.stdout + zapusk.stderr
    )
    assert "backup done" in zapusk.stdout, (
        "нет строки `backup done` — скрипт не дошёл до конца:"
        + chr(10) + zapusk.stdout + zapusk.stderr
    )
    # И главное: проверка годности отработала и вынесла вердикт. Без неё копия —
    # это надежда. Ищем её собственные слова, а не косвенный признак: строка
    # «копия годна» печатается только `scripts/verify_backup`, и напечатать её
    # больше некому.
    assert "копия годна" in zapusk.stdout, (
        "копия не проверена — шаг проверки не отработал:" + chr(10) + zapusk.stdout
    )


def test_kopiya_ne_chitaetsya_postoronnimi(tmp_path):
    """Дамп базы — это вся система в одном файле.

    Клиенты, хэши паролей, шифротексты почтовых ящиков. Скрипт всегда прятал
    файл ключа (600) и клал рядом дамп с правами 0644 — то есть читаемый любым
    пользователем машины. Найдено живьём: `ls -l` в каталоге копий на стенде.
    """
    zapusk, backups = _snyat_kopiyu(tmp_path)
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr

    fayly = sorted((backups / "daily").iterdir())
    assert len(fayly) == 3, [p.name for p in fayly]
    for fayl in fayly:
        rezhim = stat.S_IMODE(fayl.stat().st_mode)
        assert rezhim == 0o600, f"{fayl.name}: права {rezhim:o}, а не 600"


@nuzhen_sh
def test_rotatsiya_derzhit_obeshchannoe_i_ne_udalyaet_lishnego(tmp_path):
    """Обещано 7 ежедневных копий. Ошибка тут тихая с обеих сторон.

    Удалит лишнее — позавчерашней копии не окажется в тот единственный день,
    когда она нужна. Не удалит вовсе — диск кончится, а кончившийся диск это
    ровно то, из-за чего обрывается следующий дамп.
    """
    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    (backups / "weekly").mkdir(parents=True)
    for vozrast in range(1, 16):
        for imya in ("db-star-{:02d}.sql", "storage-star-{:02d}.tar.gz"):
            _polozhit(backups / "daily" / imya.format(vozrast), vozrast)

    zapusk, _ = _snyat_kopiyu(tmp_path)
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr

    zhivy = _vozrasty(backups / "daily")
    assert zhivy == set(range(1, 8)), f"осталось: {sorted(zhivy)}"
    assert len(zhivy) >= 7, "обещано 7 ежедневных копий, а хранится меньше"
    # Пара к каждой копии — тоже на месте: база без своего архива storage
    # восстанавливается наполовину.
    assert len(list((backups / "daily").glob("storage-star-*.tar.gz"))) == 7
    # И сегодняшняя копия, разумеется, никуда не делась.
    assert len(list((backups / "daily").glob("db-2*.sql"))) == 1


@nuzhen_sh
def test_voskresnaya_kopiya_uezzhaet_v_weekly_i_zhivyot_chetyre_nedeli(tmp_path):
    """Обещано 4 еженедельных. Проверяем обе половины обещания разом."""
    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    (backups / "weekly").mkdir(parents=True)
    for vozrast in (7, 14, 21, 28, 35, 42):
        _polozhit(backups / "weekly" / f"db-star-{vozrast:02d}.sql", vozrast)

    zapusk, _ = _snyat_kopiyu(tmp_path, voskresene=True)
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr

    zhivy = _vozrasty(backups / "weekly")
    assert zhivy == {7, 14, 21, 28}, f"осталось: {sorted(zhivy)}"
    assert len(zhivy) >= 4, "обещано 4 еженедельных копии, а хранится меньше"

    trojka = sorted(p.name for p in (backups / "weekly").glob("*2026-08-16*"))
    assert trojka == [
        "db-2026-08-16.sql", "secret-2026-08-16.env", "storage-2026-08-16.tar.gz",
    ], f"воскресная копия уехала в weekly не целиком: {trojka}"
    for imya in trojka:
        rezhim = stat.S_IMODE((backups / "weekly" / imya).stat().st_mode)
        assert rezhim == 0o600, f"weekly/{imya}: права {rezhim:o}, а не 600"


@nuzhen_sh
def test_dampy_pered_vosstanovleniem_ne_kopyatsya_vechno(tmp_path):
    """`db-before-restore-*.sql` — полный дамп базы, и он не ротировался вовсе.

    Каждое восстановление оставляло рядом с копиями ещё одну целую базу
    навсегда. Свежий трогать нельзя: это единственный путь назад из неудачного
    восстановления, и делают этот откат в тот же день.
    """
    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    (backups / "weekly").mkdir(parents=True)
    staryy = _polozhit(backups / "db-before-restore-20250101-000000.sql", 40)
    svezhiy = _polozhit(backups / "db-before-restore-20260101-000000.sql", 2)

    zapusk, _ = _snyat_kopiyu(tmp_path)
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr
    assert not staryy.exists(), "дамп перед восстановлением копится на диске вечно"
    assert svezhiy.exists(), "свежий дамп перед восстановлением удалять нельзя"


# --- восстановление: копия проверяется ДО того, как тронуть базу --------------


def _podstavnoy_klient(tmp_path) -> tuple[Path, Path]:
    """Подставные `mysql` и `mysqldump`: отмечаются в следе и молчат.

    След нужен ради главного вопроса этих проверок: тронули базу или нет.
    Отказ ПОСЛЕ заливки — это не отказ, а сообщение о случившемся.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sled = tmp_path / "sled.txt"
    for imya in ("mysql", "mysqldump"):
        put = bin_dir / imya
        put.write_text(
            "#!/bin/sh\n"
            f'echo "{imya}" >> "$SLED"\n'
            'echo "-- подставной клиент"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        put.chmod(0o755)
    return bin_dir, sled


def _vosstanovit(tmp_path, kopiya: Path, **pravki):
    bin_dir, sled = _podstavnoy_klient(tmp_path)
    arkhiv = good_storage(tmp_path / "storage.tar.gz")
    zapusk = subprocess.run(
        ["sh", "scripts/restore.sh", str(kopiya), str(arkhiv)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=_okruzhenie(
            PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            SLED=sled,
            OPENCRM_DB_URL="mysql+pymysql://u:p@db:3306/opencrm?charset=utf8mb4",
            OPENCRM_STORAGE_DIR=tmp_path / "vosstanovlennyy-storage",
            **pravki,
        ),
    )
    return zapusk, sled


def _ogryzok(tmp_path) -> Path:
    """Копия, оборванная РОВНО на границе оператора.

    Так и выглядит дамп, у которого место кончилось между двумя INSERT'ами.
    Клиент `mysql` заливает такой огрызок без единой жалобы и выходит с нулём —
    поэтому обрыв на границе оператора страшнее обрыва на полуслове.
    """
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    kopiya = good_dump(daily / "db-2026-08-14.sql", users=40)
    tekst = kopiya.read_text(encoding="utf-8")
    granica = tekst.rfind(";\n", 0, len(tekst) // 2)
    assert granica > 0
    kopiya.write_text(tekst[: granica + 2], encoding="utf-8")
    assert verify_backup.KHVOST_DUMPA not in kopiya.read_text(encoding="utf-8")
    return kopiya


@nuzhen_sh
def test_vosstanovlenie_otkazyvaet_oborvannoy_kopii(tmp_path):
    """Найдено живьём, и это худшая из находок круга.

    Копию, обрезанную пополам, залили в испорченную базу: скрипт напечатал
    «restore done.», вернул ноль, а таблиц users, warehouses и tasks в базе не
    было вовсе. `scripts/verify_backup.py` про эту же копию говорил «НЕГОДНА» —
    восстановление его просто не спрашивало.
    """
    kopiya = _ogryzok(tmp_path)
    zapusk, sled = _vosstanovit(tmp_path, kopiya)

    assert zapusk.returncode != 0, zapusk.stdout
    assert "оборвана" in zapusk.stderr, zapusk.stderr
    assert "restore done" not in zapusk.stdout, zapusk.stdout
    assert not sled.exists(), (
        "базу тронули оборванной копией: " + sled.read_text(encoding="utf-8")
    )


@nuzhen_sh
def test_vosstanovlenie_beryot_tseluyu_kopiyu(tmp_path):
    """Положительная половина сторожа.

    Без неё «отказывать вообще всему» тоже было бы зелёным — и восстановление
    перестало бы работать целиком, о чём узнали бы в худший день.
    """
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    kopiya = good_dump(daily / "db-2026-08-14.sql", users=3)

    zapusk, sled = _vosstanovit(tmp_path, kopiya)
    assert zapusk.returncode == 0, zapusk.stderr
    assert "restore done" in zapusk.stdout
    assert "mysql" in sled.read_text(encoding="utf-8"), "дамп до базы не доехал"

    # Прежнее состояние сохранено — и закрыто так же, как обычная копия: это
    # такая же полная база с хэшами паролей, а умаска у восстановления 022.
    otlozhennye = list((tmp_path / "backups").glob("db-before-restore-*.sql"))
    assert len(otlozhennye) == 1, "прежняя база не сохранена перед заливкой"
    rezhim = stat.S_IMODE(otlozhennye[0].stat().st_mode)
    assert rezhim == 0o600, f"{otlozhennye[0].name}: права {rezhim:o}, а не 600"


@nuzhen_sh
def test_vosstanovlenie_beryot_predmigracionnyy_snimok(tmp_path):
    """У снимка перед миграциями хвост свой — метка `scripts/snapshot_db.py`.

    Сторож, знающий только `-- Dump completed`, отказал бы снимку, которым
    откатывается неудачное обновление (`deploy/updater.py`), — то есть закрыл бы
    главный способ починки боевого сервера.
    """
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    kopiya = daily / "db-a1c7f4e29d63.sql"
    kopiya.write_text(
        "SET NAMES utf8mb4;\n"
        "SET FOREIGN_KEY_CHECKS=0;\n"
        "CREATE TABLE `users` (`id` int NOT NULL);\n"
        "INSERT INTO `users` (`id`) VALUES\n(1);\n"
        "SET FOREIGN_KEY_CHECKS=1;\n"
        f"{METKA}: таблиц 1, строк 1\n",
        encoding="utf-8",
    )

    zapusk, sled = _vosstanovit(tmp_path, kopiya)
    assert zapusk.returncode == 0, zapusk.stderr
    assert "mysql" in sled.read_text(encoding="utf-8"), "снимок до базы не доехал"


@nuzhen_sh
def test_vosstanovlenie_otkazyvaet_chuzhomu_faylu(tmp_path):
    """Файл SQLite от прежней установки лежит в том же каталоге копий.

    Заливать его некуда, и раньше он проходил мимо ветки `*.sql`: скрипт бодро
    печатал «restore done.», не тронув базу вовсе. Найдено живьём.
    """
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    chuzhoy = daily / "db-2026-08-14.db"
    chuzhoy.write_bytes(b"SQLite format 3\x00" + bytes(64))

    zapusk, sled = _vosstanovit(tmp_path, chuzhoy)
    assert zapusk.returncode != 0, zapusk.stdout
    # Именно про чужой вид, а не про оборванную копию: сказать «оборвана» о
    # файле SQLite значит послать человека искать копию поновее вместо того,
    # чтобы объяснить, что он выбрал не тот файл.
    assert "непонятная копия" in zapusk.stderr, zapusk.stderr
    assert "restore done" not in zapusk.stdout, zapusk.stdout
    assert not sled.exists()


@nuzhen_sh
def test_oborvannuyu_kopiyu_zalivayut_tolko_naroshno(tmp_path):
    """Половина данных иногда лучше, чем ничего, — но только по явной просьбе.

    Отказ без выхода был бы своей бедой: бывает, что оборванная копия — всё,
    что осталось.
    """
    kopiya = _ogryzok(tmp_path)
    zapusk, sled = _vosstanovit(tmp_path, kopiya, OPENCRM_FORCE_RESTORE="1")
    assert zapusk.returncode == 0, zapusk.stderr
    assert "mysql" in sled.read_text(encoding="utf-8")


def test_metki_kontsa_ne_razoshlis():
    """Хвостов два, и знать их обязаны все, кто решает «копия дочитана».

    Разъехавшись, они дадут не отказ, а тишину: восстановление примет огрызок
    либо откажет годному снимку.
    """
    script = Path("scripts/restore.sh").read_text(encoding="utf-8")
    assert verify_backup.KHVOST_DUMPA in script, "restore.sh не знает хвоста mysqldump"
    assert METKA in script, "restore.sh не знает метки предмиграционного снимка"
