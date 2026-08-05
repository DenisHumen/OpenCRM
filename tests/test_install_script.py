"""opencrm.sh: заслон от sudo и починка после него.

Скрипт запускают на чужих серверах руками, и цена ошибки здесь выше обычной.
Запуск под sudo однажды уже увёл боевой сайт: владельцем данных записался root
(UID 0), состояние переехало в /root/opencrm, прежняя база осталась в домашней
папке хозяина машины и выглядела пропавшей.

Проверяем не текст сообщений, а то, что защита и починка вообще на месте:
пропасть они могут молча, а обнаружится это на очередном боевом сервере.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "opencrm.sh"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="opencrm.sh рядом нет")

SH = shutil.which("sh") or shutil.which("bash")


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.mark.skipif(
    SH is None or sys.platform == "win32",
    # На Windows `sh` — это bash из WSL: он получает путь вида C:\... и не
    # находит файл. Проверка осмысленна там, где скрипт и работает, — на Linux
    # и в CI; изображать её на Windows значило бы получать красный тест на
    # ровном месте и привыкнуть его игнорировать.
    reason="нет sh или Windows: путь не переводится в WSL",
)
def test_the_script_is_valid_posix_sh():
    """Синтаксическая ошибка в установщике = сервер, на который не установить."""
    result = subprocess.run([SH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_sudo_is_refused():
    """Заслон обязан смотреть на SUDO_USER, а не только на id -u.

    Разница принципиальна: честный root-логин (на многих VPS другого
    пользователя нет) должен работать, а `sudo` от обычного пользователя —
    нет, потому что ломает именно смешивание.
    """
    text = source()
    assert "guard_root()" in text, "заслон от sudo исчез"
    assert "SUDO_USER" in text, "заслон не различает sudo и настоящий root-логин"
    assert "OPENCRM_ALLOW_ROOT" in text, "нет аварийного обхода заслона"


def test_repair_command_exists_and_is_reachable():
    """Починка бесполезна, если до неё нельзя добраться из-под sudo."""
    text = source()
    assert "cmd_repair()" in text
    assert "repair)     cmd_repair ;;" in text, "команда repair не разбирается в main"
    assert "15) cmd_repair ;;" in text, "починки нет в меню"
    # Ради этого всё и затевалось: repair должен пройти сам заслон, иначе
    # чинить последствия sudo будет нечем.
    assert "repair|help) : ;;" in text, "repair не освобождён от заслона"


def test_repair_writes_back_all_three_broken_values():
    """Sudo портит ровно три значения: UID, GID и путь к состоянию.

    Починить два из трёх — оставить сайт в том же нерабочем виде.
    """
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    for key in ("OPENCRM_UID", "OPENCRM_GID", "OPENCRM_HOME"):
        assert f'env_set "$DOCKER_ENV" {key}' in repair, f"{key} не восстанавливается"


def test_repair_never_deletes_anything():
    """Аварийный инструмент на боевом сервере. Переносить — можно, удалять — нет."""
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    assert "rm -rf" not in repair, "починка удаляет файлы — так нельзя"
    assert "rm -f" not in repair, "починка удаляет файлы — так нельзя"


def test_repair_leaves_certificates_alone():
    """Ключ Let's Encrypt читает nginx от root; трогать его не нужно."""
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    # chown -R идёт по data и storage поимённо, а не по всему каталогу состояния
    assert 'chown -R "$_want_uid:$_want_gid" "$_want_home/data" "$_want_home/storage"' in repair
    assert 'chown -R "$_want_uid:$_want_gid" "$_want_home"\n' not in repair, (
        "рекурсивный chown всего состояния заденет ключи сертификата"
    )
