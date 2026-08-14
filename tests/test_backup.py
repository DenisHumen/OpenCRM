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
import subprocess
import tarfile
from pathlib import Path

from scripts import verify_backup


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
