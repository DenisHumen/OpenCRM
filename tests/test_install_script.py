"""opencrm.sh: заслон от sudo и починка после него.

Скрипт запускают на чужих серверах руками, и цена ошибки здесь выше обычной.
Запуск под sudo однажды уже увёл боевой сайт: владельцем данных записался root
(UID 0), состояние переехало в /root/opencrm, прежняя база осталась в домашней
папке хозяина машины и выглядела пропавшей.

Проверяем не текст сообщений, а то, что защита и починка вообще на месте:
пропасть они могут молча, а обнаружится это на очередном боевом сервере.
"""

import os
import re
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


# --- закрытый сайт обязан быть виден -------------------------------------------
#
# Режим обслуживания включает человек, и он же может забыть его снять. Владелец
# в закрытый сайт проходит по устройству режима и видит его работающим, пока
# сотрудники получают 503, — то есть единственный, кто может открыть, беды и не
# видит. Отсюда две проверки: видимость в диагностике и способ открыть с сервера.


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


def test_obsluzhivanie_est_v_menyu():
    """Меню — единственная дверь для того, кто командами не пользуется.

    Режим обслуживания — то действие, за которым человек приходит в самый
    неудобный момент: сайт закрыт, а он этого не видит (владелец проходит по
    устройству режима и видит сайт рабочим).
    """
    text = source()
    # Привязка к НАЧАЛУ СТРОКИ, а не к подстроке.
    #
    # `text.index("menu() {")` находит первое вхождение где угодно, в том
    # числе внутри имени другой функции: `tui_menu() {` содержит эти знаки
    # целиком. Срез тогда начинается внутри чужого тела, и проверка порядка
    # строк падает, хотя само меню в полном порядке. Ловушка не разовая: так
    # сломала бы её любая будущая функция с именем на `_menu`.
    # Список пунктов и разбор ответа живут в РАЗНЫХ функциях, и это не
    # случайность: разбор вынесен в `menu_vypolnit`, чтобы звать его проверяемо
    # (`menu_vypolnit … || warn`). Голый вызов внутри `case` внутри `while` —
    # не проверяемый контекст, и под `set -e` ненулевой код любой команды
    # выбрасывал человека из меню в приглашение оболочки без объяснения.
    # Поэтому берём обе половины.
    menyu = text[text.index(chr(10) + "menu() {") + 1 : text.index(chr(10) + "usage() {")]
    razbor = text[text.index(chr(10) + "menu_vypolnit() {") + 1 :]
    razbor = razbor[: razbor.index(chr(10) + "}" + chr(10))]
    assert "Режим обслуживания" in menyu, "пункт «Режим обслуживания» пропал из меню"
    assert "menu_maintenance" in razbor, "пункт есть, а вызова menu_maintenance нет"

    # Номера пунктов и разбор ответа обязаны сойтись: пункт, который печатается,
    # но не разбирается, отвечает «нет такого пункта».
    import re

    napechatano = set(re.findall(r"menu_item (\d+)\s", menyu))
    razobrano = set(re.findall(r"^\s*(\d+)\)", razbor, re.MULTILINE))
    propali = napechatano - razobrano - {"0"}
    assert not propali, f"пункты печатаются, но не разбираются: {sorted(propali)}"


def test_menyu_obsluzhivaniya_snachala_govorit_kak_seychas():
    """Человек приходит сюда, НЕ ЗНАЯ, закрыт ли сайт, — с этого и начинаем."""
    text = source()
    fn = text[text.index("menu_maintenance() {") :]
    fn = fn[: fn.index("\n}\n")]
    assert "scripts.maintenance status" in fn, "меню не показывает, как сейчас"
    assert fn.index("status") < fn.index("menu_item 1"), (
        "выбор предлагается раньше, чем сказано текущее состояние"
    )


# --- терминал не должен оставаться сырым ---------------------------------------
#
# Поймано на боевом. `compose run` по умолчанию выделяет контейнеру
# псевдотерминал и ради этого переводит НАШ терминал в сырой режим. Обычно
# docker возвращает его как было, но вывод здесь уходит в пайп раскраски
# (`run_painted … | paint`), и возврат срабатывает не всегда.
#
# После переезда меню отрисовывалось ЦЕЛИКОМ и вставало на `read`: ввод перестал
# быть построчным, `read` не дожидался перевода строки, а Ctrl+C не доходил как
# сигнал — ISIG в сыром режиме выключен. Со стороны неотличимо от зависшего
# скрипта, выход один: переподключиться по ssh.


def test_zahody_v_konteyner_ne_prosyat_terminal():
    """`-T` у каждого `compose run`/`compose exec`: терминал им не нужен.

    Проверка механическая и без списка исключений: список рядом с правилом
    устаревает первым, а следующая команда без `-T` сломает ровно то же самое.
    """
    text = source()
    bez_t = []
    for nomer, stroka in enumerate(text.splitlines(), 1):
        golaya = stroka.strip()
        if golaya.startswith("#"):
            continue
        for komanda in ("compose run", "compose exec"):
            if komanda not in golaya:
                continue
            # Строка может переноситься — берём её вместе с продолжением.
            celaya = golaya
            hvost = text.splitlines()[nomer:]
            while celaya.endswith("\\") and hvost:
                celaya = celaya[:-1] + " " + hvost.pop(0).strip()
            if " -T " not in f" {celaya} ":
                bez_t.append(f"{nomer}: {golaya[:90]}")
    assert not bez_t, (
        "заход в контейнер без -T просит псевдотерминал и оставляет наш "
        "терминал сырым:\n  " + "\n  ".join(bez_t)
    )


def test_menyu_vozvrashchaet_terminal_kak_bylo():
    """Меню — пострадавшая сторона, и обязано прибирать за любой командой.

    Причину чинят выше, но исход «пришлось переподключаться по ssh» не должен
    зависеть от аккуратности каждой отдельной команды.
    """
    text = source()
    # Только исполняемые строки: `stty sane` упомянут в комментарии рядом —
    # там объяснено, почему им не пользуются.
    kod = "\n".join(s for s in text.splitlines() if not s.strip().startswith("#"))
    assert "stty -g" in kod, "состояние терминала нигде не запоминается"
    assert "stty sane" not in kod, (
        "`stty sane` навязывает свои настройки тому, у кого они намеренно другие"
    )
    # Привязка к НАЧАЛУ СТРОКИ, а не к подстроке.
    #
    # `text.index("menu() {")` находит первое вхождение где угодно, в том
    # числе внутри имени другой функции: `tui_menu() {` содержит эти знаки
    # целиком. Срез тогда начинается внутри чужого тела, и проверка порядка
    # строк падает, хотя само меню в полном порядке. Ловушка не разовая: так
    # сломала бы её любая будущая функция с именем на `_menu`.
    menyu = text[text.index(chr(10) + "menu() {") + 1 : text.index(chr(10) + "usage() {")]
    assert "tty_zapomnit" in menyu, "меню не запоминает состояние терминала"
    assert "tty_vernut" in menyu, "меню не возвращает состояние терминала"
    assert menyu.index("tty_zapomnit") < menyu.index("while :;"), (
        "состояние запоминается внутри цикла — то есть уже испорченным"
    )
    assert menyu.index("tty_vernut") < menyu.index("menu_header"), (
        "терминал возвращают после отрисовки — прибирать надо ДО неё"
    )


# --- общие переменные между функциями -----------------------------------------
#
# В POSIX sh нет локальных переменных: всё, что присвоено внутри функции, —
# глобальное. Отсюда самая дорогая ошибка живого меню: `tui_shapka` держала
# счётчик контейнеров в `_vsego` — том же имени, каким цикл меню считает пункты.
# Отрисовка шапки затирала счётчик ЧУЖИМ числом, и цифровой выбор сверялся с
# числом контейнеров: «4» работала, пока их было четыре, и переставала на трёх.
#
# Заметить это чтением невозможно: обе функции по отдельности верны. Соседняя
# проверка (`test_pomoshchniki_ne_zatirayut_peremennye_tsikla`) держит границу
# между помощниками меню и его циклом; эта — то же правило для ВСЕГО скрипта,
# без списка имён.


def _bez_podstanovok(stroka: str) -> str:
    """Вырезает `$( ... )` и обратные кавычки, считая вложенность.

    Вызов внутри подстановки уезжает в ПОДОБОЛОЧКУ: что он там присвоит, до
    вызывающей не долетит, и столкновением это не является. Простым выражением
    не обойтись — внутри подстановки почти всегда сидит ещё одна
    (`$(ask "$(tr_ ...)")`), и нежадный шаблон обрывается на первой закрывающей
    скобке. Из-за этого проверка сначала выдавала полтора десятка ложных пар и
    была бы отключена первым же, кто её прочитал.
    """
    out = []
    i = 0
    glubina = 0
    v_kavychkah = False
    while i < len(stroka):
        if not v_kavychkah and stroka.startswith("$(", i):
            glubina += 1
            i += 2
            continue
        if glubina and stroka[i] == "(":
            glubina += 1
        elif glubina and stroka[i] == ")":
            glubina -= 1
            i += 1
            continue
        if stroka[i] == "`":
            v_kavychkah = not v_kavychkah
            i += 1
            continue
        if not glubina and not v_kavychkah:
            out.append(stroka[i])
        i += 1
    return "".join(out)


def _funktsii(text: str) -> dict:
    """Тела функций верхнего уровня: имя → (номер первой строки, строки тела)."""
    nachalo_f = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*$")
    razobrano = {}
    imya = None
    nachalo = 0
    glubina = 0
    telo = []
    for nomer, stroka in enumerate(text.splitlines(), 1):
        if imya is None:
            sovpalo = nachalo_f.match(stroka)
            if sovpalo:
                imya, nachalo, glubina, telo = sovpalo.group(1), nomer, 1, []
            continue
        telo.append(stroka)
        glubina += stroka.count("{") - stroka.count("}")
        if glubina <= 0:
            razobrano[imya] = (nachalo, telo)
            imya = None
    return razobrano


def _stolknoveniya(text: str) -> list[str]:
    """Пары «A зовёт B, обе пишут V, и A читает V ПОСЛЕ вызова».

    Читает после — ключевое условие. До вызова чужая запись ещё не случилась,
    после — уже затёрла; пара, где вызывающая к переменной больше не
    возвращается, беды не несёт и в список не идёт.
    """
    prisvoenie = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=")
    cikl = re.compile(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
    chtenie = re.compile(r"\bread\s+(?:-r\s+)?([A-Za-z_][A-Za-z0-9_]*)")

    funktsii = _funktsii(text)
    pishet = {}
    zovyot = {}
    for imya, (_, telo) in funktsii.items():
        p, z = set(), []
        for sdvig, stroka in enumerate(telo):
            bez_komm = re.sub(r"#.*$", "", stroka)
            for vyrazhenie in (prisvoenie, cikl, chtenie):
                for sovpalo in vyrazhenie.finditer(bez_komm):
                    p.add(sovpalo.group(1))
            vidimoe = _bez_podstanovok(bez_komm)
            for drugaya in funktsii:
                if drugaya == imya:
                    continue
                if re.search(r"(^|[\s;(&|])" + re.escape(drugaya) + r"([\s;)&|\"]|$)", vidimoe):
                    z.append((sdvig, drugaya))
        pishet[imya], zovyot[imya] = p, z

    nayden = []
    for imya, (nachalo, telo) in funktsii.items():
        for sdvig, drugaya in zovyot[imya]:
            for peremennaya in sorted(pishet[imya] & pishet.get(drugaya, set())):
                if peremennaya == "IFS":
                    continue
                posle = "\n".join(telo[sdvig + 1:])
                if re.search(r"\$\{?" + re.escape(peremennaya) + r"\b", posle):
                    nayden.append(
                        f"opencrm.sh:{nachalo + sdvig + 1}: {imya} зовёт {drugaya}, "
                        f"обе пишут ${peremennaya}, и {imya} читает её после вызова"
                    )
    return sorted(set(nayden))


def test_funktsii_ne_zatirayut_peremennye_drug_druga():
    """Ни одна функция не затирает переменную, которую вызывающая ещё читает.

    Правило, а не список имён. Список пришлось бы дополнять каждый раз, когда
    заводится новая переменная, — то есть он отставал бы ровно на ту правку,
    которая беду и приносит.

    Разбор приблизительный: это не оболочка, а поиск подозрительных пар. Ложное
    срабатывание лечится своим префиксом у функции (образец — `sync_alert_channel`
    с `_sac_*`), и это ровно то, что и следует сделать: беды сегодня может не
    быть, но пара остаётся заряженной до первой перестановки строк.
    """
    nayden = _stolknoveniya(source())
    assert not nayden, "функции делят переменные:\n  " + "\n  ".join(nayden)


def test_razbor_stolknoveniy_vidit_podlozhennoe():
    """Проверка самой проверки: подложенное столкновение обязано находиться.

    Разбор с вырезанием подстановок легко сделать слишком щедрым — и он станет
    зелёным навсегда. Поэтому рядом стоит образец беды: функция, вызванная НЕ из
    подстановки, затирает переменную, которую вызывающая читает следом.
    """
    obrazets = "\n".join([
        "vneshnyaya() {",
        "    _schet=5",
        "    vnutrennyaya",
        '    echo "$_schet"',
        "}",
        "vnutrennyaya() {",
        "    _schet=9",
        "}",
    ])
    assert _stolknoveniya(obrazets), "разбор не увидел подложенного столкновения"

    # И обратная сторона: та же пара через подстановку — не столкновение,
    # потому что подоболочка своих присвоений наружу не отдаёт.
    bezopasno = obrazets.replace("    vnutrennyaya\n", "    _drugoe=$(vnutrennyaya)\n")
    assert not _stolknoveniya(bezopasno), "разбор считает бедой вызов в подоболочке"


def test_doctor_nazyvaet_lekarstvo_ot_gryaznogo_dereva():
    """Диагностика обязана сказать не только «что», но и «чем».

    Остальные строки доктора этому и следуют: `./opencrm.sh logs redis`,
    `./opencrm.sh monitoring reload`. Строка про несохранённые правки была
    единственной, которая нарушала собственное правило файла, — и стоила
    ровно того: на боевом сервере обновление встало, а команду пришлось
    спрашивать на стороне.

    Проверяется само присутствие команды, а не её точная запись: важно,
    чтобы человек не уходил за ней в другое место.
    """
    text = source()
    telo = text[text.index("cmd_doctor() {"):]
    telo = telo[: telo.index(chr(10) + "}")]
    stroka = [s for s in telo.splitlines() if "несохранённые правки" in s]
    assert stroka, "доктор перестал проверять чистоту репозитория"
    # ДВА раза, по разу на язык. Строка собрана из `tr_ "русское" "english"`,
    # и команда, оставшаяся в одной половине, — это лекарство, которого нет у
    # половины читателей. Найдено подрывом: снятие русской половины проверку
    # не покраснело, потому что английская держала её одна.
    assert stroka[0].count("checkout -- .") >= 2, (
        "доктор говорит про несохранённые правки, но не говорит на обоих языках, "
        "чем их стереть: " + stroka[0].strip()
    )


# --- согласие и отказ ----------------------------------------------------------


def _confirm_pod_sh(otvet: str) -> bool:
    """Гоняет НАСТОЯЩУЮ `confirm` из opencrm.sh настоящим `sh` с готовым ответом.

    Не поиск подстроки в исходнике: беда была именно в том, КАК оболочка
    разбирает образец, а не в том, что написано. Текст `[yYдД]*` выглядит
    безупречно и читается как «y, Y, д или Д» — беда только в исполнении.

    `ask` подменяется заглушкой: проверяем разбор ответа, а не чтение с
    терминала, которого в наборе тестов нет.
    """
    text = source()
    nachalo = text.index("confirm() {")
    telo = text[nachalo : text.index(chr(10) + "}", nachalo) + 2]
    skript = (
        'ask() { printf "%s" "$OTVET"; }' + chr(10)
        + telo + chr(10)
        + 'if confirm "Продолжить?" n; then echo SOGLASIE; else echo OTKAZ; fi'
    )
    gotovo = subprocess.run(
        [SH, "-c", skript],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "OTVET": otvet, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    assert "SOGLASIE" in gotovo.stdout or "OTKAZ" in gotovo.stdout, gotovo
    return "SOGLASIE" in gotovo.stdout


@pytest.mark.skipif(
    SH is None or sys.platform == "win32",
    # На Windows `sh` — это bash, а bash собирает многобайтные знаки в
    # скобочном наборе и отвечает ВЕРНО. Проверка зеленела бы на сломанном
    # коде — то есть была бы хуже отсутствующей. Работает она там, где работает
    # и скрипт: на Linux, в докерном шлюзе и в CI.
    reason="нет sh или Windows: там bash, и беды не видно",
)
@pytest.mark.parametrize(
    "otvet",
    ["нет", "Нет", "НЕТ", "нельзя", "отмена", "не надо", "no", "n", "", "0", "ЯЯЯ"],
)
def test_otkaz_ostayotsya_otkazom(otvet):
    """«нет» обязано означать нет. На боевом сервере оно означало да.

    `[yYдД]*` — скобочный набор с многобайтными знаками. dash сопоставляет
    образцы побайтно и такой знак не собирает: набор разворачивался во
    множество БАЙТОВ {y, Y, D0, B4, 94}, а байтом D0 начинается любая
    кириллическая буква от «А» до «п». Совпадало всё: «нет», «нельзя»,
    «отмена».

    Цена: «Продолжить?» перед заливкой дампа поверх живой базы, «Остановить
    сайт?», установка ufw с `default deny incoming`. Ответ «нет» делал ровно
    то, от чего человек отказывался.
    """
    assert not _confirm_pod_sh(otvet), (
        f"ответ «{otvet}» принят за согласие — опасные вопросы больше ничего не спрашивают"
    )


@pytest.mark.skipif(
    SH is None or sys.platform == "win32",
    reason="нет sh или Windows: там bash, и беды не видно",
)
@pytest.mark.parametrize("otvet", ["да", "Да", "ДА", "д", "Д", "y", "Y", "yes", "Yes"])
def test_soglasie_ostayotsya_soglasiem(otvet):
    """Обратная сторона: чинить отказ, сломав согласие, — не починка.

    Без этой половины правка «выкинуть кириллицу из набора» прошла бы как
    верная, а человек, отвечающий «да», получал бы отказ и не мог бы ни
    восстановиться из копии, ни включить фаервол.
    """
    assert _confirm_pod_sh(otvet), f"ответ «{otvet}» не принят за согласие"


@pytest.mark.skipif(
    SH is None or sys.platform == "win32",
    reason="нет sh или Windows: путь не переводится в WSL",
)
def test_autoupdate_ne_vypuskaet_okruzhenie_naruzhu(tmp_path):
    """`autoupdate` втягивает свой env-файл, и ничего из него не должно вытечь.

    В файле лежит `OPENCRM_HOME`, а у `docker compose` переменная окружения
    СИЛЬНЕЕ файла `docker/.env`. К `${OPENCRM_HOME}` привязаны все тома стека,
    включая каталог данных MySQL. Стоило двум файлам разойтись — а починка
    прав после sudo правит их по отдельности, — и любой `compose up -d` после
    вызова автообновления в том же запуске поднимал бы стек на ДРУГИХ
    каталогах: пустая база, сайт с нуля, настоящие данные целыми лежат по
    прежнему пути и выглядят пропавшими.

    Проверяется прогоном настоящей функции: разница между `.` в текущей
    оболочке и в подоболочке видна только в исполнении.
    """
    text = source()
    nachalo = text.index("autoupdate() {")
    telo = text[nachalo : text.index(chr(10) + "}", nachalo) + 2]

    env_file = tmp_path / "autoupdate.env"
    env_file.write_text("OPENCRM_HOME=/chuzhoy/put" + chr(10), encoding="utf-8")
    skript = (
        f'home_dir() {{ printf "%s" "{tmp_path.as_posix()}"; }}' + chr(10)
        + 'python3() { :; }' + chr(10)
        + 'REPO_DIR=/nety' + chr(10)
        + telo + chr(10)
        + 'OPENCRM_HOME=/pravilnyy' + chr(10)
        + 'autoupdate status >/dev/null 2>&1 || true' + chr(10)
        + 'printf "%s" "$OPENCRM_HOME"'
    )
    gotovo = subprocess.run(
        [SH, "-c", skript], capture_output=True, text=True, encoding="utf-8"
    )
    assert gotovo.stdout == "/pravilnyy", (
        "autoupdate вытолкнул OPENCRM_HOME наружу: " + repr(gotovo.stdout)
        + ". Следующий в этом же запуске `compose up -d` поднимет стек на чужих томах."
    )


def test_parol_administratora_ne_uezzhaet_v_argumenty():
    """Пароль владельца системы не должен попадать в командную строку.

    Аргументы видны в `ps` ЛЮБОМУ пользователю машины: `/proc/<pid>/cmdline`
    читается всеми, — и оседают в `docker inspect`. Пароль root-аккаунта CRM
    там утекал всякому, кто в эту минуту оказался на сервере, и всякому
    процессу, снимающему `ps` в цикле.

    Правило в этом файле записано дважды и оба раза с объяснением — у пароля
    наблюдателя базы и у пароля панели. Сюда оно просто не дошло, и проверка
    стоит затем, чтобы не «дошло обратно».
    """
    text = source()
    telo = text[text.index("cmd_password() {"):]
    telo = telo[: telo.index(chr(10) + "}")]
    assert "--password-stdin" in telo, "пароль снова уходит не через стандартный ввод"
    assert not re.search(r"--password[ =]\"?\$", telo), (
        "пароль подставляется в аргументы команды: " + telo
    )


def _reset_root_so_vvodom(vvod: str) -> str:
    """Гоняет `reset_root.py --password-stdin` с готовым вводом, отдаёт вывод."""
    korenn = Path(__file__).resolve().parent.parent
    gotovo = subprocess.run(
        # `--email` обязателен: без него скрипт отказывает РАНЬШЕ, чем дойдёт до
        # пароля, и проверка мерила бы не то.
        [
            sys.executable,
            str(korenn / "scripts" / "reset_root.py"),
            "--email",
            "proba-stdin@opencrm.test",
            "--password-stdin",
        ],
        input=vvod,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(korenn),
        timeout=120,
    )
    return gotovo.stdout + gotovo.stderr


def test_pustoy_vvod_ne_uvodit_v_terminal():
    """Пустой ввод обязан отказать словами, а не спрашивать пароль с терминала.

    Ровно этим кончилась первая редакция правки: `if` вместо `elif`, и пустой
    ввод проваливался в `getpass`. При обновлении терминала нет, и скрипт висел
    бы вечно — обновление встало бы намертво, не сказав ни слова. Поймано этой
    проверкой: она не покраснела, а ЗАВИСЛА на две минуты.
    """
    vyvod = _reset_root_so_vvodom("")
    assert "Пустой пароль" in vyvod, (
        "пустой ввод не отбит словами — скрипт мог уйти спрашивать в терминал: "
        + vyvod[:300]
    )


def test_parol_so_vvoda_vpravdu_chitaetsya():
    """Ключ обязан ЧИТАТЬ ввод, а не просто существовать в разборе аргументов.

    Проверяется коротким паролем: на него скрипт отвечает про длину, а на
    непрочитанный ввод — про пустоту. Две разные жалобы отличают «прочёл и не
    принял» от «не прочёл вовсе», и без этой пары ключ, который молча ничего не
    делает, выглядел бы исправным.

    База при этом не меняется: короткий пароль до записи не доходит.
    """
    vyvod = _reset_root_so_vvodom("ab" + chr(10))
    assert "короче" in vyvod, (
        "ключ --password-stdin ввод не прочитал: " + vyvod[:300]
    )


#: Слова, по которым видно, что у человека спрашивают СЕКРЕТ.
#:
#: По-русски и по-английски сразу: приглашения в этом файле двуязычные, и
#: половина из них написана только на одном языке в каждой ветке `tr_`.
SEKRETNYE = ("парол", "password", "токен", "token", "секрет", "secret", "ключ", "key")


def _prigashcheniya():
    """Строки вида `_x=$(ask ...)` — вопрос человеку с ответом в переменную."""
    for nomer, stroka in enumerate(source().splitlines(), 1):
        if "=$(ask " in stroka or "=$(ask_secret " in stroka:
            yield nomer, stroka


def test_perebor_nahodit_voprosy():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    naydeno = list(_prigashcheniya())
    assert len(naydeno) >= 5, (
        f"вопросов человеку нашлось {len(naydeno)} — сменился способ их писать, "
        "и проверка ниже стерегла бы пустоту"
    )


def test_sekret_ne_nabiraetsya_na_vidu():
    """Пароль и токен не должны оставаться в прокрутке терминала.

    **Беда, которая была.** Пароль владельца из `ps` в этом файле убирали
    ДВАЖДЫ, каждый раз с разбором в комментарии, — а про экран не подумали ни
    разу. На общей машине набранный пароль читает следующий, кто сядет за
    терминал; в записанной сессии он лежит вечно.

    Проверка механическая, потому что мест таких три и они в разных местах
    файла: два токена бота и пароль. Четвёртое появится тем же путём — кто-то
    напишет `ask`, и это будет выглядеть правильно.
    """
    na_vidu = []
    for nomer, stroka in _prigashcheniya():
        if "=$(ask_secret " in stroka:
            continue
        nizhnyaya = stroka.lower()
        if any(slovo in nizhnyaya for slovo in SEKRETNYE):
            na_vidu.append(f"строка {nomer}")

    assert not na_vidu, (
        "секрет спрашивается обычным `ask` — он наберётся на виду и останется в "
        "прокрутке терминала: " + ", ".join(na_vidu) + ". Зовите `ask_secret`"
    )


def test_ask_secret_vozvrashchaet_ekho_dazhe_pri_obryve():
    """Немой терминал после Ctrl+C — беда хуже той, что чинили.

    Человек жмёт Ctrl+C именно здесь чаще всего («не тот пароль набрал»), и без
    ловушки терминал остаётся без эха до `stty sane` — а знать про `stty sane`
    владелец не обязан.
    """
    telo = source().split("ask_secret() {", 1)[1].split("\nconfirm()", 1)[0]
    assert "stty -echo" in telo, "`ask_secret` не глушит ввод вовсе"
    assert "trap " in telo and "INT" in telo, (
        "эхо возвращается строкой следом, а не ловушкой: Ctrl+C оставит терминал "
        "немым"
    )
    assert "stty -g" in telo, (
        "состояние терминала не снимается перед правкой — возвращать будет нечего"
    )


# --- пульт: чужая ошибка не закрывает меню, а кадр не разъезжается -------------
#
# Три беды из сплошного разбора скрипта, и ни одной не видно чтением.
#
# `die` внутри команды — это `exit`: он закрывает ТУ ЖЕ оболочку, а `||` рядом с
# вызовом ловит код возврата, а не выход. Человек оставался без пульта ровно в ту
# минуту, когда что-то пошло не так, — то есть когда пульт нужнее всего.
#
# Кадр рисуется от `ESC[H` поверх прежнего, без очистки экрана. Строка длиннее
# окна не «вылезает вправо», а переносится терминалом: остаток кадра съезжает на
# строку вниз, и рамка остаётся разъехавшейся до самого выхода из меню.
#
# Ширину считал `wc -m`, а он считает знаки ПО ЛОКАЛИ: под `LANG=C` это байты,
# кириллица двухбайтная, и всякая русская подпись объявлялась вдвое шире, чем
# занимает на экране. Поэтому проверки ниже гоняются в обеих локалях.


def _telo_funktsii(imya: str, text: str) -> str:
    """Функция целиком, как она написана в скрипте, вместе с заголовком."""
    nachalo = text.index(chr(10) + imya + "() {") + 1
    return text[nachalo : text.index(chr(10) + "}" + chr(10), nachalo) + 3]


def _odnostrochnaya(imya: str, text: str) -> str:
    """`die` и `warn` написаны в одну строку — берём её целиком."""
    for stroka in text.splitlines():
        if stroka.startswith(imya + "()"):
            return stroka
    raise AssertionError(f"функции {imya} в скрипте больше нет")


#: Настоящий ESC внутри собираемого куска: `printf '\033'`.
#:
#: Именно настоящий, а не два знака «косая и ноль-три-три»: подставь их — и
#: раскраска в проверке станет обычным текстом, а сторож будет мерить не то.
_ESC_SH = "$(printf '" + chr(92) + "033')"


def _pod_sh(skript: str, lokal: str = "C.UTF-8") -> subprocess.CompletedProcess:
    """Гоняет собранный кусок скрипта настоящим `sh` в НАЗВАННОЙ локали.

    Локаль — не мелочь окружения, а половина проверяемого: от неё зависит,
    считает `wc -m` знаки или байты.

    Скрипт уходит СТАНДАРТНЫМ ВВОДОМ, а не `-c`: длинный аргумент под Windows
    обрезается на полуслове, и оболочка спотыкается о незакрытую кавычку вместо
    того, чтобы что-то проверить. `errors="replace"` — затем, чтобы разрезанная
    посередине буква красила проверку, а не ломала разбор вывода.
    """
    return subprocess.run(
        [SH],
        input=skript,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "LANG": lokal, "LC_ALL": lokal},
    )


def _pult_pod_sh(deystvie: str) -> subprocess.CompletedProcess:
    """Гоняет НАСТОЯЩУЮ `tui_vypolnit` с подложенным пунктом меню.

    Поиском по исходнику здесь не проверить ничего: написано всё правильно, а
    беда в том, КАК оболочка исполняет `exit` внутри вызванной функции.

    Терминала в наборе тестов нет, поэтому вывод в `/dev/tty` из тела убран: на
    разбор чужого `exit` это не влияет.
    """
    text = source()
    skript = chr(10).join([
        "UI_LANG=ru",
        'B=""; D=""; R=""; YELLOW=""; RED=""',
        _telo_funktsii("tr_", text),
        _odnostrochnaya("warn", text),
        _odnostrochnaya("die", text),
        "tui_vyklyuchit() { :; }",
        "tui_vklyuchit() { :; }",
        "tui_ubrat() { :; }",
        "tui_svodka_obnovit() { :; }",
        "ask() { :; }",
        "clear() { :; }",
        _telo_funktsii("tui_vypolnit", text).replace("> /dev/tty", ""),
        deystvie,
        "tui_vypolnit punkt",
        "printf 'PULT-ZHIV" + chr(92) + "n'",
    ])
    return _pod_sh(skript)


@pytest.mark.skipif(SH is None, reason="нет sh")
def test_die_v_komande_ne_zakryvaet_pult():
    """Отказ внутри пункта обязан оставить человека в меню.

    Комментарий у вызова обещал это с самого появления живого меню, а `||`
    обещания не держал: `die` делает `exit 1`, и он закрывает ту же оболочку, в
    которой крутится меню. `die` в командах меню не редкость — им кончается и
    «приложение не отвечает» в режиме обслуживания, и половина проверок доктора.
    """
    gotovo = _pult_pod_sh(
        "punkt() { printf 'VYVOD-KOMANDY" + chr(92) + "n'; die 'сайт не отвечает'; "
        "printf 'POSLE-DIE" + chr(92) + "n'; }"
    )
    vyvod = gotovo.stdout
    assert "POSLE-DIE" not in vyvod, (
        "`die` перестал обрывать команду — проверка ниже мерила бы не ту беду"
    )
    assert "PULT-ZHIV" in vyvod, (
        "`die` внутри пункта закрыл пульт целиком: "
        + repr(vyvod) + " " + repr(gotovo.stderr)
    )
    assert "VYVOD-KOMANDY" in vyvod, "вывод команды пропал: " + repr(vyvod)
    assert "команда закончилась ошибкой" in vyvod, "об отказе не сказано ни слова"
    assert vyvod.index("VYVOD-KOMANDY") < vyvod.index("команда закончилась ошибкой"), (
        "вывод команды пришёл ПОСЛЕ приговора о ней — значит он где-то копился, "
        "а не шёл человеку на экран по ходу дела: " + repr(vyvod)
    )


@pytest.mark.skipif(SH is None, reason="нет sh")
@pytest.mark.parametrize(
    ("punkt", "zhdyom_zhalobu"),
    [
        ("punkt() { printf 'GOTOVO" + chr(92) + "n'; }", False),
        ("punkt() { printf 'GOTOVO" + chr(92) + "n'; return 3; }", True),
    ],
)
def test_pult_otlichaet_udachu_ot_bedy(punkt, zhdyom_zhalobu):
    """Оборотная сторона: удача не должна начать выглядеть отказом.

    Починить одно, сломав другое, — не починка: жалоба под каждым удачным
    пунктом отучает её читать ровно так же, как её отсутствие.
    """
    vyvod = _pult_pod_sh(punkt).stdout
    assert "PULT-ZHIV" in vyvod, "пульт закрылся на ровном месте: " + repr(vyvod)
    assert ("команда закончилась ошибкой" in vyvod) is zhdyom_zhalobu, repr(vyvod)


@pytest.mark.skipif(SH is None, reason="нет sh")
@pytest.mark.parametrize("lokal", ["C.UTF-8", "C"])
def test_shirina_stroki_schitaetsya_v_znakah_a_ne_v_baytah(lokal):
    """Ширина строки — знаки на экране, а не байты в памяти.

    `wc -m` считает знаки не сам по себе, а по локали: под `LANG=C` он считает
    байты, кириллица в UTF-8 двухбайтная — и русская подпись объявляется вдвое
    шире, чем занимает. Рамка после этого разъезжается, а бегущая строка бежит
    там, где влезала целиком.
    """
    v_cvete = "x" + chr(92) + "033[32mабвгдежзий" + chr(92) + "033[0mx"
    skript = chr(10).join([
        "TUI_ESC=" + _ESC_SH,
        _telo_funktsii("tui_shirina", source()),
        "printf 'lat=%s" + chr(92) + "n' \"$(tui_shirina 'abcdefghij')\"",
        "printf 'kir=%s" + chr(92) + "n' \"$(tui_shirina 'абвгдежзий')\"",
        "printf 'cvet=%s" + chr(92) + "n' \"$(tui_shirina \"$(printf '"
        + v_cvete + "')\")\"",
    ])
    gotovo = _pod_sh(skript, lokal)
    assert gotovo.returncode == 0, gotovo.stderr
    zamer = dict(s.split("=", 1) for s in gotovo.stdout.split())
    assert zamer["lat"] == "10", zamer
    assert zamer["kir"] == "10", (
        f"русская строка в локали {lokal} померена в байтах: {zamer}"
    )
    assert zamer["cvet"] == "12", (
        f"раскраска посчитана за ширину в локали {lokal}: {zamer}"
    )


#: Шапка живого меню собирается из сводки — подкладываем её целиком.
_POLE_ZAGLUSHKA = chr(10).join([
    "tui_pole() {",
    '    case "$1" in',
    "        url) printf 'https://ochen-dlinnyy-domen-dlya-proverki-ramki.example.com' ;;",
    "        zdorov) printf '1' ;;",
    "        obsluzhivanie) printf '0' ;;",
    "        konteynerov) printf '5' ;;",
    "        zhivyh) printf '4' ;;",
    "        legli) printf 'redis' ;;",
    "        versiya) printf '1.0.7-abcdef0' ;;",
    "        obnova) printf 'есть' ;;",
    "        avto) printf 'включено' ;;",
    "        poslednee) printf 'вчера в 03:14 — обновление откатилось, схема не сошлась' ;;",
    "        disk) printf '27G' ;;",
    "    esac",
    "}",
])

#: Всё, из чего собирается кадр. Берётся из скрипта как есть: подменённая
#: половина проверяла бы саму себя.
_KADR_FUNKTSII = (
    "tr_", "tui_shirina", "tui_srez", "tui_ramki", "tui_obrezat", "tui_liniya",
    "tui_stroka_ramki", "tui_vertushka", "tui_begushchaya", "tui_shapka",
    "tui_punkty", "tui_pole_stroki", "tui_narisovat",
)


def _kadr_pod_sh(stolbcov: int, lokal: str) -> list:
    """Рисует НАСТОЯЩИЙ кадр меню в окне заданной ширины.

    Отдаёт пары «ширина в знаках, строка без раскраски» — то есть то, что
    увидит терминал, а не то, что записано в исходнике.
    """
    text = source()
    skript = [
        "TUI_ESC=" + _ESC_SH,
        "TUI_KE=$(printf '" + chr(92) + "033[K')",
        "B=$(printf '\\033[1m'); D=$(printf '\\033[2m'); R=$(printf '\\033[0m')",
        "GREEN=$(printf '\\033[32m'); YELLOW=$(printf '\\033[33m')",
        "RED=$(printf '\\033[31m'); CYAN=$(printf '\\033[36m')",
        "UI_LANG=ru",
        "TUI_SVODKA=/takogo-fayla-net",
        # Свой же PID: сборщик считается живым, и в шапку попадает вертушка —
        # её ширину тоже надо мерить.
        "TUI_SBOR_PID=$$",
        "TUI_VERTUSHKA_KADR=3",
        f"TUI_STOLBCOV={stolbcov}",
        _POLE_ZAGLUSHKA,
    ]
    for imya in _KADR_FUNKTSII:
        # Терминала в наборе тестов нет: кадр уходит в обычный вывод.
        skript.append(_telo_funktsii(imya, text).replace("> /dev/tty", ""))
    skript.append("tui_narisovat glavnoe 3")
    gotovo = _pod_sh(chr(10).join(skript), lokal)
    assert gotovo.returncode == 0, gotovo.stderr
    bez_cveta = re.compile(chr(27) + r"\[[0-9;?]*[A-Za-z]")
    golye = [bez_cveta.sub("", s) for s in gotovo.stdout.split(chr(10))]
    return [(len(s), s) for s in golye]


@pytest.mark.skipif(SH is None, reason="нет sh")
@pytest.mark.parametrize("lokal", ["C.UTF-8", "C"])
@pytest.mark.parametrize("stolbcov", [60, 71, 100])
def test_kadr_pulta_ostayotsya_pryamougolnym(stolbcov, lokal):
    """Ни одна строка кадра не шире окна, и рамка остаётся прямоугольной.

    Шестьдесят колонок — самое узкое окно, в котором живое меню вообще
    соглашается работать, и именно там кадр разъезжался: третья строка шапки и
    подсказка внизу длиннее рамки просто по своему тексту.

    Прямоугольность стережёт вторую половину беды. Ширина, посчитанная в
    байтах, делает строки с кириллицей то короче, то длиннее — рамка перестаёт
    быть рамкой, хотя ни одна строка за окно и не вылезает.
    """
    stroki = _kadr_pod_sh(stolbcov, lokal)
    shirokie = [t for n, t in stroki if n > stolbcov]
    assert not shirokie, (
        f"строка шире окна в {stolbcov} колонок — терминал перенесёт её, и весь "
        "кадр съедет на строку вниз:" + chr(10) + chr(10).join(shirokie)
    )
    ramka = [n for n, t in stroki if t[:1] in ("│", "╭", "╰", "|", "+")]
    assert len(ramka) >= 5, (
        f"рамка не нарисовалась ({len(ramka)} строк) — проверка мерила бы пустоту"
    )
    assert len(set(ramka)) == 1, (
        f"рамка разъехалась, ширина строк разная: {sorted(set(ramka))}"
    )


def test_sekret_trevog_odin_na_oba_kontejnera():
    """Разошедшиеся стороны молча перестают доставлять тревоги.

    Приложение проверяет заголовок, Alertmanager его шлёт. Задай их порознь — и
    в день, когда значения разъедутся, тревоги перестанут доходить БЕЗ единого
    признака: не доедет и тревога про эту же поломку. Поэтому переменная одна и
    та же, и обе службы читают её из одного файла.
    """
    compose = (SCRIPT.parent / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    # Считаем СТРОКИ, а не вхождения: в строке `X: ${X:-}` имя стоит дважды, и
    # проверка «вхождений хотя бы два» зеленела на одной службе из двух —
    # поймано подрывом.
    sluzhb = sum(
        1 for stroka in compose.splitlines()
        if stroka.strip().startswith("OPENCRM_ALERTS_SECRET:")
    )
    assert sluzhb >= 2, (
        f"секрет тревог уходит в {sluzhb} службу из двух — приложение и "
        "Alertmanager разъедутся, и доставка встанет молча"
    )

    tochka = (
        SCRIPT.parent / "docker" / "monitoring" / "alertmanager" / "entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "__ALERTS_SECRET__" in tochka, (
        "точка входа Alertmanager не подставляет секрет в конфиг — заголовок "
        "уедет с меткой-заглушкой"
    )

    shablon = (
        SCRIPT.parent / "docker" / "monitoring" / "alertmanager"
        / "alertmanager-crm.yml.template"
    ).read_text(encoding="utf-8")
    assert "X-OpenCRM-Alerts-Key" in shablon and "__ALERTS_SECRET__" in shablon, (
        "в шаблоне конфига нет заголовка с ключом — приложение будет ждать того, "
        "чего никто не шлёт"
    )

    assert "configure_alerts_secret" in source(), (
        "установщик не заводит секрет — свежая установка останется с открытым "
        "приёмом, о котором никто не узнает"
    )


def test_doctor_otlichaet_perevody_strok_ot_pravok():
    """Диагностика не пугает «автообновление остановится» там, где правки —
    только переводы строк: обновлятор такое дерево терпит (deploy/updater.py),
    и строка «переводы строк» называет число смешанных файлов и лекарство."""
    text = source()
    section = text[text.index("cmd_doctor() {") : text.index("why_down() {")]
    assert "--ignore-cr-at-eol" in section, "doctor не отличает переводы строк от правок"
    assert "ls-files --eol" in section and "i/mixed" in section, "нет строки про смешанные переводы строк"
