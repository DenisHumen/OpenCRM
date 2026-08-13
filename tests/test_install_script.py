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


def test_painted_output_keeps_the_exit_code():
    """Пайп подменяет код возврата — это ловушка, а не мелочь.

    В `cmd | paint` значение $? принадлежит раскраске, и она успешна всегда.
    Проверки вида `if compose up; then` начали бы считать успехом любой исход,
    включая упавший деплой. POSIX sh не знает PIPESTATUS, поэтому код обязан
    переноситься отдельно.
    """
    text = source()
    runner = text[text.index("run_painted() {") : text.index("run_painted() {") + 700]
    assert "mktemp" in runner, "код возврата снова теряется в пайпе"
    assert 'return "${_rc:-1}"' in runner, "функция не возвращает код команды"
    # Отсутствие mktemp не должно превращать раскраску в отказ работать.
    assert '|| { "$@" 2>&1; return $?; }' in runner, "без mktemp команда не выполнится вовсе"


def test_colorizer_avoids_gnu_only_regex():
    """В Ubuntu awk — это mawk, и расширения GNU там молча не работают.

    `\\b` в шаблоне не даёт ошибки: он просто никогда не совпадает, и подсветка
    успеха тихо исчезает именно на той системе, ради которой писалась.
    """
    text = source()
    paint = text[text.index("paint() {") : text.index("run_painted() {")]
    assert "awk" in paint
    # Смотрим сами шаблоны, а не пояснения к ним: в комментарии \b упомянут
    # словами, и проверка всего блока целиком ловила бы собственный текст.
    patterns = [line for line in paint.splitlines() if "~ /" in line]
    assert patterns, "в раскраске не осталось шаблонов"
    for line in patterns:
        assert "\\b" not in line, f"граница слова из GNU awk: {line.strip()[:60]}"
    assert "fflush()" in paint, "без сброса буфера живые логи идут рывками"
    # Слово из отчёта деплоя должно попадать в красное: перечисление форм по
    # одной (failed|failure) как раз и пропускало голое FAIL.
    assert "fail" in paint


def test_menu_commands_paint_their_output():
    """Цвет нужен во всех пунктах, а не только в сообщениях самого скрипта.

    Docker, git и python сыплют ровной простынёй, и именно в ней тонули
    сегодняшние поломки: строка про ошибку ничем не отличалась от соседних.
    """
    text = source()
    assert text.count("run_painted ") >= 10, "раскраска подключена не везде"
    for noisy in ("run_painted compose up -d", "run_painted compose ps", "run_painted autoupdate"):
        assert noisy in text, f"без раскраски: {noisy}"


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


def test_repair_fixes_autoupdate_paths_and_service_user():
    """Автообновление помнит пути отдельно от docker/.env.

    Внутри autoupdate.env лежит OPENCRM_HOME, записанный при установке под
    sudo. Обновлятор берёт каталог состояния оттуда и падает с «Permission
    denied: /root/opencrm/updates», когда всё остальное уже починено.

    А в systemd-юните прописан User. Остался root — демон продолжит работать от
    root и заново создаст root-овские файлы, отменив починку на первом же тике.
    Это то место, где не поправить значит не починить вовсе.
    """
    text = source()
    repair = text[text.index("cmd_repair()") : text.index("cmd_doctor()")]
    assert 'env_set "$_auto_env" OPENCRM_HOME' in repair, "путь в autoupdate.env не правится"
    assert 'env_set "$_auto_env" OPENCRM_UPDATE_PROJECT_DIR' in repair
    assert "opencrm-autoupdate.service" in repair, "юнит остаётся с прежним User"
    assert "s#^User=.*#User=$_owner#" in repair, "User в юните не переписывается"
    assert "daemon-reload" in repair, "systemd не перечитает изменённый юнит"


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


# --- оборванный переезд не имеет права оставить сайт закрытым ------------------
#
# Перенос базы идёт при ЗАКРЫТОМ сайте: писать в это время некому, и потому
# «сверка сошлась» — факт, а не обещание. Цена этого решения — окно, в котором
# оборванный скрипт оставляет сайт закрытым навсегда.
#
# Проверено убийством дерева переезда на середине переноса: режим обслуживания
# остался включён, `/healthz` отвечал `status ok, schema ok, redis ok`, и не
# видел беды никто — ни докер, ни автообновление, ни мониторинг. Сотрудник при
# этом получал 503 даже на ЧТЕНИЕ списка клиентов, публичная главная отвечала
# 503, а владелец проходил по устройству режима и видел РАБОЧИЙ САЙТ.
#
# Отсюда две проверки. Перехват сигналов закрывает обычные обрывы (Ctrl+C,
# `kill`, закрытая ssh-сессия), видимость — все остальные, включая `kill -9` и
# выдернутое питание, от которых перехват не спасает вовсе.


def test_the_move_traps_signals_and_reopens_the_site():
    """Без перехвата Ctrl+C посреди переноса закрывает сайт навсегда."""
    text = source()
    assert "trap " in text, "в скрипте нет ни одного перехвата сигналов"
    perehvat = text[text.index("migrate_trap() {") : text.index("migrate_trap() {") + 600]
    for signal in ("EXIT", "INT", "TERM", "HUP"):
        assert signal in perehvat, f"сигнал {signal} не перехватывается"
    assert "migrate_cleanup" in perehvat, "перехват ничего не снимает"

    uborka = text[text.index("migrate_cleanup() {") : text.index("migrate_trap() {")]
    assert "migrate_maintenance off" in uborka, "уборка не открывает сайт обратно"


def test_the_trap_is_armed_before_the_site_is_closed():
    """Сигнал между «поставили перехват» и «закрыли» оставил бы сайт закрытым."""
    text = source()
    perenos = text[text.index("migrate_sqlite_to_mysql() {") :]
    perenos = perenos[: perenos.index("\n}\n")]
    assert "migrate_trap" in perenos, "перехват при переезде не ставится вовсе"
    assert perenos.index("migrate_trap") < perenos.index("migrate_maintenance on"), (
        "перехват ставится ПОСЛЕ закрытия сайта — окно между ними ничем не закрыто"
    )


def test_doctor_says_the_site_is_closed():
    """Закрытый сайт обязан быть видно там, куда смотрят при разборе аварии.

    Перехват не спасает от `kill -9` и от отключения питания, поэтому видимость
    важнее самого перехвата: без неё единственный, кто может открыть сайт, о
    беде не узнаёт — он-то в закрытый сайт как раз проходит.
    """
    text = source()
    doctor = text[text.index("cmd_doctor() {") : text.index("why_down() {")]
    assert '"maintenance":"on"' in doctor, "диагностика не спрашивает про режим обслуживания"
    assert '"maintenance":"off"' in doctor, "диагностика молчит и когда сайт открыт"


def test_maintenance_can_be_lifted_from_the_command_line():
    """Открыть сайт должно быть можно с сервера, а не только из закрытой CRM."""
    text = source()
    assert "cmd_maintenance() {" in text, "нет команды снятия режима с сервера"
    komanda = text[text.index("cmd_maintenance() {") :]
    komanda = komanda[: komanda.index("\n}\n")]
    assert "scripts.maintenance" in komanda, "команда не пользуется общим сервисом режима"
    assert "maintenance) cmd_maintenance" in text, "команда не подключена к разбору аргументов"
