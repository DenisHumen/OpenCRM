"""Резервные копии: годна ли копия и есть ли в ней то, без чего не восстановиться.

Копии, которые никто не проверял, — это не копии, а надежда. Обычная история:
скрипт год пишет файлы, диск заполняется, `.backup` начинает обрываться на
полпути, а узнают об этом в день, когда база понадобилась.

Проверки ниже — про две беды, каждая из которых делает копию бесполезной молча:
**копия негодна** (оборвана, пуста, не отмечена миграцией) и **в копии нет
ключа шифрования** — а без него пароли почтовых ящиков не расшифровать никогда.
"""

import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path

from scripts import verify_backup


def good_db(path: Path, users: int = 1) -> Path:
    """Правдоподобная копия: миграция отмечена, пользователи и настройки есть."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('c3d9f2a71b58');
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE site_settings (id INTEGER PRIMARY KEY, key TEXT);
        INSERT INTO site_settings (key) VALUES ('currency');
        CREATE TABLE clients (id INTEGER PRIMARY KEY);
        """
    )
    for number in range(users):
        db.execute("INSERT INTO users (email) VALUES (?)", (f"u{number}@example.com",))
    db.commit()
    db.close()
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
        good_db(tmp_path / "db.db", users=3),
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
    """`.backup`, прерванный на полном диске, оставляет файл, который выглядит
    как база и читается до первой битой страницы."""
    path = good_db(tmp_path / "torn.db", users=400)
    # Обрезаем файл, а не портим байты: именно так выглядит копия, снятие
    # которой прервалось на полном диске. Обнулять середину бессмысленно —
    # в маленькой базе это попадает в свободное место, и `integrity_check`
    # честно отвечает «ok» (проверено первым прогоном).
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])

    report = verify_backup.verify(path, None, None)
    assert not report["ok"], report
    assert any(
        "целостность" in p or "не открывается" in p or "не читается" in p
        for p in report["problems"]
    ), report["problems"]


def test_pustaya_kopiya_zamechena(tmp_path):
    """Самый коварный случай: файл есть, размер правдоподобный, внутри пусто.

    Такую копию восстанавливают и обнаруживают, что войти в систему некому.
    """
    report = verify_backup.verify(good_db(tmp_path / "empty.db", users=0), None, None)
    assert not report["ok"]
    assert any("users" in p for p in report["problems"]), report["problems"]


def test_kopiya_bez_migratsii_zamechena(tmp_path):
    """База без `alembic_version` не поднимется новым кодом: он не знает, чем
    её доводить (см. `database/schema_check.py`)."""
    path = good_db(tmp_path / "unstamped.db")
    db = sqlite3.connect(path)
    db.execute("DROP TABLE alembic_version")
    db.commit()
    db.close()

    report = verify_backup.verify(path, None, None)
    assert not report["ok"]
    assert any("миграцией" in p for p in report["problems"]), report["problems"]


def test_bityy_arkhiv_zamechen(tmp_path):
    """Оглавление читаем без распаковки: место под неё может и не найтись."""
    broken = tmp_path / "storage.tar.gz"
    broken.write_text("это не архив", encoding="utf-8")
    report = verify_backup.verify(good_db(tmp_path / "db.db"), broken, None)
    assert not report["ok"]
    assert any("storage" in p for p in report["problems"]), report["problems"]


def test_propavshiy_fayl_zamechen(tmp_path):
    report = verify_backup.verify(tmp_path / "нет-такого.db", None, None)
    assert not report["ok"]
    assert any("нет" in p for p in report["problems"])


# --- ключ шифрования ----------------------------------------------------------


def test_kopiya_bez_klyucha_zamechena(tmp_path):
    """Потеря ключа необратима.

    Пароли почтовых ящиков зашифрованы `OPENCRM_SECRET_KEY`, и он не выводится
    из данных (`core/security/secretbox.py`). Ключ, оставшийся только в
    config/.env на сгоревшем сервере, — это потеря навсегда, а не неудобство.
    """
    report = verify_backup.verify(
        good_db(tmp_path / "db.db"), None, tmp_path / "secret.env"
    )
    assert not report["ok"]
    assert any("ключ" in p for p in report["problems"]), report["problems"]


def test_pustoy_fayl_klyucha_zamechen(tmp_path):
    """Файл есть, а ключа в нём нет — так выглядит бэкап, снятый без переменных
    окружения. Проверка обязана отличать это от «ключ на месте»."""
    empty = tmp_path / "secret.env"
    empty.write_text("# ключей не досталось\n", encoding="utf-8")
    report = verify_backup.verify(good_db(tmp_path / "db.db"), None, empty)
    assert not report["ok"]
    assert any("ключа в нём нет" in p for p in report["problems"]), report["problems"]


# --- отчёт --------------------------------------------------------------------


def test_otchyot_ostayotsya_na_diske(tmp_path):
    """Вопрос «когда проверяли в последний раз» задают тогда, когда копия уже
    понадобилась, и ответ должен лежать на диске, а не в чьей-то памяти."""
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    code = verify_backup.main([str(good_db(daily / "db-2026-08-07.db"))])
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
    assert verify_backup.main([str(good_db(daily / "db.db", users=0))]) == 1


def test_skript_bekapa_zovyot_proverku():
    """Сторож: проверка должна остаться частью снятия копии.

    Убрать вызов при чистке легко — выглядит он как лишний шаг, — а обнаружится
    пропажа в день, когда копия понадобилась.
    """
    script = Path("scripts/backup.sh").read_text(encoding="utf-8")
    assert "verify_backup" in script, "бэкап перестал проверять сам себя"
    assert "OPENCRM_SECRET_KEY" in script, "ключ шифрования перестал попадать в копию"


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
