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


def test_existing_certificate_is_detected_by_the_same_file_nginx_uses():
    """Иначе получается тупик без объяснения.

    Каталог live/<домен>/ остаётся после оборванного выпуска и от переезда с
    другого сервера, а сертификата в нём нет. Проверка по каталогу заставляла
    скрипт отвечать «уже выпущен» и ничего не делать, nginx при этом не находил
    fullchain.pem и не поднимал 443 — человек оставался без HTTPS навсегда.
    """
    text = source()
    issue = text[text.index("issue_certificate()") : text.index("setup_autoupdate()")]
    assert "fullchain.pem" in issue, "наличие сертификата проверяется не по файлу"
    nginx = (SCRIPT.parent / "docker" / "nginx" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "fullchain.pem" in nginx, "nginx смотрит на другой признак — они разойдутся"


def test_doctor_explains_why_the_site_is_down():
    """Разбор аварии не должен превращаться в переписку «пришлите ещё логи».

    Причина лежачего сайта каждый раз складывалась из трёх вещей: состояние
    контейнеров, слушает ли кто-то 80/443, и хвост лога того, кто не поднялся.
    Диагностика обязана отдавать их разом.
    """
    text = source()
    assert "why_down()" in text, "диагностика больше не объясняет, почему сайт лежит"
    section = text[text.index("why_down() {") :]
    assert "compose ps" in section, "не видно состояния контейнеров"
    assert "compose logs" in section, "не видно логов"
    assert ":(80|443)" in section or "80|443" in section, "не проверяются порты"
    # Вывод человек отдаёт тому, кто помогает, — секретов в нём быть не должно.
    for secret in ("OPENCRM_SECRET_KEY", "OPENCRM_IP_HASH_SALT", "cat \"$APP_ENV\""):
        assert secret not in section, f"в разбор аварии попал секрет: {secret}"


def test_certbot_is_run_with_its_entrypoint_overridden():
    """Сервис certbot объявляет entrypoint — бесконечный цикл продления, а
    `docker compose run` подменяет команду, а не entrypoint.

    Без `--entrypoint certbot` аргументы `certonly ...` уезжают в позиционные
    параметры `sh -c` и не выполняются вовсе: контейнер запускает продление
    навечно, выпуск висит на «Created», сертификат не появляется никогда.
    А без файла сертификата nginx не поднимает 443 — снаружи сайт выглядит
    недоступным по HTTPS. Именно так и случилось на боевом сервере.

    Проверяем и код, и напечатанные подсказки: команду из подсказки человек
    скопирует и получит тот же вечный цикл.
    """
    root = SCRIPT.parent
    places = [
        SCRIPT,
        root / "docker" / "docker-compose.yml",
        root / "docker" / "nginx" / "entrypoint.sh",
    ]
    for path in places:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "certonly" not in line:
                continue
            # интересует только строка, которая действительно запускает контейнер
            if "compose run" not in line and "run --rm" not in line:
                continue
            assert "--entrypoint" in line, (
                f"{path.name}: `compose run` для certbot без --entrypoint — "
                f"выпуск сертификата повиснет:\n{line.strip()}"
            )


def test_repair_looks_into_root_home_with_sudo():
    """Каталог /root закрыт (0700), и обычный `[ -d /root/opencrm/data ]` от
    имени пользователя отвечает «нет» не потому, что каталога нет, а потому что
    туда не заглянуть.

    На этом починка уже обожглась на боевом сервере: не увидела базу в
    /root/opencrm, отрапортовала «путь исправим» и оставила сайт с пустым
    каталогом. Данные были целы, но выглядело это как их потеря.
    """
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    assert "_dir_has_data()" in repair, "проверка наличия данных снова инлайновая"
    assert '$SUDO test -d "$1"' in repair, "существование каталога проверяется без sudo"
    assert '$SUDO test -e "$_source/$_item"' in repair, "перенос проверяет источник без sudo"
    # Путь в .env мог быть уже исправлен прошлым запуском починки, а данные
    # остаться в /root — поэтому известное плохое место проверяется отдельно.
    assert "_root_home" in repair, "починка ищет данные только по записи в .env"


def test_empty_target_directory_does_not_block_the_move():
    """Пустой каталог в цели — след прошлого запуска починки, а не данные.

    На этом переехала база, а медиа осталось в /root: сайт выглядел рабочим,
    доски открывались, но все картинки были битые — nginx отдаёт их с диска из
    ${OPENCRM_HOME}/storage, а там было пусто.

    Снимаем пустышку через rmdir: непустой каталог он удалить откажется, и это
    та самая страховка, из-за которой здесь нельзя писать rm.
    """
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    assert "rmdir" in repair, "пустой каталог в цели снова отменит перенос"
    assert "rm -r" not in repair, "перенос удаляет каталоги рекурсивно — так нельзя"


def test_repair_detects_stranded_media_not_only_the_database():
    """Брошенным считается место с любыми данными — базой ИЛИ медиа.

    Проверка одного лишь data пропускала случай «база уже переехала, картинки
    нет»: починка отвечала «всё на месте» и не делала ничего.
    """
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    assert "_has_state()" in repair
    assert '_dir_has_data "$1/storage"' in repair, "медиа не учитывается при поиске"


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
