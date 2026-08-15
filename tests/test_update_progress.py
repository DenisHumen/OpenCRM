"""Ход обновления на странице обслуживания.

Пока идёт обновление, приложения нет: заглушку отдаёт nginx, и всё, что
посетитель может узнать о происходящем, он узнаёт из одного файла. Файл пишут
ДВА разных места — `deploy/updater.py` с хоста и `docker/entrypoint.sh` изнутри
контейнера, — а читает третье, встроенный скрипт заглушки. Ни один из троих не
импортирует остальных, и разъехаться они могут молча: страница просто перестанет
показывать шаги — ровно тогда, когда сайт лежит и посмотреть больше некуда.
Отсюда сторожа на согласие всех троих и на путь, по которому файл доезжает.

Проверки читают файлы как текст: формат их меняется редко, а тащить ради тестов
разборщик YAML или JS в зависимости не стоит.
"""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deploy.updater import (
    PROGRESS_ERROR_LIMIT,
    PROGRESS_NAME,
    PROGRESS_STEPS,
    STATUS_ABORTED,
    STATUS_BROKEN,
    STATUS_DEPLOYED,
)
from tests.test_autoupdate import (
    NEW,
    FakeProbe,
    FakeShell,
    damp_snimaetsya,
    make_config,
    make_updater,
)

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docker" / "nginx" / "maintenance" / "maintenance.html"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
LOCATIONS = ROOT / "docker" / "nginx" / "templates" / "locations.inc"
COMPOSE = ROOT / "docker" / "docker-compose.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def state_path(config) -> Path:
    return config.data_dir.parent / "storage" / "branding" / PROGRESS_NAME


def state_of(config) -> dict:
    return json.loads(state_path(config).read_text("utf-8"))


class WatchingProbe(FakeProbe):
    """Тот же дубль health-check'а, но запоминает шаг, объявленный в тот момент."""

    def __init__(self, seen, config, **extra):
        super().__init__(**extra)
        self.seen = seen
        self.config = config

    def get(self, url, follow=True):
        self.seen.setdefault("health", state_of(self.config)["step"])
        return super().get(url, follow)


# --- обновление рассказывает о себе ---


def test_the_update_writes_its_progress_where_nginx_already_serves_it(tmp_path):
    """Путь — половина задачи: приложения в этот момент нет, отдать файл может
    только nginx, и только из каталога, который он уже раздаёт. Свой `location`
    начал бы работать со следующего обновления, а не с этого."""
    config = make_config(tmp_path)
    updater = make_updater(tmp_path, config=config)

    assert updater.run_once().status == STATUS_DEPLOYED

    assert state_path(config).is_file(), "странице обслуживания читать нечего"
    assert "alias /srv/storage/branding/" in _read(LOCATIONS), (
        "nginx больше не раздаёт этот каталог — ход обновления никуда не доедет"
    )
    assert "/storage:/srv/storage:ro" in _read(COMPOSE), "storage не примонтирован в nginx"
    # Именно значение STATE_URL, а не вхождение строки: имя файла поминается на
    # странице и в комментариях, и проверка «встречается где-то» осталась бы
    # зелёной, даже начни скрипт стучаться совсем в другое место.
    asked = re.search(r'var STATE_URL = "([^"]+)"', _read(PAGE))
    assert asked and asked.group(1) == f"/branding/{PROGRESS_NAME}", (
        "страница спрашивает адрес, которого nginx без приложения не отдаёт"
    )


def test_each_step_is_announced_while_it_is_happening(tmp_path):
    """Не «после», а «во время»: страницу читают ровно в ту минуту, пока шаг идёт.

    Объяви мы шаг после его окончания — посетитель весь долгий `--build` смотрел
    бы на «снимаем копию базы», то есть на уже неверное.
    """
    config = make_config(tmp_path)
    seen: dict[str, str] = {}
    shell = FakeShell()
    shell.effect(
        "scripts.snapshot_db dump",
        lambda: (seen.setdefault("backup", state_of(config)["step"]),
                 damp_snimaetsya(config, shell)()),
    )
    shell.effect("up -d --build", lambda: seen.setdefault("build", state_of(config)["step"]))
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=WatchingProbe(seen, config)
    )

    assert updater.run_once().status == STATUS_DEPLOYED

    assert seen == {"backup": "backup", "build": "build", "health": "health"}


def test_a_successful_update_stops_drawing_the_steps(tmp_path):
    """Готовый список перед самым уходом на сайт только мигнул бы напоследок."""
    config = make_config(tmp_path)

    assert make_updater(tmp_path, config=config).run_once().status == STATUS_DEPLOYED

    assert state_of(config)["phase"] == "done"


def test_a_dead_site_leaves_the_step_and_the_reason_on_the_page(tmp_path):
    """Единственный случай, когда страницу с провалом вправду увидят: откат тоже
    не поднялся, сайт лежит, и человек с той стороны имеет право знать, где."""
    config = make_config(tmp_path)
    updater = make_updater(tmp_path, config=config, probe=FakeProbe(health=(False, False)))

    assert updater.run_once().status == STATUS_BROKEN

    state = state_of(config)
    assert state["phase"] == "failed"
    assert state["step"] == "health"
    assert "health-check" in state["error"], "провал без причины — половина новости"


def test_an_update_that_never_touched_the_site_leaves_nothing_behind(tmp_path):
    """Сайт работал всё это время, страницы обслуживания никто не видел.

    Оставленное «не удалось» всплыло бы при следующем — уже постороннем —
    падении сайта и соврало бы про его причину.
    """
    config = make_config(tmp_path)
    # Копию класть некуда: на её месте каталог, а не файл.
    (config.state_dir / f"pre-update-{NEW[:12]}.sql").mkdir()

    assert make_updater(tmp_path, config=config).run_once().status == STATUS_ABORTED

    assert not state_path(config).exists()


def test_the_progress_file_is_never_half_written(tmp_path):
    """Страницу открывают в произвольный момент; огрызок JSON она не разберёт."""
    config = make_config(tmp_path)
    make_updater(tmp_path, config=config).run_once()

    assert [item.name for item in state_path(config).parent.iterdir()] == [PROGRESS_NAME]


def test_a_broken_state_directory_does_not_break_the_update(tmp_path):
    """Ход — удобство, а не часть обновления: своё же удобство ронять его не вправе."""
    config = make_config(tmp_path)
    # Каталога с таким именем не создать: на его месте файл.
    (config.data_dir.parent / "storage").parent.mkdir(parents=True, exist_ok=True)
    (config.data_dir.parent / "storage").write_text("не каталог", encoding="utf-8")

    assert make_updater(tmp_path, config=config).run_once().status == STATUS_DEPLOYED


# --- согласие трёх сторон ---


def _page_block(name: str) -> str:
    found = re.search(r"var " + name + r" = \{(.*?)\n  \};", _read(PAGE), re.S)
    assert found, f"на странице больше нет {name} — тест смотрит не туда"
    return found.group(1)


def test_the_page_the_updater_and_the_container_agree_on_the_steps():
    """Разъедься ключи — страница нарисует пустой список или чужой шаг."""
    labels = set(re.findall(r"(\w+):", _page_block("STEP_LABELS")))
    assert labels == set(PROGRESS_STEPS), "подписи шагов и PROGRESS_STEPS разошлись"

    order = _page_block("STEP_ORDER")
    full = re.search(r"update:\s*\[(.*?)\]", order, re.S)
    assert full and tuple(re.findall(r'"(\w+)"', full.group(1))) == PROGRESS_STEPS, (
        "порядок шагов на странице не тот, в котором их объявляет обновление"
    )
    short = re.search(r"restart:\s*\[(.*?)\]", order, re.S)
    assert short, "страница не умеет показывать обычный перезапуск контейнера"

    written_by_container = set(re.findall(r"write_state \w+ (\w+)", _read(ENTRYPOINT)))
    assert written_by_container <= set(PROGRESS_STEPS), "контейнер пишет неизвестный странице шаг"
    assert {"migrate", "start"} <= written_by_container, (
        "миграции и старт объявляет только контейнер — без них середина пути пропадёт"
    )


def test_the_page_reads_exactly_the_fields_that_are_written(tmp_path):
    """Лишнее поле на странице читается как undefined и молча ничего не рисует."""
    config = make_config(tmp_path)
    make_updater(tmp_path, config=config, probe=FakeProbe(health=(False, False))).run_once()
    written = set(state_of(config))

    # Дефис в отрицательном просмотре назад — из-за имени самого файла:
    # `update-state.json` иначе засчитался бы за обращение к полю `json`.
    assert set(re.findall(r"(?<![-\w])state\.(\w+)", _read(PAGE))) == written
    assert set(re.findall(r'"(\w+)":"%s"', _read(ENTRYPOINT))) == written, (
        "контейнер пишет не тот набор полей, что обновлятор"
    )


def test_every_way_the_container_gives_up_is_explained_on_the_page():
    """Молча упавший контейнер оставляет посетителя с вечным «обновляем базу».

    Такой отказ уходит в цикл перезапусков, то есть сайт не поднимется уже
    никогда, а на странице ничто на это не намекнёт.
    """
    lines = _read(ENTRYPOINT).splitlines()
    checked = 0
    for number, line in enumerate(lines):
        if not re.match(r"exit\s+\S", line.strip()):
            continue
        checked += 1
        window = " ".join(lines[max(0, number - 5) : number])
        assert "write_state failed" in window, f"{ENTRYPOINT.name}:{number + 1}: выход без объяснения"
    assert checked >= 3, "выходов оказалось меньше, чем было, — тест устарел"


# --- то, что страница уже умела и не должна разучиться ---


def test_the_progress_never_takes_the_visitor_off_the_page():
    """Уход со страницы бывает ровно в двух местах и только после отправки счёта.

    Третий, добавленный ради «сайт уже готов», обрывал бы партию в змейку и
    терял бы результат ровно в тот момент, когда его наконец есть куда отдать.
    """
    page = _read(PAGE)
    assert page.count("location.replace") == 2
    assert "location.reload" not in page


def test_the_progress_is_hidden_until_there_is_something_to_show():
    """Файла может не быть вовсе: сайт лёг не из-за обновления, стек подняли
    руками, storage смонтирован по-своему. Тогда страница — как раньше."""
    assert re.search(r'<div class="progress" id="progress" hidden>', _read(PAGE))


def test_the_progress_is_asked_for_from_our_own_site():
    """Внешний адрес вёл бы на тот же лежащий сайт — или на чужой."""
    url = re.search(r'var STATE_URL = "([^"]+)"', _read(PAGE))
    assert url and url.group(1).startswith("/"), "адрес хода обновления перестал быть своим"


def test_the_progress_does_not_start_a_second_timer():
    """Отдельный таймер продолжал бы стучаться и после возвращения на сайт —
    и, что хуже, тикал бы во время партии, которую страница обязана доиграть."""
    page = _read(PAGE)
    assert "askProgress();" in page
    assert page.count("setInterval") == 0
    assert "setTimeout(poll" in page


# --- сам shell, а не пересказ о нём ---
#
# Двойников здесь нет намеренно: беда в этом коде будет не логической, а
# шелловой — недоэкранированная кавычка, `set -e` на ровном месте, sed, который
# ничего не нашёл. Такое видно только настоящему sh.

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="нужен POSIX sh")

RUNNING = (
    '{"scope":"update","phase":"running","step":"build",'
    '"started_at":"2020-01-01T00:00:00Z","error":""}'
)


def write_state(tmp_path, phase, step, error="", previous=None) -> dict:
    """Позвать `write_state` из entrypoint.sh, не запуская всё остальное.

    Куски вырезаются из файла, а не переписываются в тест: копия разошлась бы с
    оригиналом в первый же день, и проверять мы стали бы её, а не то, что вправду
    выполняется в контейнере.
    """
    text = _read(ENTRYPOINT)
    pieces = re.findall(r"^(?:STATE_DIR|STATE_FILE)=.*$", text, re.M)
    pieces += re.findall(r"^(?:json_escape|write_state)\(\)\s*\{.*?^\}$", text, re.S | re.M)
    assert len(pieces) == 4, "разметка entrypoint.sh изменилась — тест вырезает не то"

    storage = tmp_path / "storage"
    (storage / "branding").mkdir(parents=True, exist_ok=True)
    landing = storage / "branding" / PROGRESS_NAME
    if previous is not None:
        landing.write_text(previous, encoding="utf-8")

    environment = dict(os.environ)
    # Прямые слэши: под Windows тест гоняется через sh из Git for Windows, и
    # обратный слэш в пути — для него экранирование, а не разделитель.
    environment["OPENCRM_STORAGE_DIR"] = str(storage).replace("\\", "/")
    subprocess.run(
        ["sh", "-s", phase, step, error],
        input="\n".join(pieces) + '\nwrite_state "$1" "$2" "$3"\n',
        text=True,
        env=environment,
        capture_output=True,
        check=True,
    )
    return json.loads(landing.read_text("utf-8"))


@needs_sh
def test_the_container_writes_json_the_page_can_parse(tmp_path):
    state = write_state(tmp_path, "running", "migrate")

    assert state["phase"] == "running" and state["step"] == "migrate"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", state["started_at"]), (
        "без пометки зоны браузер разберёт время как своё местное"
    )


@needs_sh
def test_a_plain_restart_does_not_pretend_to_be_an_update(tmp_path):
    """Ни образа не собирали, ни копии базы с хоста не снимали — и отмечать эти
    шаги пройденными значило бы соврать о том, чего не было.

    Второй шаг проверяется отдельно, и не для полноты: контейнер за один запуск
    пишет дважды, и его же первая запись выглядит для второй ровно как чужая,
    оставленная обновлятором. Поймано прогоном entrypoint.sh с подставными
    python и sqlite3 — на «старте» перезапуск объявлял себя обновлением.
    """
    first = write_state(tmp_path, "running", "migrate")
    assert first["scope"] == "restart"

    second = write_state(tmp_path, "running", "start", previous=json.dumps(first))
    assert second["scope"] == "restart"
    assert second["started_at"] == first["started_at"], "внутри одного запуска отсчёт один"


def test_vtoraya_zapis_prodolzhaet_otschyot_a_ne_nachinaet_zanovo(tmp_path):
    """Отсчёт один на запуск, даже если записи легли в РАЗНЫЕ секунды.

    Проверка выше это и требует, но доказать не может: обе её записи обычно
    попадают в одну секунду, и она была зелёной при сломанном наследовании. Так
    и вышло — когда наследование у перезапуска сняли целиком, она краснела не
    всегда, а примерно раз из нескольких прогонов, и выглядело это миганием.

    Здесь разрыв задан нарочно: `started_at` отодвинут на пять секунд назад. Без
    наследования вторая запись поставила бы своё время, и на странице обратный
    отсчёт дёрнулся бы назад между двумя опросами.

    Пять секунд, а не полчаса, — это по-прежнему СВОЯ запись из этого же
    запуска: между «миграции» и «старт» столько и проходит. Чужая, оставшаяся от
    прожившего своё контейнера, наследоваться не должна, и об этом парная
    проверка `test_obychnyy_perezapusk_nachinaet_otschyot_zanovo`.
    """
    davecha = datetime.now(timezone.utc) - timedelta(seconds=5)
    ranshe = {
        "scope": "restart",
        "phase": "running",
        "step": "migrate",
        "started_at": davecha.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": "",
    }
    vtoraya = write_state(tmp_path, "running", "start", previous=json.dumps(ranshe))

    assert vtoraya["scope"] == "restart"
    assert vtoraya["started_at"] == ranshe["started_at"], (
        "вторая запись за тот же запуск начала отсчёт заново — на странице "
        "обратный отсчёт дёрнется назад"
    )


@needs_sh
def test_the_container_keeps_the_start_time_of_an_update_in_progress(tmp_path):
    """Контейнер поднимается уже посреди обновления и начала не знает.

    Запиши он своё время — страница показала бы, что обновление только-только
    началось, ровно в ту минуту, когда оно идёт уже двадцать.
    """
    state = write_state(tmp_path, "running", "migrate", previous=RUNNING)

    assert state["started_at"] == "2020-01-01T00:00:00Z"
    assert state["scope"] == "update", "шаги обновления показались бы как перезапуск"


@needs_sh
@pytest.mark.parametrize("phase", ["done", "failed"])
def test_a_finished_update_does_not_lend_its_clock_to_the_next_restart(tmp_path, phase):
    """Иначе перезапуск через неделю сообщил бы, что идёт с прошлого вторника."""
    stale = RUNNING.replace('"phase":"running"', f'"phase":"{phase}"')

    state = write_state(tmp_path, "running", "migrate", previous=stale)

    assert state["started_at"] != "2020-01-01T00:00:00Z"
    assert state["scope"] == "restart"


@needs_sh
def test_a_migration_error_cannot_break_the_json(tmp_path):
    """В хвосте чужого вывода бывают и кавычки, и слэши, и переводы строк.

    Любой из них превращает файл в мусор, а мусор страница молча пропускает —
    то есть провал миграции пропал бы ровно там, где он важнее всего.
    """
    nasty = 'OperationalError: no such column "x"\\y\nDETAIL:\tсправа'

    state = write_state(tmp_path, "failed", "migrate", nasty)

    assert state["phase"] == "failed" and state["step"] == "migrate"
    assert "OperationalError" in state["error"]
    assert '"' not in state["error"] and "\n" not in state["error"]


def test_the_updater_cuts_the_reason_short(tmp_path):
    """Файл публичен: его читает страница, за которой нет ни сессии, ни приложения."""
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.fail("up -d --build", err="ERROR " * 400)
    make_updater(tmp_path, config=config, shell=shell).run_once()

    assert 0 < len(state_of(config)["error"]) <= PROGRESS_ERROR_LIMIT


@needs_sh
def test_the_container_cuts_the_reason_short_too(tmp_path):
    """Предел один на обоих писателей — иначе он был бы не правилом, а привычкой."""
    state = write_state(tmp_path, "failed", "migrate", "boom " * 400)

    assert 0 < len(state["error"]) <= PROGRESS_ERROR_LIMIT


@needs_sh
def test_an_unwritable_storage_does_not_stop_the_container(tmp_path):
    """Наверху entrypoint.sh стоит `set -e`, и упавшая запись хода уронила бы
    запуск целиком — ради строчки, которую всего лишь не нарисуют."""
    text = _read(ENTRYPOINT)
    pieces = re.findall(r"^(?:STATE_DIR|STATE_FILE)=.*$", text, re.M)
    pieces += re.findall(r"^(?:json_escape|write_state)\(\)\s*\{.*?^\}$", text, re.S | re.M)
    blocked = tmp_path / "storage"
    blocked.write_text("не каталог", encoding="utf-8")  # mkdir -p по такому пути не пройдёт

    environment = dict(os.environ)
    environment["OPENCRM_STORAGE_DIR"] = str(blocked).replace("\\", "/")
    done = subprocess.run(
        ["sh", "-s"],
        input="set -e\n" + "\n".join(pieces) + '\nwrite_state running migrate ""\necho жив\n',
        text=True,
        env=environment,
        capture_output=True,
    )

    assert done.returncode == 0, done.stderr
    assert "жив" in done.stdout
