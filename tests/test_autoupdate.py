"""Автообновление сайта (пакет `deploy`).

Сценарий деплоя целиком проверяется здесь, а не руками на сервере: откат
случается ровно тогда, когда всё уже плохо, и «проверю, когда понадобится» для
него не работает. Внешний мир — команды, HTTP, GitHub, Telegram — подменяется
двойниками из `runner`-контракта, поэтому прогон не собирает образ и не поднимает
сервер.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from deploy import notify
from deploy.config import UpdateConfig
from deploy.github import (
    CHECKS_FAILURE,
    CHECKS_PENDING,
    CHECKS_SUCCESS,
    Checks,
    GitHub,
    GitHubError,
    Head,
)
from deploy.journal import Journal
from deploy.runner import Response, Result, Shell
from deploy.updater import (
    STATUS_ABORTED,
    STATUS_BROKEN,
    STATUS_DEPLOYED,
    STATUS_DISABLED,
    STATUS_ROLLED_BACK,
    SNAPSHOTS_KEPT,
    STATUS_UP_TO_DATE,
    STATUS_WAITING,
    Updater,
)

OLD = "a" * 40
NEW = "b" * 40


# --- двойники внешнего мира ---


class FakeShell:
    """Команды не запускаются, а записываются; ответы задаются правилами."""

    def __init__(self):
        self.calls: list[str] = []
        self.rules: list[tuple[str, int, str, str]] = []
        self.effects: list = []
        self.head = OLD
        self.dirty = ""
        #: Что и с каким файлом на входе звали. Возврат базы MySQL — это
        #: заливка дампа клиенту `mysql` на stdin, и без записи этого аргумента
        #: «откат сработал» нельзя отличить от «команда позвана впустую».
        self.stdins: list[tuple[str, str]] = []

    def fail(self, needle: str, err: str = "boom"):
        self.rules.append((needle, 1, "", err))

    def otvet(self, needle: str, out: str) -> None:
        """Удачный ответ с заданным выводом — для `git diff` и `compose config`."""
        self.rules.append((needle, 0, out, ""))

    def effect(self, needle: str, action):
        self.effects.append((needle, action))

    def run(self, argv, cwd=None, timeout=None, stdin=None):
        line = " ".join(str(part) for part in argv)
        self.calls.append(line)
        if stdin is not None:
            self.stdins.append((line, str(stdin)))
        for needle, action in self.effects:
            if needle in line:
                action()
        for needle, code, out, err in self.rules:
            if needle in line:
                return Result(tuple(argv), code, out, err)
        if "rev-parse HEAD" in line:
            return Result(tuple(argv), 0, self.head, "")
        if "status --porcelain" in line:
            return Result(tuple(argv), 0, self.dirty, "")
        return Result(tuple(argv), 0, "", "")

    def ran(self, needle: str) -> bool:
        return any(needle in call for call in self.calls)


class FakeProbe:
    """Планы ответов: каждый вызов забирает следующий, последний повторяется.

    План, а не флаг, потому что откат проверяет здоровье второй раз — и его
    ответ должен отличаться от того, из-за которого откат случился.
    """

    def __init__(self, health=(True,), smoke=(True,), smoke_status=200, obsluzhivanie=False):
        self.health = list(health)
        self.smoke = list(smoke)
        # Закрыт ли сайт на работы. Настоящий `/healthz` это поле отдаёт всегда
        # (web/main.py), и подпись дубля обязана совпадать с настоящей — иначе
        # проверка стережёт не тот код, который работает.
        self.obsluzhivanie = obsluzhivanie
        # Чем именно отвечает smoke-адрес, когда он «живой». На боевом сервере с
        # HTTPS это 301 на https://…, а не 200: подпись дубля обязана совпадать
        # с настоящей, иначе тест проверяет не тот код, который работает.
        self.smoke_status = smoke_status
        self.calls: list[str] = []
        self.followed: list[bool] = []

    @staticmethod
    def _next(plan: list[bool]) -> bool:
        return plan.pop(0) if len(plan) > 1 else plan[0]

    def get(self, url, follow=True):
        self.calls.append(url)
        self.followed.append(follow)
        if "healthz" in url:
            ok = self._next(self.health)
            rezhim = "on" if self.obsluzhivanie else "off"
            telo = '{"status": "ok", "maintenance": "%s"}' % rezhim
            return Response(200, telo) if ok else Response(502, "bad gateway")
        ok = self._next(self.smoke)
        return Response(self.smoke_status, "<html>ok</html>") if ok else Response(500, "")


class FakeGitHub:
    def __init__(
        self,
        sha=NEW,
        changed=True,
        etag='W/"new"',
        summary="feat: новая витрина",
        checks=Checks(CHECKS_SUCCESS, "зелёных проверок: 1"),
    ):
        self._head = Head(sha=sha if changed else "", etag=etag, changed=changed)
        self._summary = summary
        # По умолчанию зелёные: гейт — предохранитель, и в тестах про откат,
        # health-check и снимок базы он должен молчать, а не быть фоном.
        self._checks = checks
        self.etags_seen: list[str] = []
        self.checks_asked: list[str] = []

    def head(self, branch, etag=""):
        self.etags_seen.append(etag)
        return self._head

    def summary(self, sha):
        return self._summary

    def checks(self, sha):
        self.checks_asked.append(sha)
        if isinstance(self._checks, Exception):
            raise self._checks
        return self._checks


class FakeNotifier:
    configured = True

    def __init__(self):
        self.messages: list[str] = []
        #: Со звуком или без — часть исхода, а не мелочь: удачное ночное
        #: обновление не должно будить, а упавшее обязано.
        self.tihie: list[bool] = []

    def send(self, text, *, tiho=False):
        self.messages.append(text)
        self.tihie.append(tiho)
        return True

    @property
    def plain(self) -> list[str]:
        """Сообщения без разметки — по ним удобно проверять смысл, а не теги."""
        from deploy.notify import bez_razmetki

        return [bez_razmetki(m) for m in self.messages]


# --- обвязка ---


#: Адрес боевой базы. Он же умолчание обвязки: другой базы у продукта нет,
#: и «обычный» прогон обязан идти по боевому пути, а не по исключению.
MYSQL_URL = "mysql+pymysql://opencrm:parol@db:3306/opencrm?charset=utf8mb4"

#: Хвост, по которому и только по которому копия считается снятой целиком.
#: Ту же строку пишет `scripts/snapshot_db.py` — за совпадением следит
#: `tests/test_pre_migrate_snapshot.py`.
METKA_DAMPA = "-- opencrm snapshot complete: таблиц 37, строк 2700347"


def make_config(tmp_path, **extra) -> UpdateConfig:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    env = {
        "OPENCRM_HOME": str(tmp_path / "home"),
        "OPENCRM_UPDATE_DB_URL": MYSQL_URL,
        "OPENCRM_UPDATE_PROJECT_DIR": str(repo),
        "OPENCRM_UPDATE_HEALTH_ATTEMPTS": "1",
        "OPENCRM_UPDATE_HEALTH_DELAY": "0",
        "OPENCRM_UPDATE_SMOKE_URLS": "http://127.0.0.1/",
    }
    env.update(extra)
    config = UpdateConfig.from_env(env)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def damp_snimaetsya(config, shell, celyy: bool = True):
    """Подставной `snapshot_db dump`: кладёт файл туда, куда его попросили."""

    def sdelat():
        put = shell.calls[-1].rsplit("/app/data/", 1)[1].split()[0]
        soderzhimoe = "INSERT INTO clients VALUES (1);\n"
        if celyy:
            soderzhimoe += METKA_DAMPA + "\n"
        (config.data_dir / put).write_text(soderzhimoe, encoding="utf-8")

    return sdelat


def make_updater(tmp_path, *, shell=None, probe=None, github=None, config=None, **extra):
    config = config or make_config(tmp_path, **extra)
    shell = shell or FakeShell()
    # Копия перед миграциями обязательна: без неё деплой не начинается вовсе
    # (миграции вперёд необратимы). Значит подставной дампер нужен КАЖДОМУ
    # прогону, а не только тем, кто проверяет саму копию.
    if not any("snapshot_db dump" in needle for needle, _ in shell.effects):
        shell.effect("scripts.snapshot_db dump", damp_snimaetsya(config, shell))
    return Updater(
        config,
        journal=Journal(config.state_file, config.history_file),
        github=github or FakeGitHub(),
        shell=shell,
        probe=probe or FakeProbe(),
        notifier=FakeNotifier(),
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
    )


def step_names(outcome):
    return [step.name for step in outcome.steps]


def blob(text: str) -> bytes:
    return text.encode("utf-8")


def once(action):
    """Побочный эффект только на первый вызов — `up -d --build` бывает и в откате."""
    fired = []

    def wrapper():
        if not fired:
            fired.append(True)
            action()

    return wrapper


# --- настройки ---


def test_defaults_follow_the_compose_home(tmp_path):
    config = UpdateConfig.from_env({"OPENCRM_HOME": str(tmp_path / "opencrm")})
    assert config.state_dir == tmp_path / "opencrm" / "updates"
    assert config.data_dir == tmp_path / "opencrm" / "data"
    assert config.data_dir == tmp_path / "opencrm" / "data"
    assert config.branch == "main"


def test_environment_overrides_everything(tmp_path):
    config = UpdateConfig.from_env(
        {
            "OPENCRM_HOME": str(tmp_path),
            "OPENCRM_UPDATE_BRANCH": "release",
            "OPENCRM_UPDATE_POLL_SECONDS": "60",
            "OPENCRM_UPDATE_RUN_CHECKS": "0",
            "OPENCRM_UPDATE_SMOKE_URLS": "http://a/, http://b/",
        }
    )
    assert config.branch == "release"
    assert config.poll_seconds == 60
    assert config.run_checks is False
    assert config.smoke_urls == ("http://a/", "http://b/")


def test_the_ci_gate_is_on_unless_switched_off():
    """Предохранители по умолчанию взведены: забыть включить проще, чем выключить."""
    assert UpdateConfig.from_env({}).require_ci is True
    assert UpdateConfig.from_env({"OPENCRM_UPDATE_REQUIRE_CI": "0"}).require_ci is False


# --- журнал ---


def test_state_survives_a_round_trip(tmp_path):
    journal = Journal(tmp_path / "state.json", tmp_path / "history.jsonl")
    assert journal.read() == {}
    journal.write(deployed_sha=NEW, etag='W/"x"')
    assert journal.deployed_sha == NEW
    assert journal.etag == 'W/"x"'


def test_autoupdate_is_on_until_switched_off(tmp_path):
    journal = Journal(tmp_path / "state.json", tmp_path / "history.jsonl")
    assert journal.autoupdate_enabled is True
    journal.set_autoupdate(False)
    assert journal.autoupdate_enabled is False
    journal.set_autoupdate(True)
    assert journal.autoupdate_enabled is True


def test_history_reads_newest_first_and_respects_the_limit(tmp_path):
    journal = Journal(tmp_path / "state.json", tmp_path / "history.jsonl")
    for number in range(5):
        journal.append({"status": "deployed", "to_sha": str(number)})
    assert [record["to_sha"] for record in journal.history(3)] == ["4", "3", "2"]
    assert journal.last()["to_sha"] == "4"


def test_a_torn_line_does_not_hide_the_rest_of_the_history(tmp_path):
    history = tmp_path / "history.jsonl"
    journal = Journal(tmp_path / "state.json", history)
    journal.append({"status": "deployed", "to_sha": "1"})
    with history.open("a", encoding="utf-8") as stream:
        stream.write('{"status": "dep\n')  # обрыв на середине записи
    journal.append({"status": "deployed", "to_sha": "2"})
    assert [record["to_sha"] for record in journal.history(10)] == ["2", "1"]


def test_state_is_written_atomically(tmp_path):
    """Обновление может оборваться в любой момент — огрызка JSON остаться не должно."""
    journal = Journal(tmp_path / "state.json", tmp_path / "history.jsonl")
    journal.write(deployed_sha=OLD)
    assert json.loads((tmp_path / "state.json").read_text("utf-8"))["deployed_sha"] == OLD
    assert not list(tmp_path.glob("*.tmp"))


# --- preflight против настоящего git ---
#
# Здесь двойников нет намеренно. Обновление на боевом сервере встало намертво не
# на логике, а на двух особенностях самого git — на них подделка команд слепа:
# `git status` отвечал `detected dubious ownership`, а `chmod +x opencrm.sh` из
# инструкции по установке навсегда делал дерево «грязным». Обе беды видны только
# настоящему git, поэтому проверяем настоящим.

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="нужен настоящий git")


def real_repo(path, executable_file="opencrm.sh"):
    """Клон-двойник боевого чекаута: один коммит, один скрипт без бита исполнения."""
    path.mkdir(parents=True, exist_ok=True)
    script = path / executable_file
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    def git(*args, env=None):
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, env=env, check=False
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    # Как в репозитории на GitHub: файл записан режимом 100644 (см. `git
    # update-index --chmod=+x` — бит хранится в дереве, а не в файловой системе).
    git("config", "core.fileMode", "true")
    git("add", "-A")
    git("update-index", "--chmod=-x", executable_file)
    git("commit", "-qm", "init")
    return git


def real_updater(tmp_path, repo, **extra):
    config = make_config(tmp_path, OPENCRM_UPDATE_PROJECT_DIR=str(repo), **extra)
    return Updater(
        config,
        journal=Journal(config.state_file, config.history_file),
        github=FakeGitHub(),
        shell=Shell(),  # настоящий: проверяем ровно то, как git отвечает на наши флаги
        probe=FakeProbe(),
        notifier=FakeNotifier(),
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
    )


def test_every_git_command_carries_the_ownership_exception(tmp_path):
    """Не только `status`: `fetch` и `checkout` спотыкались бы о то же самое.

    Откат идёт через `checkout` — то есть без флага чужой владелец ронял бы не
    обновление, а возврат назад, ровно тогда, когда всё уже плохо.
    """
    updater = make_updater(tmp_path)
    updater.run_once()

    git_calls = [call for call in updater.shell.calls if call.startswith("git ")]
    assert git_calls, "git вообще не звали — тест ничего не проверяет"
    for call in git_calls:
        assert f"-c safe.directory={updater.config.project_dir}" in call


def test_only_the_cleanliness_check_ignores_the_executable_bit(tmp_path):
    """`core.fileMode=false` — точечно, а не на все команды подряд.

    Смысл он имеет ровно в одном месте, и раздавать его `checkout`/`fetch`
    значило бы глушить бит исполнения там, где о нём никто не просил.
    """
    updater = make_updater(tmp_path)
    updater.run_once()

    for call in updater.shell.calls:
        if not call.startswith("git "):
            continue
        assert ("core.fileMode=false" in call) == ("status --porcelain" in call), call


@needs_git
def test_chmod_from_the_install_instructions_does_not_jam_updates(tmp_path):
    """`chmod +x opencrm.sh` — не правка, а бит; дерево остаётся чистым.

    Именно это и заклинило боевой сервер: инструкция по установке взводила бит,
    git показывал `M opencrm.sh`, preflight отказывался обновляться — и так
    каждые пять минут, без единого способа догадаться, что «правку» никто не
    делал. `checkout` расставит бит из дерева сам, содержимого в нём нет.
    """
    repo = tmp_path / "checkout"
    real_repo(repo)
    (repo / "opencrm.sh").chmod(0o755)

    updater = real_updater(tmp_path, repo)
    steps = []
    updater._preflight(steps)  # не поднимает _Stop — это и есть проверка

    assert [(step.name, step.ok) for step in steps] == [("preflight", True)]


@needs_git
def test_a_real_edit_still_stops_the_update(tmp_path):
    """Обратная сторона: содержательную правку по-прежнему не затираем молча."""
    from deploy.updater import _Stop

    repo = tmp_path / "checkout"
    real_repo(repo)
    (repo / "opencrm.sh").write_text("#!/bin/sh\necho правка руками\n", encoding="utf-8")

    updater = real_updater(tmp_path, repo)
    with pytest.raises(_Stop, match="несохранённые правки"):
        updater._preflight([])


@needs_git
def test_a_repository_owned_by_someone_else_is_still_ours(tmp_path):
    """`sudo ./opencrm.sh` в каталоге, склонированном под другим пользователем.

    git отвечает на это `fatal: detected dubious ownership` и не выполняет
    ничего — ни `status`, ни `checkout`. На сервере из-за этого автообновление
    падало ещё до первой осмысленной проверки. Владельца в тестах не подделать,
    поэтому просим сам git считать, что владелец чужой (флаг из его тестового
    набора), — путь кода при этом ровно тот же.
    """
    repo = tmp_path / "checkout"
    real_repo(repo)
    updater = real_updater(tmp_path, repo)

    argv = ["git", "-C", str(repo), "status", "--porcelain"]
    hostile = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    if subprocess.run(argv, capture_output=True, text=True, env=hostile).returncode == 0:
        pytest.skip("этот git не умеет притворяться чужим владельцем")

    monkey = subprocess.run(
        updater._git("status", "--porcelain").argv, capture_output=True, text=True, env=hostile
    )
    assert monkey.returncode == 0, monkey.stderr
    assert "dubious ownership" not in monkey.stderr


# --- гейт по проверкам GitHub ---


def test_a_green_commit_goes_out(tmp_path):
    updater = make_updater(tmp_path)
    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert updater.github.checks_asked == [NEW]


def test_a_red_commit_never_reaches_the_server(tmp_path):
    """Красный CI — отказ до `fetch`: живой сайт не трогали, сборку не начинали."""
    github = FakeGitHub(checks=Checks(CHECKS_FAILURE, "pytest — failure"))
    updater = make_updater(tmp_path, github=github)
    shell = updater.shell

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "красные" in outcome.reason
    assert not shell.ran("fetch") and not shell.ran("checkout") and not shell.ran("up -d")
    # Коммит виноват — это событие: в историю, в Telegram и в failed_sha, чтобы
    # новость не повторялась каждые пять минут.
    assert updater.journal.read()["failed_sha"] == NEW
    assert updater.journal.last()["status"] == STATUS_ABORTED
    assert updater.notifier.messages


def test_a_commit_whose_checks_are_still_running_is_waited_for_not_rejected(tmp_path):
    """Ключевое отличие от красного: коммит не виноват, и клеймить его нельзя.

    Попади он в `failed_sha` — не поехал бы уже никогда, даже позеленев: повтор
    бывает только на следующем коммите или по `force-update`.
    """
    github = FakeGitHub(checks=Checks(CHECKS_PENDING, "pytest"))
    updater = make_updater(tmp_path, github=github)

    outcome = updater.run_once()

    assert outcome.status == STATUS_WAITING
    assert not updater.shell.ran("fetch")
    assert not updater.journal.read().get("failed_sha")
    # Ожидание — не новость: ни в историю, ни в Telegram.
    assert updater.journal.last() is None
    assert updater.notifier.messages == []


def test_waiting_forgets_the_etag_so_the_next_poll_asks_again(tmp_path):
    """Иначе гейт запирал бы коммит навсегда — самой же экономией запросов.

    ETag висит на голове ветки и не меняется, пока не появится новый коммит. Не
    сбросив его, следующий опрос получил бы 304 «ничего не изменилось», вышел
    через `up-to-date` и до проверок не дошёл: CI позеленел бы, а обновление
    осталось бы стоять до следующего коммита.
    """
    updater = make_updater(tmp_path, github=FakeGitHub(checks=Checks(CHECKS_PENDING, "pytest")))
    updater.run_once()
    assert updater.journal.etag == ""

    # CI позеленел — тот же самый коммит едет, повторного опроса ничто не глушит.
    updater.github._checks = Checks(CHECKS_SUCCESS, "зелёных проверок: 1")
    assert updater.run_once().status == STATUS_DEPLOYED


def test_github_being_unreachable_is_not_the_same_as_a_red_build(tmp_path):
    """Не спросили — не значит «плохо»: ждём, но коммит не клеймим."""
    github = FakeGitHub(checks=GitHubError("GitHub недоступен"))
    updater = make_updater(tmp_path, github=github)

    outcome = updater.run_once()

    assert outcome.status == STATUS_WAITING
    assert "недоступн" in outcome.reason
    assert not updater.journal.read().get("failed_sha")
    assert not updater.shell.ran("up -d")


def test_force_update_is_the_human_override(tmp_path):
    """Чинить упавший CI, не имея права выкатить фикс, было бы тупиком."""
    github = FakeGitHub(checks=Checks(CHECKS_FAILURE, "pytest — failure"))
    updater = make_updater(tmp_path, github=github)

    assert updater.run_once(force=True).status == STATUS_DEPLOYED
    assert github.checks_asked == []


def test_the_gate_can_be_switched_off_where_there_is_no_ci(tmp_path):
    github = FakeGitHub(checks=Checks(CHECKS_FAILURE, "pytest — failure"))
    updater = make_updater(tmp_path, github=github, OPENCRM_UPDATE_REQUIRE_CI="0")

    assert updater.run_once().status == STATUS_DEPLOYED
    assert github.checks_asked == []


def test_status_explains_why_nothing_is_deploying(tmp_path):
    """«Обновление есть, а сайт прежний» без этой строки выглядит поломкой."""
    github = FakeGitHub(checks=Checks(CHECKS_PENDING, "pytest"))
    state = make_updater(tmp_path, github=github).status()

    assert state["update_available"] is True
    assert state["checks"] == CHECKS_PENDING
    assert state["checks_detail"] == "pytest"


# --- GitHub ---


class _FakeResponse:
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status

    def read(self, _size=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_head_returns_the_sha_and_the_etag():
    github = GitHub(
        "owner/repo",
        opener=lambda request, timeout=None: _FakeResponse(NEW.encode(), {"ETag": 'W/"1"'}),
    )
    assert github.head("main") == Head(sha=NEW, etag='W/"1"', changed=True)


def test_not_modified_means_nothing_to_do():
    import urllib.error

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

    head = GitHub("owner/repo", opener=opener).head("main", etag='W/"1"')
    assert head.changed is False
    assert head.etag == 'W/"1"'


def test_a_reply_that_is_not_a_sha_is_an_error():
    github = GitHub(
        "owner/repo",
        opener=lambda request, timeout=None: _FakeResponse(b"<html>rate limited</html>"),
    )
    with pytest.raises(GitHubError):
        github.head("main")


def test_a_corrupted_etag_is_ignored_rather_than_fatal():
    """Заголовки HTTP — latin-1; иначе порченое состояние навсегда глушило бы опрос."""
    sent = {}

    def opener(request, timeout=None):
        sent["if_none_match"] = request.get_header("If-none-match")
        return _FakeResponse(NEW.encode(), {"ETag": 'W/"1"'})

    head = GitHub("owner/repo", opener=opener).head("main", etag="испорченный")

    assert head.sha == NEW
    assert sent["if_none_match"] is None


def test_a_missing_summary_never_blocks_a_deploy():
    def opener(request, timeout=None):
        raise OSError("сеть отвалилась")

    assert GitHub("owner/repo", opener=opener).summary(NEW) == ""


def checking(runs=(), statuses=None):
    """GitHub, отвечающий заданными check-run'ами и commit statuses."""
    payloads = {
        "check-runs": {"total_count": len(runs), "check_runs": list(runs)},
        "/status": statuses if statuses is not None else {"state": "", "total_count": 0},
    }

    def opener(request, timeout=None):
        for needle, payload in payloads.items():
            if needle in request.full_url:
                return _FakeResponse(json.dumps(payload).encode())
        raise AssertionError(request.full_url)

    return GitHub("owner/repo", opener=opener)


def test_all_checks_completed_and_good_is_green():
    checks = checking(
        runs=[
            {"name": "pytest", "status": "completed", "conclusion": "success"},
            # neutral и skipped — сознательно не выполнявшиеся шаги (условный job,
            # пропущенная матрица). Требовать от них зелёного значило бы требовать
            # результата там, где проверки и не было.
            {"name": "lint", "status": "completed", "conclusion": "skipped"},
        ]
    ).checks(NEW)
    assert checks.state == CHECKS_SUCCESS and checks.green


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled", "action_required"])
def test_a_bad_conclusion_is_red(conclusion):
    checks = checking(runs=[{"name": "pytest", "status": "completed", "conclusion": conclusion}])
    assert checks.checks(NEW).state == CHECKS_FAILURE
    assert conclusion in checks.checks(NEW).detail


def test_a_running_check_outweighs_the_ones_already_green():
    """Половина зелёного — это не зелёное: ждём, пока досчитает вторая половина."""
    checks = checking(
        runs=[
            {"name": "pytest", "status": "completed", "conclusion": "success"},
            {"name": "build", "status": "in_progress", "conclusion": None},
        ]
    ).checks(NEW)
    assert checks.state == CHECKS_PENDING and "build" in checks.detail


def test_a_red_check_outweighs_a_running_one():
    """Ждать нечего: результат уже известен и он плохой."""
    checks = checking(
        runs=[
            {"name": "build", "status": "in_progress", "conclusion": None},
            {"name": "pytest", "status": "completed", "conclusion": "failure"},
        ]
    ).checks(NEW)
    assert checks.state == CHECKS_FAILURE


def test_external_commit_statuses_count_too():
    """Actions отчитывается check-run'ами, внешние сервисы — commit statuses."""
    checks = checking(statuses={"state": "failure", "total_count": 1}).checks(NEW)
    assert checks.state == CHECKS_FAILURE and "commit status" in checks.detail


def test_no_checks_at_all_means_wait_not_go():
    """Workflow заводится не мгновенно.

    Приняв пустоту за зелёный свет, гейт пропускал бы ровно тот случай, ради
    которого поставлен: коммит, для которого проверки ещё не начинались.
    """
    assert checking().checks(NEW).state == CHECKS_PENDING


def test_checks_on_a_broken_reply_are_an_error_not_a_verdict():
    github = GitHub(
        "owner/repo", opener=lambda request, timeout=None: _FakeResponse(b"<html>rate limited</html>")
    )
    with pytest.raises(GitHubError):
        github.checks(NEW)


# --- уведомления ---


def test_telegram_is_silent_until_configured(tmp_path):
    assert isinstance(notify.from_config(make_config(tmp_path)), notify.Silent)

    configured = make_config(
        tmp_path,
        OPENCRM_UPDATE_TELEGRAM_TOKEN="123:abc",
        OPENCRM_UPDATE_TELEGRAM_CHAT="42",
    )
    assert isinstance(notify.from_config(configured), notify.Telegram)


def test_telegram_posts_the_text_to_the_chat():
    sent = {}

    def opener(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = request.data.decode()
        return _FakeResponse(status=200)

    assert notify.Telegram("123:abc", "42", opener=opener).send("привет") is True
    assert "/bot123:abc/sendMessage" in sent["url"]
    assert "chat_id=42" in sent["body"]


def test_an_unreachable_telegram_does_not_raise():
    def opener(request, timeout=None):
        raise OSError("нет сети")

    assert notify.Telegram("1:a", "2", opener=opener).send("текст") is False


# --- обновление: когда ничего делать не надо ---


def test_a_disabled_switch_stops_everything(tmp_path):
    updater = make_updater(tmp_path)
    updater.journal.set_autoupdate(False)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DISABLED
    assert not updater.shell.ran("docker")


def test_force_update_ignores_the_switch(tmp_path):
    updater = make_updater(tmp_path)
    updater.journal.set_autoupdate(False)

    assert updater.run_once(force=True).status == STATUS_DEPLOYED


def test_a_304_costs_nothing(tmp_path):
    updater = make_updater(tmp_path, github=FakeGitHub(changed=False))

    outcome = updater.run_once()

    assert outcome.status == STATUS_UP_TO_DATE
    assert not updater.shell.ran("docker")


def test_the_same_commit_is_not_redeployed(tmp_path):
    updater = make_updater(tmp_path, github=FakeGitHub(sha=OLD))

    outcome = updater.run_once()

    assert outcome.status == STATUS_UP_TO_DATE
    assert not updater.shell.ran("docker")
    assert updater.journal.deployed_sha == OLD


def test_force_update_redeploys_even_the_current_commit(tmp_path):
    """`force-update` — «передеплой сейчас», а не «проверь, нет ли нового»."""
    updater = make_updater(tmp_path, github=FakeGitHub(sha=OLD))

    outcome = updater.run_once(force=True)

    assert outcome.status == STATUS_DEPLOYED
    assert updater.shell.ran("up -d --build")


def test_a_github_outage_is_not_written_into_the_history(tmp_path):
    """Опрос раз в пять минут — сетевые сбои затопили бы журнал обновлений."""

    class Broken:
        def head(self, branch, etag=""):
            raise GitHubError("GitHub недоступен")

        def summary(self, sha):
            return ""

    updater = make_updater(tmp_path, github=Broken())

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert updater.journal.history() == []
    assert updater.notifier.messages == []


# --- обновление: удачный путь ---


def test_a_deploy_walks_the_steps_in_order(tmp_path):
    updater = make_updater(tmp_path)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert step_names(outcome) == [
        "preflight", "fetch", "checkout", "tests", "config", "backup", "deploy",
        "nginx-reload", "health", "prune",
    ]
    assert all(step.ok for step in outcome.steps)


def test_old_images_are_swept_after_a_successful_deploy(tmp_path):
    """Демон работает месяцами; забитый диск сломал бы и загрузку файлов."""
    updater = make_updater(tmp_path)

    updater.run_once()

    assert updater.shell.ran("image prune -f")


def test_a_failed_sweep_does_not_spoil_a_good_deploy(tmp_path):
    shell = FakeShell()
    shell.fail("image prune", err="daemon busy")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert updater.journal.deployed_sha == NEW


def test_the_tests_run_before_the_live_site_is_touched(tmp_path):
    updater = make_updater(tmp_path)

    updater.run_once()

    checks = next(i for i, call in enumerate(updater.shell.calls) if "docker-compose.tests.yml" in call)
    deploy = next(i for i, call in enumerate(updater.shell.calls) if "up -d --build" in call)
    assert checks < deploy


def test_success_is_recorded_and_announced(tmp_path):
    updater = make_updater(tmp_path)

    updater.run_once()

    assert updater.journal.deployed_sha == NEW
    record = updater.journal.last()
    assert record["status"] == STATUS_DEPLOYED
    assert record["to_sha"] == NEW
    assert "feat: новая витрина" in updater.notifier.messages[0]


def test_health_check_asks_the_real_pages_too(tmp_path):
    probe = FakeProbe()
    updater = make_updater(tmp_path, probe=probe)

    updater.run_once()

    assert "http://127.0.0.1/healthz" in probe.calls
    assert "http://127.0.0.1/" in probe.calls


def test_a_smoke_failure_counts_as_a_broken_deploy(tmp_path):
    """`/healthz` жив, а страница отдаёт 500 — «контейнер поднялся» этого не ловит."""
    updater = make_updater(tmp_path, probe=FakeProbe(smoke=(False, True)))

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    assert "smoke" in outcome.reason


# --- обновление: остановка до подмены ---


def test_local_edits_stop_the_update(tmp_path):
    shell = FakeShell()
    shell.dirty = " M web/public/layout.py\n"
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "затёрло" in outcome.reason
    assert not shell.ran("docker")


def test_local_edits_can_be_overridden_on_purpose(tmp_path):
    shell = FakeShell()
    shell.dirty = " M web/public/layout.py\n"
    updater = make_updater(tmp_path, shell=shell, OPENCRM_UPDATE_ALLOW_DIRTY="1")

    assert updater.run_once().status == STATUS_DEPLOYED


def test_red_tests_never_reach_the_live_site(tmp_path):
    shell = FakeShell()
    shell.fail("docker-compose.tests.yml up", err="2 failed")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert not shell.ran("up -d --build")
    assert shell.ran(f"checkout --detach --quiet {OLD}")  # чекаут вернули на место
    assert "2 failed" in outcome.reason


def test_nastroyki_ne_soshlis_zhivoy_sayt_ne_tronut(tmp_path):
    """Новый код требует настройку, которой на машине нет.

    Так уже случилось: Redis стал обязательным, а обновление никогда не
    дописывает `OPENCRM_REDIS_URL` — пароль ему взять неоткуда. Отказ вылезал
    после подмены контейнера: сайт лёг, `/healthz` вернул 502, откатились и код,
    и база. И так кругом, каждые полчаса, пока человек не зашёл руками.

    Отказ по конфигу заранее известен и ничего не стоит — значит он обязан
    случиться до того, как живой сайт тронут.
    """
    shell = FakeShell()
    shell.fail("config.selfcheck", err="OPENCRM_REDIS_URL пуст: общего счётчика нет")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert not shell.ran("up -d --build")
    assert not shell.ran("snapshot_db dump")  # до копии базы даже не дошли
    assert shell.ran(f"checkout --detach --quiet {OLD}")
    assert "OPENCRM_REDIS_URL" in outcome.reason


def test_proverka_nastroek_ne_zapuskaet_migratsii(tmp_path):
    """Проверка обязана быть безобидной — иначе она хуже отсутствующей.

    У образа `ENTRYPOINT ["/entrypoint.sh"]`, а `compose run` подменяет команду,
    но НЕ точку входа. Забыть `--entrypoint` — значит на «безобидной проверке
    настроек» дождаться базы и накатить миграции: до снятия копии и раньше своей
    очереди. Копии нет, схема уже новая, откатываться нечем.
    """
    updater = make_updater(tmp_path)

    updater.run_once()

    zapusk = next(cmd for cmd in updater.shell.calls if "config.selfcheck" in cmd)
    assert "--entrypoint python" in zapusk, zapusk
    # и без чужих служб: базу с redis проверка настроек не поднимает
    assert "--no-deps" in zapusk, zapusk


def test_proverka_nastroek_smotrit_na_novyy_kod(tmp_path):
    """Проверять надо новый код, а не образ с прошлого раза.

    `compose run` без сборки берёт лежащий образ — то есть требования СТАРОГО
    кода. Проверка при этом зелёная и совершенно бесполезная: обновление ровно
    так же ляжет после подмены. Поэтому образ пересобирается перед запуском.

    Отдельной командой, а не флагом `run --build`: флаг появился только в
    Compose v2.13, а от установки проект требует просто «compose v2».
    """
    updater = make_updater(tmp_path)

    updater.run_once()

    poryadok = updater.shell.calls
    sborka = next(i for i, cmd in enumerate(poryadok) if "compose" in cmd and "build app" in cmd)
    proverka = next(i for i, cmd in enumerate(poryadok) if "config.selfcheck" in cmd)
    assert sborka < proverka, poryadok[sborka:proverka + 1]
    assert "--build" not in poryadok[proverka]  # флага из v2.13 быть не должно


def test_a_directory_that_is_not_a_repository_stops_cleanly(tmp_path):
    """Разворачивание из архива вместо clone: обновляться неоткуда, но и ломать нечего."""
    config = make_config(tmp_path)
    (config.project_dir / ".git").rmdir()
    shell = FakeShell()
    shell.head = ""  # `git rev-parse HEAD` в не-репозитории молчит
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "не git-репозиторий" in outcome.reason
    # возвращаться некуда — пустой sha в git не уезжает
    assert not shell.ran("checkout")
    assert not shell.ran("docker")


def test_checks_can_be_switched_off(tmp_path):
    updater = make_updater(tmp_path, OPENCRM_UPDATE_RUN_CHECKS="0")

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert not updater.shell.ran("docker-compose.tests.yml")
    tests_step = next(step for step in outcome.steps if step.name == "tests")
    assert "пропущены" in tests_step.detail


# --- обновление: откат ---


def test_the_daemon_restarts_itself_when_its_own_code_changes(tmp_path):
    """Python читает исходники один раз, при импорте.

    Обновление правит файлы на диске, но уже запущенный демон продолжает
    работать тем, что загрузил при старте. На боевом сервере это стоило
    отдельного разбора: smoke-тест чинили дважды, а деплой оба раза падал
    старым кодом, потому что служба крутилась с прошлой недели.

    Выход из watch — это и есть перезапуск: systemd поднимет службу заново
    (Restart=always), и следующий круг пойдёт новым кодом.
    """
    shell = FakeShell()
    shell.rules.append(("diff --name-only", 0, "deploy/updater.py\nweb/main.py\n", ""))
    # после деплоя HEAD уезжает на новый коммит
    shell.effect("checkout --detach", lambda: setattr(shell, "head", NEW))
    lines: list[str] = []
    updater = make_updater(tmp_path, shell=shell)
    # make_updater отдаёт лишние аргументы в конфиг, а не в обновлятор,
    # поэтому лог подменяем напрямую.
    updater.log = lines.append

    updater.watch(rounds=5)

    assert any("выхожу" in line for line in lines), (
        "демон остался в работе со старым кодом в памяти"
    )


def test_a_foreign_change_does_not_restart_the_daemon(tmp_path):
    """Перезапуск ради чужой правки — лишний простой в опросе."""
    shell = FakeShell()
    shell.rules.append(("diff --name-only", 0, "web/main.py\ncore/services/deal_service.py\n", ""))
    shell.effect("checkout --detach", lambda: setattr(shell, "head", NEW))
    lines: list[str] = []
    updater = make_updater(tmp_path, shell=shell)
    # make_updater отдаёт лишние аргументы в конфиг, а не в обновлятор,
    # поэтому лог подменяем напрямую.
    updater.log = lines.append

    updater.watch(rounds=2)

    assert not any("выхожу" in line for line in lines), (
        "демон вышел из-за правки, которая его не касается"
    )


def test_https_redirect_is_a_live_site_not_a_failure(tmp_path):
    """Сайт на HTTPS отвечает на http://127.0.0.1/ перенаправлением.

    Идти по нему нельзя: адрес ведёт на https://127.0.0.1/, а сертификат
    выписан на домен и к IP-адресу не подходит — проверка сертификата
    провалится всегда. Так и было на боевом сервере: деплой падал и
    откатывался, а сайт при этом полностью работал.

    Само перенаправление выдаёт настроенный и живой nginx, а живость
    приложения уже подтвердил /healthz. Значит 3xx — успех.
    """
    probe = FakeProbe(smoke_status=301)
    updater = make_updater(tmp_path, probe=probe)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED, outcome.detail
    # и по редиректу мы не пошли
    smoke_calls = [f for url, f in zip(probe.calls, probe.followed) if "healthz" not in url]
    assert smoke_calls, "smoke-тест не выполнялся вовсе"
    assert not any(smoke_calls), "smoke пошёл по редиректу — упрётся в чужой сертификат"


def test_a_dead_site_is_rolled_back(tmp_path):
    shell = FakeShell()
    updater = make_updater(tmp_path, shell=shell, probe=FakeProbe(health=(False, True)))

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    assert shell.ran(f"checkout --detach --quiet {OLD}")
    assert shell.ran("stop app")
    assert step_names(outcome)[-5:] == [
        "rollback-checkout", "rollback-stop", "rollback-db",
        "rollback-deploy", "rollback-health",
    ]


def test_the_rollback_puts_the_database_back(tmp_path):
    """Откат кода без отката базы оставил бы прежнее приложение на новой схеме.

    Миграции вперёд необратимы: вернуть контейнер и не вернуть базу — значит
    получить старый код поверх схемы, которой он не знает.
    """
    shell = FakeShell()
    updater = make_updater(
        tmp_path, shell=shell, probe=FakeProbe(health=(False, True))
    )

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    vozvrat = next(shag for shag in outcome.steps if shag.name == "rollback-db")
    assert vozvrat.ok, vozvrat.detail
    zalivki = [(line, put) for line, put in shell.stdins if "mysql -uroot" in line]
    assert zalivki, "база не возвращена — старый код остался на новой схеме"
    assert zalivki[0][1].endswith(".sql"), "залит не дамп"

def test_kopiya_na_mysql_snimaetsya_dampom_a_ne_broshennym_faylom(tmp_path):
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_snimaetsya(config, shell))
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert shell.ran("scripts.snapshot_db dump"), "копия MySQL не снималась вовсе"
    assert not shell.ran("sqlite3"), "снималась копия базы, с которой не работают"
    kopiya = config.state_dir / f"pre-update-{NEW[:12]}.sql"
    assert kopiya.is_file(), "дампа нет в каталоге состояния — откатывать нечем"
    assert METKA_DAMPA in kopiya.read_text(encoding="utf-8")


def test_bez_kopii_mysql_deploy_ne_nachinaetsya(tmp_path):
    """Миграции вперёд необратимы: нет копии — нет и подмены контейнера."""
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.fail("scripts.snapshot_db dump", "Errno 28 No space left on device")
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert not shell.ran("up -d --build"), "живой сайт тронули без копии базы"
    backup = next(step for step in outcome.steps if step.name == "backup")
    assert not backup.ok


def test_oborvannyy_damp_ne_schitaetsya_snyatoy_kopiey(tmp_path):
    """Дамп без метки конца — обычный текстовый файл, годным он не бывает."""
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_snimaetsya(config, shell, celyy=False))
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "метки конца" in outcome.reason
    assert not shell.ran("up -d --build")


def test_otkat_na_mysql_zalivaet_damp_v_bazu(tmp_path):
    """Возврат — это заливка дампа клиенту `mysql`, а не запись в чужой файл."""
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_snimaetsya(config, shell))
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    kopiya = config.state_dir / f"pre-update-{NEW[:12]}.sql"
    zalivki = [(line, put) for line, put in shell.stdins if "mysql -uroot opencrm" in line]
    assert zalivki, "дамп в MySQL не заливался — база осталась после миграций нового кода"
    assert zalivki[0][1] == str(kopiya), "залили не ту копию"
    assert "exec -T db" in zalivki[0][0], "клиент mysql живёт в контейнере базы, не приложения"
    vozvrat = next(step for step in outcome.steps if step.name == "rollback-db")
    assert vozvrat.ok


def test_otkat_na_mysql_ne_zalivaet_oborvannuyu_kopiyu(tmp_path):
    """Дамп, залитый наполовину, оставит базу хуже, чем она есть сейчас."""
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_snimaetsya(config, shell))
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )
    # Копия испортилась между снятием и откатом — место на диске кончилось уже
    # после неё, а обрезанный хвост виден только чтением.
    shell.effect(
        "up -d --build",
        once(
            lambda: (config.state_dir / f"pre-update-{NEW[:12]}.sql").write_text(
                "INSERT INTO clients VALUES (1);\n", encoding="utf-8"
            )
        ),
    )

    outcome = updater.run_once()

    assert not any(line for line, _ in shell.stdins if "mysql -uroot" in line)
    vozvrat = next(step for step in outcome.steps if step.name == "rollback-db")
    assert not vozvrat.ok and "оборвана" in vozvrat.detail


def test_neudachnaya_baza_mysql_otkladyvaetsya_v_storonu(tmp_path):
    """Неудачная база не стирается, а откладывается в сторону — тем же дампом.

    В ней данные за время неудавшегося обновления, и разбираться с ними будет
    человек.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_snimaetsya(config, shell))
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    updater.run_once()

    otlozheno = list(config.state_dir.glob("failed-update-*.sql"))
    assert len(otlozheno) == 1, "состояние базы на момент отката потеряно молча"


def test_a_build_failure_leaves_the_database_alone(tmp_path):
    """Сборка упала — старое приложение всё это время работало и принимало записи.

    Возврат базы из копии стёр бы их. Копия к этому моменту уже снята, и
    соблазн «вернуть на всякий случай» тут самый сильный — поэтому проверка.
    """
    shell = FakeShell()
    shell.fail("up -d --build", err="build failed")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    zalivki = [line for line, _ in shell.stdins if "mysql -uroot" in line]
    assert not zalivki, (
        "база возвращена из копии, хотя контейнер не подменяли: записи, "
        "принятые старым приложением за время сборки, стёрты"
    )

def test_a_rollback_that_does_not_come_up_is_called_broken(tmp_path):
    updater = make_updater(tmp_path, probe=FakeProbe(health=(False, False)))

    outcome = updater.run_once()

    assert outcome.status == STATUS_BROKEN
    assert "нужен человек" in updater.notifier.messages[0]


def test_a_rolled_back_commit_is_not_tried_again(tmp_path):
    """Иначе демон каждые пять минут пересобирал бы заведомо сломанную версию."""
    config = make_config(tmp_path)
    first = make_updater(tmp_path, config=config, probe=FakeProbe(health=(False, True)))
    assert first.run_once().status == STATUS_ROLLED_BACK
    assert first.journal.read()["failed_sha"] == NEW

    second = make_updater(tmp_path, config=config)
    outcome = second.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "force-update" in outcome.reason
    assert not second.shell.ran("up -d --build")


def test_force_update_retries_a_commit_that_failed(tmp_path):
    config = make_config(tmp_path)
    make_updater(tmp_path, config=config, probe=FakeProbe(health=(False, True))).run_once()

    retry = make_updater(tmp_path, config=config)
    outcome = retry.run_once(force=True)

    assert outcome.status == STATUS_DEPLOYED
    assert retry.journal.read()["failed_sha"] == ""


def test_a_new_commit_is_tried_even_after_a_failure(tmp_path):
    config = make_config(tmp_path)
    make_updater(tmp_path, config=config, probe=FakeProbe(health=(False, True))).run_once()

    later = make_updater(tmp_path, config=config, github=FakeGitHub(sha="c" * 40))
    assert later.run_once().status == STATUS_DEPLOYED


# --- отчёт для человека ---


def test_status_shows_what_is_running_and_what_is_available(tmp_path):
    state = make_updater(tmp_path).status()

    assert state["deployed"] == OLD
    assert state["available"] == NEW
    assert state["update_available"] is True
    assert state["autoupdate"] is True


def test_status_survives_github_being_down(tmp_path):
    class Broken:
        def head(self, branch, etag=""):
            raise GitHubError("таймаут")

        def summary(self, sha):
            return ""

    state = make_updater(tmp_path, github=Broken()).status()

    assert state["deployed"] == OLD
    assert state["available"] == ""
    assert "таймаут" in state["github_error"]


def test_the_notification_names_the_step_that_failed(tmp_path):
    updater = make_updater(tmp_path, probe=FakeProbe(health=(False, True)))

    updater.run_once()

    message = updater.notifier.messages[0]
    assert "откачено" in message
    assert "health" in message
    assert NEW[:12] in message


def test_a_quiet_round_says_nothing(tmp_path):
    updater = make_updater(tmp_path, github=FakeGitHub(changed=False))

    updater.run_once()

    assert updater.notifier.messages == []


def test_the_daemon_polls_on_a_schedule(tmp_path):
    slept: list[float] = []
    config = make_config(tmp_path)
    updater = Updater(
        config,
        journal=Journal(config.state_file, config.history_file),
        github=FakeGitHub(changed=False),
        shell=FakeShell(),
        probe=FakeProbe(),
        notifier=FakeNotifier(),
        sleep=slept.append,
        clock=lambda: 0.0,
    )

    updater.watch(rounds=3)

    assert slept == [config.poll_seconds, config.poll_seconds]


def test_the_daemon_outlives_an_unexpected_error(tmp_path):
    class Exploding:
        def __init__(self):
            self.calls = 0

        def head(self, branch, etag=""):
            self.calls += 1
            raise RuntimeError("что-то совсем неожиданное")

        def summary(self, sha):
            return ""

    github = Exploding()
    updater = make_updater(tmp_path, github=github)

    updater.watch(rounds=2)

    assert github.calls == 2


def test_nginx_is_asked_to_reread_its_config_after_a_deploy(tmp_path):
    """Без этого правки конфига nginx не применяются вовсе.

    Файлы nginx примонтированы из чекаута, а не лежат в его образе: `git
    checkout` меняет их на диске мгновенно. Но `docker compose up -d --build`
    пересоздаёт только те службы, у которых изменилось описание или образ, — у
    nginx не меняется ни то, ни другое. Он остаётся работать с конфигом,
    прочитанным при своём запуске, и сам за файлами не следит.

    Поймано репетицией обновления на живом стенде: compose тронул только `app`,
    и nginx продолжал раздавать `/media/` прямо с диска, хотя новый конфиг на
    диске уже проксировал этот путь в приложение. Починка, закрывшая файлы
    витрины после отзыва ссылки, молча не действовала.

    Зовётся ИМЕННО скрипт: голый `nginx -s reload` не рендерит шаблон заново и,
    что хуже, завершается нулём, не дождавшись разбора конфига. Разбор — в шапке
    `docker/nginx/reload.sh`.
    """
    updater = make_updater(tmp_path)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert updater.shell.ran("exec -T nginx sh /opencrm/reload.sh")
    assert not updater.shell.ran("nginx -s reload"), (
        "обновление снова шлёт голый сигнал: правки шаблонов не применятся, "
        "а отвергнутый конфиг вернёт код 0 и сойдёт за удачу"
    )
    assert step_names(outcome).index("deploy") < step_names(outcome).index("nginx-reload")
    assert step_names(outcome).index("nginx-reload") < step_names(outcome).index("health")


def test_a_missing_nginx_does_not_fail_the_update(tmp_path):
    """У кого-то свой nginx снаружи, и внутреннего нет вовсе.

    Перезагрузка тогда не удастся, и валить из-за этого удавшееся обновление
    незачем: приложение обновилось, сайт живой.
    """
    shell = FakeShell()
    shell.fail("reload.sh")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    reload_step = next(s for s in outcome.steps if s.name == "nginx-reload")
    assert reload_step.ok is False


def test_a_failed_reload_is_named_in_the_message_even_when_the_update_worked(tmp_path):
    """Не смертельный шаг — не значит незаметный.

    Пять суток боевой сервер работал со старым конфигом nginx: метрики были
    открыты наружу, в журнал попадали адреса клиентов и ссылки `/b/<токен>`.
    Никто не знал потому, что перечитывание падало молча — шаг не смертельный,
    и о его провале не говорил ни один канал. Уведомление об удавшемся
    обновлении обязано называть провалившийся шаг И его причину: заголовок
    «OpenCRM обновлён» читают как «всё хорошо».
    """
    shell = FakeShell()
    shell.fail("reload.sh", err="nginx: [emerg] unknown log format \"opencrm_json\"")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED, "провал перечитывания не валит обновление"
    reload_step = next(s for s in outcome.steps if s.name == "nginx-reload")
    assert "unknown log format" in reload_step.detail, (
        "причина провала не попала в detail шага — в истории её тоже не будет"
    )

    message = updater.notifier.messages[0]
    assert "nginx-reload" in message, "об упавшем перечитывании сообщение молчит"
    assert "unknown log format" in message, (
        "шаг назван, а причина нет — идти смотреть придётся вслепую"
    )


def test_imya_bazy_beryotsya_iz_config_env(tmp_path):
    """Адрес базы обновлятор читает там же, где его читает контейнер.

    Пакет `deploy` работает на хосте и настроек приложения не тянет (см. шапку
    пакета), поэтому `config/.env` разбирается своими силами — тем же файлом,
    который compose отдаёт контейнеру через `env_file`.
    """
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / ".env").write_text(
        "# комментарий\nOPENCRM_SECRET_KEY=x\n"
        f"OPENCRM_DB_URL={MYSQL_URL}\n",
        encoding="utf-8",
    )
    config = UpdateConfig.from_env(
        {"OPENCRM_HOME": str(tmp_path / "home"), "OPENCRM_UPDATE_PROJECT_DIR": str(repo)}
    )

    assert config.mysql_db == "opencrm"


def test_stranno_nazvannaya_baza_ne_edet_v_komandnuyu_stroku(tmp_path):
    """Имя базы уезжает в командную строку клиента `mysql`, и оно проверяется.

    Пустое имя означает внятный отказ отката, а не подстановку чего попало в
    чужую команду.
    """
    config = UpdateConfig.from_env(
        {
            "OPENCRM_HOME": str(tmp_path),
            "OPENCRM_UPDATE_DB_URL": "mysql+pymysql://u:p@db:3306/opencrm;rm -rf /",
        }
    )
    assert config.mysql_db == ""


# --- оформление сообщений в Telegram ------------------------------------------
#
# Разметка тут когда-то была выключена целиком, и повод был настоящий: в текст
# попадает заголовок коммита, а в MarkdownV2 экранировать надо восемнадцать
# символов. Любой пропущенный — и Telegram отбивает сообщение целиком, то есть
# об упавшем деплое не узнаёт никто именно потому, что сообщение было подробным.
#
# HTML требует трёх символов вместо восемнадцати, но одного этого мало: свойство,
# которое обязано держаться, — «ошибка в оформлении не заглушает аварию».


def test_zagolovok_kommita_s_razmetkoy_ne_lomaet_soobshchenie(tmp_path):
    """`<`, `&` и `_` в заголовке коммита — обычное дело, а не редкость."""
    github = FakeGitHub(summary="fix(a_b): <script> & «кавычки» *звёздочки* `код`")
    updater = make_updater(tmp_path, github=github)

    updater.run_once()

    soobshchenie = updater.notifier.messages[0]
    assert "&lt;script&gt;" in soobshchenie, "угловые скобки не экранированы"
    assert "&amp;" in soobshchenie, "амперсанд не экранирован"
    # А смысл при этом на месте: подчёркивания и звёздочки в HTML не разметка.
    assert "fix(a_b)" in soobshchenie
    assert "*звёздочки*" in soobshchenie


def test_otbituyu_razmetku_dosylaem_ploskim_tekstom(tmp_path):
    """Если Telegram не разобрал разметку — сообщение обязано дойти без неё.

    Это и есть то свойство, ради которого разметку когда-то сняли совсем.
    Теперь она есть, а гарантия осталась.
    """
    import urllib.error

    from deploy import notify

    poshlo = []

    def otkryvatel(request, timeout=None):  # noqa: ARG001
        telo = request.data.decode("utf-8")
        poshlo.append(telo)
        if "parse_mode" in telo:
            raise urllib.error.HTTPError(request.full_url, 400, "can't parse entities", {}, None)

        class Otvet:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Otvet()

    kanal = notify.Telegram("token", "chat", opener=otkryvatel)
    assert kanal.send("<b>Обновлено</b>\n<blockquote>шаг</blockquote>") is True
    assert len(poshlo) == 2, "запасной отправки не было"
    assert "parse_mode" not in poshlo[1], "во второй раз снова послали разметку"
    # И теги в плоском тексте не остались мусором.
    assert "%3Cb%3E" not in poshlo[1] and "b%3E" not in poshlo[1]


def test_udachnoe_obnovlenie_ne_budit_a_upavshee_budit(tmp_path):
    """Ночью удачный деплой — не повод звенеть. Откат — повод."""
    updater = make_updater(tmp_path)
    updater.run_once()
    assert updater.notifier.tihie == [True], "удачное обновление звенит на телефоне"

    shell = FakeShell()
    shell.fail("up -d --build", err="build failed")
    upavshiy = make_updater(tmp_path, shell=shell)
    upavshiy.run_once()
    assert upavshiy.notifier.tihie == [False], "откат ушёл беззвучно — его не заметят"


def test_spisok_shagov_svorachivaetsya(tmp_path):
    """Четырнадцать ходов не должны занимать пол-экрана, но и теряться не должны."""
    updater = make_updater(tmp_path)
    updater.run_once()

    soobshchenie = updater.notifier.messages[0]
    assert "<blockquote expandable>" in soobshchenie, "список шагов не сворачивается"
    assert "Ход обновления:" in soobshchenie, "в свёрнутом виде не видно счёта"
    # Первая строка цитаты видна всегда — счёт обязан быть именно в ней.
    vnutri = soobshchenie.split("<blockquote expandable>")[1]
    assert vnutri.splitlines()[0].startswith("<b>Ход обновления:")


def test_dlitelnost_chitaetsya_glazami():
    """«1136 c» человек всё равно переводит в уме — сделаем это за него."""
    from deploy.updater import _dlitelnost

    assert _dlitelnost(42) == "42 с"
    assert _dlitelnost(1136) == "18 мин 56 с"
    assert _dlitelnost(3725) == "1 ч 02 мин"


def test_pervaya_stroka_nazyvaet_i_ishod_i_chto_priehalo(tmp_path):
    """В списке чатов Telegram показывает начало сообщения — и только его.

    «✅ Обновлено» отвечает на половину вопроса: обновлено-то чем? Вторую
    половину — заголовок коммита — приходилось искать, открыв чат, а открывают
    его не всегда и не сразу.
    """
    updater = make_updater(tmp_path)
    updater.run_once()

    pervaya = updater.notifier.plain[0].splitlines()[0]
    assert "Обновлено" in pervaya, "первая строка не называет исход"
    assert "feat: новая витрина" in pervaya, (
        "первая строка не называет, что именно приехало"
    )


def test_ssylka_vedyot_na_sravnenie_a_ne_na_odin_kommit(tmp_path):
    """Между двумя обновлениями в ветку попадает несколько коммитов.

    Страница последнего из них показывает не то, что приехало, — а приехали
    все. Сравнение показывает ровно разницу между тем, что было на сервере, и
    тем, что стало.
    """
    updater = make_updater(tmp_path)
    updater.run_once()

    soobshchenie = updater.notifier.messages[0]
    assert f"/compare/{OLD}...{NEW}" in soobshchenie, "ссылки на сравнение нет"
    assert f"{OLD[:12]} → {NEW[:12]}" in soobshchenie, "не видно, что с чем сравнивать"


def test_pervyy_deploy_ssylaetsya_na_kommit_a_ne_na_pustotu(tmp_path):
    """Сравнивать не с чем, когда предыдущего номера нет вовсе.

    `.../compare/...abc` — битый адрес, и жать по нему человек будет ровно в
    тот момент, когда что-то пошло не так.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.head = ""  # чекаут без истории: `rev-parse HEAD` молчит
    updater = make_updater(tmp_path, config=config, shell=shell)

    updater._deploy("", NEW)

    soobshchenie = updater.notifier.messages[0]
    assert "/compare/" not in soobshchenie, "сравнение с пустотой ведёт в никуда"
    assert f"/commit/{NEW}" in soobshchenie


def test_soobshchenie_priznayotsya_chto_napisano_starym_kodom(tmp_path):
    """Жалоба владельца: «обновление приехало, а оформление прежнее».

    Так и есть, и это не поломка. Python читает исходники один раз, при импорте;
    сообщение об исходе собирает код, загруженный при старте демона, а на диске
    к этому моменту лежит уже новый. Дальше `watch` увидит расхождение и выйдет,
    systemd поднимет службу заново — но сообщение уже ушло старым оформлением.

    Объяснить это нечем, кроме как сказав прямо. Молчание здесь стоит дороже
    строчки: владелец идёт искать поломку там, где её нет.
    """
    shell = FakeShell()
    shell.rules.append(("diff --name-only", 0, "deploy/updater.py\nweb/main.py\n", ""))
    shell.effect("checkout --detach", lambda: setattr(shell, "head", NEW))
    updater = make_updater(tmp_path, shell=shell)

    updater.run_once()

    ploskiy = updater.notifier.plain[0]
    assert "перезапустится" in ploskiy, (
        "сообщение молчит о том, что написано кодом, который уже заменён"
    )
    assert "следующего обновления" in ploskiy


def test_chuzhaya_pravka_ne_rozhdaet_opravdaniy(tmp_path):
    """Обратная сторона: обычное обновление не должно ничего объяснять.

    Оговорка, приходящая с каждым сообщением, перестаёт читаться в тот же день.
    """
    shell = FakeShell()
    shell.rules.append(("diff --name-only", 0, "web/main.py\ncore/services/deal_service.py\n", ""))
    shell.effect("checkout --detach", lambda: setattr(shell, "head", NEW))
    updater = make_updater(tmp_path, shell=shell)

    updater.run_once()

    assert "перезапустится" not in updater.notifier.plain[0]


def test_prichina_ne_povtoryaetsya_trizhdy(tmp_path):
    """Причина, шаг и список ходов не должны твердить одно и то же подряд.

    Три одинаковых строки читаются как три разные беды — ровно так выглядел
    откат в чате: заголовок, причина, и следом «✗ health: <та же причина>».
    """
    shell = FakeShell()
    shell.fail("up -d --build", err="build failed")
    updater = make_updater(tmp_path, shell=shell)

    updater.run_once()

    ploskiy = updater.notifier.plain[0]
    do_citaty = ploskiy.split("Ход обновления:")[0]
    assert do_citaty.count("build failed") == 1, (
        f"причина повторена {do_citaty.count('build failed')} раза до свёрнутого списка"
    )
    # Имя упавшего шага при этом названо — иначе непонятно, где встало.
    assert "✗ deploy" in do_citaty


# --- правка внутри примонтированного файла ---------------------------------
#
# Compose пересоздаёт контейнер по изменившемуся ОПИСАНИЮ службы и не читает
# того, что лежит внутри томов. А половина обвязки живёт именно там: точки входа
# Alertmanager и Prometheus, шаблоны nginx, конфиг promtail. Их правка доезжала
# на сервер и не применялась — контейнер здоров, лог чист, код новый, поведение
# прежнее.

OPISANIE_STEKA = json.dumps({
    "services": {
        "app": {"volumes": [{"type": "volume", "source": "storage"}]},
        "alertmanager": {"volumes": [
            {"type": "bind", "source": "PROEKT/docker/monitoring/alertmanager/entrypoint.sh"},
        ]},
        "nginx": {"volumes": [
            {"type": "bind", "source": "PROEKT/docker/nginx/templates"},
        ]},
        "grafana": {"volumes": [{"type": "bind", "source": "/var/lib/opencrm/grafana"}]},
    }
})


def _stek(shell, config, izmeneno: str) -> None:
    """Подсказать поддельной оболочке, что изменилось и как устроен стек."""
    shell.otvet("diff --name-only", izmeneno)
    shell.otvet("config --format json", OPISANIE_STEKA.replace(
        "PROEKT", config.project_dir.as_posix()
    ))


def test_pravka_v_primontirovannom_fayle_peresozdayot_sluzhbu(tmp_path):
    """Иначе Alertmanager неделю работает скриптом, который уже исправлен."""
    config = make_config(tmp_path)
    shell = FakeShell()
    _stek(shell, config, "docker/monitoring/alertmanager/entrypoint.sh\nweb/main.py\n")
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    peresozdanie = [c for c in shell.calls if "--force-recreate" in c]
    assert len(peresozdanie) == 1, shell.calls
    assert "alertmanager" in peresozdanie[0], peresozdanie[0]
    assert "nginx" not in peresozdanie[0], "пересоздано лишнее"


def test_pravka_vnutri_primontirovannogo_kataloga_tozhe_schitaetsya(tmp_path):
    """Монтируют и файл, и целый каталог — второе не должно проскакивать."""
    config = make_config(tmp_path)
    shell = FakeShell()
    _stek(shell, config, "docker/nginx/templates/https.conf.template\n")
    updater = make_updater(tmp_path, config=config, shell=shell)

    updater.run_once()

    peresozdanie = [c for c in shell.calls if "--force-recreate" in c]
    assert len(peresozdanie) == 1 and "nginx" in peresozdanie[0], shell.calls


def test_bez_pravki_v_tomakh_nichego_ne_peresozdayotsya(tmp_path):
    """Пересоздание не бесплатно: это простой службы. Зря его делать нельзя."""
    config = make_config(tmp_path)
    shell = FakeShell()
    _stek(shell, config, "web/main.py\ncore/services/deal_service.py\n")
    updater = make_updater(tmp_path, config=config, shell=shell)

    updater.run_once()

    assert not shell.ran("--force-recreate"), shell.calls


def test_neudachnoe_peresozdanie_ne_valit_podnyatyy_sayt(tmp_path):
    """Сайт уже поднят и здоров — откатывать базу из-за этого шага нельзя.

    Пересоздание службы обвязки — улучшение, а не условие работоспособности.
    Валить обновление здесь значило бы лечить насморк ампутацией.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    _stek(shell, config, "docker/monitoring/alertmanager/entrypoint.sh\n")
    shell.fail("--force-recreate", err="no space left on device")
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    recreate = next(step for step in outcome.steps if step.name == "recreate")
    assert recreate.ok is False
    assert "no space" in recreate.detail


def test_ischeznuvshaya_sluzhba_ne_ostayotsya_rabotat(tmp_path):
    """Служба, убранная из compose, иначе живёт на сервере вечно.

    `up -d` без `--remove-orphans` про неё не знает: в описании её больше нет, а
    контейнер продолжает занимать память и порт. Установщик это давно делает
    (`monitoring_apply`), обновление — не делало.
    """
    updater = make_updater(tmp_path)

    updater.run_once()

    podnyatie = next(c for c in updater.shell.calls if "up -d --build" in c)
    assert "--remove-orphans" in podnyatie, podnyatie


# --- место на диске ----------------------------------------------------------


def test_nekhvatka_mesta_ostanavlivaet_do_pervoy_zapisi(tmp_path, monkeypatch):
    """Отказ по месту дёшев ДО первой записи и очень дорог посреди дампа.

    Обновление пишет на диск трижды: копия базы (на боевом объёме гигабайт с
    лишним), слои нового образа, образ ворот. Старый образ лежит до `prune`,
    значит на пике их два. Это не гипотеза: образ панели мониторинга разом
    вырос с 744 МБ до 1.16 ГБ.

    А оборванный нехваткой места дамп — обычный текстовый файл, и негодность у
    него не видна ничем, кроме отсутствующего хвоста. Такую копию однажды уже
    заливали «успешно», не заметив пропажи половины таблиц.
    """
    import shutil as _shutil
    from collections import namedtuple

    config = make_config(tmp_path, OPENCRM_UPDATE_MIN_FREE_MB="2048")
    shell = FakeShell()
    Mesto = namedtuple("Mesto", "total used free")
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: Mesto(0, 0, 500 * 1024 * 1024))
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "500 МБ" in outcome.reason and "2048" in outcome.reason
    # Docker не звали вовсе: ни сборки, ни ворот, ни дампа.
    assert not shell.ran("docker"), shell.calls
    # И на новый коммит не переходили. Возврат чекаута на ПРЕЖНИЙ при этом
    # случается, и так и надо — первая редакция этой проверки запрещала любой
    # `checkout` и краснела на верном коде: отказ обязан оставить дерево там,
    # где оно было.
    assert not shell.ran(f"checkout --detach --quiet {NEW}"), shell.calls
    assert not shell.ran("fetch"), shell.calls


def test_mesta_khvataet_obnovlenie_idyot(tmp_path, monkeypatch):
    """Парная проверка: иначе «отказывать всегда» тоже прошло бы предыдущую."""
    import shutil as _shutil
    from collections import namedtuple

    config = make_config(tmp_path, OPENCRM_UPDATE_MIN_FREE_MB="2048")
    Mesto = namedtuple("Mesto", "total used free")
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: Mesto(0, 0, 9000 * 1024 * 1024))
    updater = make_updater(tmp_path, config=config)

    assert updater.run_once().status == STATUS_DEPLOYED


def test_nemoy_disk_ne_zaklinivaet_obnovleniya(tmp_path, monkeypatch):
    """Не сумели узнать свободное место — работаем как раньше.

    Это важнее самой проверки. Страховка, превращённая в новый способ
    остановить обновления, хуже той беды, от которой она заведена: место
    кончается раз в год, а неотвечающий `disk_usage` (сетевой том, права,
    чужая файловая система) заклинил бы деплой навсегда и без объяснений.
    """
    import shutil as _shutil

    def ne_otvechaet(_put):
        raise OSError("нет такого устройства")

    config = make_config(tmp_path, OPENCRM_UPDATE_MIN_FREE_MB="2048")
    monkeypatch.setattr(_shutil, "disk_usage", ne_otvechaet)
    updater = make_updater(tmp_path, config=config)

    assert updater.run_once().status == STATUS_DEPLOYED


# --- след упавшей миграции: таблицы, которых копия не знает --------------------
#
# НАЙДЕНО ЖИВЫМ ОТКАТОМ НА СТЕНДЕ, а не рассуждением. Копия снимается ДО
# миграций и открывает каждую свою таблицу строкой `DROP TABLE IF EXISTS` — то
# есть заливка полностью пересобирает всё, что копия знает. Таблицу, которую
# упавшая миграция успела создать уже ПОСЛЕ снимка, копия не знает, и заливка её
# не трогает, а `alembic_version` при этом откатывается.
#
# Беда от этого тихая: сайт поднимается (schema_check ругается только на
# нехватку), отчёт зелёный, а ПОВТОРНАЯ попытка того же коммита упирается в
# `(1050, "Table 'x' already exists")` — и будет упираться всегда, пока человек
# не удалит таблицу руками. Замерено на стенде: два прогона подряд, оба
# откатились, и в отчёте оба раза стояло одно и то же «health-check не прошёл».

#: Копия, которая ЗНАЕТ две таблицы. Формат строки — тот же, каким её пишет
#: `scripts/snapshot_db._odna_tablica`; совпадение стережёт отдельная проверка.
KOPIYA_S_TABLITSAMI = (
    "DROP TABLE IF EXISTS `clients`;\n"
    "CREATE TABLE `clients` (id int);\n"
    "INSERT INTO `clients` VALUES (1);\n"
    "DROP TABLE IF EXISTS `orders`;\n"
    "CREATE TABLE `orders` (id int);\n"
    + METKA_DAMPA
    + "\n"
)


def damp_s_tablitsami(config, shell):
    """Подставной дампер, кладущий копию С именами таблиц."""

    def sdelat():
        put = shell.calls[-1].rsplit("/app/data/", 1)[1].split()[0]
        (config.data_dir / put).write_text(KOPIYA_S_TABLITSAMI, encoding="utf-8")

    return sdelat


def _otkat_s_tablitsami(tmp_path, zhivye: str):
    """Прогон до отката, где база отвечает заданным списком таблиц."""
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_s_tablitsami(config, shell))
    shell.otvet("SHOW FULL TABLES", zhivye)
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )
    outcome = updater.run_once()
    return outcome, shell


def _snosy(shell) -> list[str]:
    return [call for call in shell.calls if "DROP TABLE IF EXISTS" in call]


def test_otkat_snimaet_tablitsu_ot_upavshey_migratsii(tmp_path):
    """Таблица, созданная миграцией после снимка, обязана исчезнуть при откате.

    Иначе тот же коммит не накатится больше никогда: `alembic_version` откачен,
    миграция пойдёт заново и упрётся в «таблица уже есть». Человек при этом
    видит «health-check не прошёл» — то же сообщение, что и у просто сломанного
    кода, и причину ему никто не назовёт.
    """
    outcome, shell = _otkat_s_tablitsami(
        tmp_path,
        "clients\tBASE TABLE\norders\tBASE TABLE\notkat_sled\tBASE TABLE\n",
    )

    assert outcome.status == STATUS_ROLLED_BACK
    snosy = _snosy(shell)
    assert snosy, (
        "таблица от упавшей миграции осталась в базе — повторная попытка того "
        "же коммита упрётся в «таблица уже есть», и так навсегда"
    )
    assert "otkat_sled" in snosy[0]
    assert "clients" not in snosy[0] and "orders" not in snosy[0], (
        "снесли таблицу, которая ЕСТЬ в копии: восстановленные данные стёрты"
    )
    vozvrat = next(step for step in outcome.steps if step.name == "rollback-db")
    assert vozvrat.ok


def test_imya_tablitsy_ekranirovano_dlya_sh(tmp_path):
    """Обратные кавычки экранированы: строка уезжает в `sh -c`.

    Голая обратная кавычка там открывает подстановку команды. Поймано живым
    откатом на стенде: `sh` ответил «otkat_sled: command not found», в mysql
    уехало пустое имя — и снос молча не состоялся при шаге, который по виду
    отработал.
    """
    _, shell = _otkat_s_tablitsami(
        tmp_path, "clients\tBASE TABLE\norders\tBASE TABLE\notkat_sled\tBASE TABLE\n"
    )

    snos = _snosy(shell)[0]
    obratnaya = chr(96)
    ekran = chr(92) + obratnaya
    assert ekran + "otkat_sled" + ekran in snos, (
        f"имя таблицы без экрана — `sh` съест его как подстановку команды: {snos}"
    )


def test_tablitsa_so_strannym_imenem_ne_edet_v_komandnuyu_stroku(tmp_path):
    """Имя уезжает в командную строку, поэтому пропускаем только простые.

    То же правило и по той же причине стоит на имени базы
    (`test_stranno_nazvannaya_baza_ne_edet_v_komandnuyu_stroku`).
    """
    _, shell = _otkat_s_tablitsami(
        tmp_path,
        "clients\tBASE TABLE\norders\tBASE TABLE\nzlo" + chr(96) + "e\tBASE TABLE\n",
    )

    assert not _snosy(shell), "таблица с непростым именем уехала в команду"


def test_bez_lishnih_tablits_nichego_ne_snositsya(tmp_path):
    """База сошлась с копией — сносить нечего, и лишней команды быть не должно."""
    _, shell = _otkat_s_tablitsami(tmp_path, "clients\tBASE TABLE\norders\tBASE TABLE\n")

    assert not _snosy(shell)


def test_kopiya_bez_imyon_tablits_nichego_ne_snosit(tmp_path):
    """Разбор не нашёл ни одной таблицы — значит сносить нельзя ВООБЩЕ.

    Такая копия бывает у пустой базы (первый деплой), а бывает при расхождении
    разбора с дампером. В обоих случаях разница «живое минус известное» — это
    вся база, и снести её было бы худшим из возможных исходов отката.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    # Дампер по умолчанию кладёт копию без строк `DROP TABLE`.
    shell.otvet("SHOW FULL TABLES", "clients\tBASE TABLE\norders\tBASE TABLE\n")
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    assert not _snosy(shell), "снесли таблицы по пустому списку известных"


def test_nemaya_baza_ne_valit_otkat(tmp_path):
    """Не перечислить таблицы — говорим и едем дальше.

    Вернуть рабочую базу важнее, чем прибрать за миграцией: заливка дампа к
    этому моменту уже прошла, и валить откат из-за уборки незачем.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    shell.effect("scripts.snapshot_db dump", damp_s_tablitsami(config, shell))
    shell.fail("SHOW FULL TABLES", "база молчит")
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    outcome = updater.run_once()

    vozvrat = next(step for step in outcome.steps if step.name == "rollback-db")
    assert vozvrat.ok, f"откат свалился на уборке: {vozvrat.detail}"
    assert not _snosy(shell)


def test_razbor_imyon_sovpadaet_s_damperom(tmp_path):
    """Строка `DROP TABLE` разобрана так же, как её пишет дампер.

    Удвоение между `deploy/` и `scripts/` неизбежно — пакет `deploy` работает
    на хосте и драйвера базы не имеет, — но молча разойтись эти два места не
    должны. Разойдись они, `_tablicy_kopii` вернёт пустоту, и разница «живое
    минус известное» станет всей базой; спасает от беды только защита «пустой
    список — не сносим ничего», а не сам разбор.

    **Строку спрашиваем У ДАМПЕРА.** Прежде она была переписана сюда руками —
    и сторож оставался зелёным при любой правке формата: подлог (лишний пробел
    перед `;` в `scripts/snapshot_db.py`) не покраснел ни одним из ста
    двадцати двух тестов файла. Тот же приём, что у `SNAPSHOT_MARK` в
    `tests/test_pre_migrate_snapshot.py`: сверяем два места, а не место с
    литералом.
    """
    from deploy.updater import _tablicy_kopii
    from scripts.snapshot_db import stroka_drop

    # Ровно та обёртка, в которой дампер печатает строку (`_odna_tablica`).
    kak_pishet_damper = f"\n--\n-- clients\n--\n{stroka_drop('clients')}\nCREATE TABLE x;\n"
    fayl = tmp_path / "damp.sql"
    fayl.write_text(kak_pishet_damper, encoding="utf-8")

    assert _tablicy_kopii(fayl) == {"clients"}, (
        "разбор имён разошёлся с дампером: откат перестанет убирать за упавшими "
        "миграциями, и никто об этом не узнает"
    )


def test_razbor_imyon_perezhivaet_gigabaytnyy_damp(tmp_path):
    """Копия читается ПОТОКОМ: на боевой базе дамп — гигабайт с лишним.

    `read_text` развернул бы его в строку Python по два байта на знак (в дампе
    кириллица), то есть под два с половиной гигабайта. На VPS это `MemoryError`
    — и прилетел бы он посреди отката, сразу после заливки базы и ДО подъёма
    контейнера: сайт лежит, страница обслуживания навсегда показывает «идёт», а
    демон через пять минут берётся за тот же коммит снова.

    Гигабайт здесь не пишем — проверка не должна занимать минуту. Проверяем то,
    из-за чего беда и была возможна: что файл не читается целиком.
    """
    import ast
    import inspect

    from deploy import updater

    # Разбираем в дерево, а не ищем подстроку: слово `read_text` стоит в самой
    # докстроке (там объяснено, почему его нет в коде), и поиск по тексту
    # краснел на исправной правке.
    derevo = ast.parse(inspect.getsource(updater._tablicy_kopii))
    obrashcheniya = {u.attr for u in ast.walk(derevo) if isinstance(u, ast.Attribute)}
    assert "read_text" not in obrashcheniya, (
        "копия снова читается целиком — гигабайтный дамп положит откат посередине"
    )
    assert any(isinstance(u, ast.For) for u in ast.walk(derevo)), (
        "чтения по строкам не видно"
    )


def test_nechitaemaya_kopiya_ne_valit_otkat(tmp_path):
    """Беду чтения копии глотаем: вернуть рабочую базу важнее уборки.

    Пропавший файл, отнятые права, кончившаяся память — всё это прилетало бы
    наружу из `_snyat_lishnie` в самый неудачный миг отката. Пустое множество —
    верный ответ: у вызывающего пустота уже значит «сносить нечего».
    """
    from deploy.updater import _tablicy_kopii

    assert _tablicy_kopii(tmp_path / "ne-sushchestvuet.sql") == set()

    katalog = tmp_path / "eto-katalog.sql"
    katalog.mkdir()
    assert _tablicy_kopii(katalog) == set()


# --- отчёт называет причину, а не только «не поднялось» -----------------------
#
# Снаружи «сайт не отвечает» одинаково выглядит у трёх разных бед: код сломан,
# миграции упали, база не пускает. Отчёт про все три говорил «health-check не
# прошёл за N попыток», и человек шёл разбираться с нуля — при том что контейнер
# причину ЗНАЕТ и уже записал её в `update-state.json`.
#
# Поймано живым откатом на стенде: два обновления подряд откатились по РАЗНЫМ
# причинам (первое — сломанный код, второе — «таблица уже есть» от таблицы,
# оставшейся после первого), а в отчёте оба раза стояла одна и та же строка.


class ProbaSUpavshimKonteynerom(FakeProbe):
    """Проба, при которой контейнер успевает записать СВОЮ беду.

    Порядок здесь важнее удобства и повторяет живой. Обновлятор пишет ход
    (`running health`) сразу после `up -d`, а контейнер стартует и падает
    позже — значит его запись ложится ПОВЕРХ. Напиши подделка раньше, она бы
    проверяла обратный порядок, которого на живой машине не бывает, и тест
    зеленел бы там, где код не работает.
    """

    def __init__(self, put, step: str, error: str, **kwargs):
        super().__init__(**kwargs)
        self._put = put
        self._step = step
        self._error = error

    def get(self, url, follow=True):
        if "healthz" in url:
            self._put.parent.mkdir(parents=True, exist_ok=True)
            self._put.write_text(
                json.dumps(
                    {
                        "scope": "update",
                        "phase": "failed",
                        "step": self._step,
                        "started_at": "2026-08-19T18:00:00Z",
                        "error": self._error,
                    }
                ),
                encoding="utf-8",
            )
        return super().get(url, follow=follow)


def _proba_s_upavshim(config, step: str, error: str, **kwargs):
    put = config.data_dir.parent / "storage" / "branding" / "update-state.json"
    return ProbaSUpavshimKonteynerom(put, step, error, **kwargs)


def test_otchyot_nazyvaet_upavshie_migratsii_a_ne_tolko_zdorovye(tmp_path):
    """«Health-check не прошёл» — это симптом, а не причина.

    Контейнер точно знает, на чём он умер, и уже записал это. Не прочитать его —
    значит заставить человека разбираться заново с тем, что уже выяснено.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    updater = make_updater(
        tmp_path,
        config=config,
        shell=shell,
        probe=_proba_s_upavshim(
            config, "migrate", "Table 'otkat_sled' already exists", health=(False, True)
        ),
    )

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    zdorovye = next(step for step in outcome.steps if step.name == "health")
    assert "миграции" in zdorovye.detail, (
        "в отчёте нет причины: человек видит «не поднялось» и идёт выяснять то, "
        f"что контейнер уже выяснил. Вышло: {zdorovye.detail!r}"
    )
    assert "already exists" in zdorovye.detail


def test_otchyot_ne_vydumyvaet_prichinu_kogda_konteyner_molchit(tmp_path):
    """Файла нет или он не про беду — говорим только то, что знаем сами."""
    config = make_config(tmp_path)
    shell = FakeShell()
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    outcome = updater.run_once()

    zdorovye = next(step for step in outcome.steps if step.name == "health")
    assert "Контейнер сказал" not in zdorovye.detail


def test_prichina_ot_konteynera_ne_rastyot_bez_granits(tmp_path):
    """Хвост причины уезжает в Telegram, где длинное сообщение отбивается целиком."""
    config = make_config(tmp_path)
    shell = FakeShell()
    updater = make_updater(
        tmp_path,
        config=config,
        shell=shell,
        probe=_proba_s_upavshim(config, "migrate", "ы" * 5000, health=(False, True)),
    )

    outcome = updater.run_once()

    zdorovye = next(step for step in outcome.steps if step.name == "health")
    assert len(zdorovye.detail) < 600, f"причина разрослась до {len(zdorovye.detail)} знаков"



# --- сайт, закрытый на работы, не считается сломанным -------------------------


def test_obnovlenie_pri_rezhime_obsluzhivaniya_ne_otkatyvaet_bazu(tmp_path):
    """НАЙДЕНО РАЗБОРОМ: выкатка на закрытом сайте съедала работу владельца.

    Режим обслуживания отдаёт 503 всем, у кого нет сессии владельца, — значит
    smoke-тест не проходит НИКОГДА, пока сайт закрыт. Обновление считало это
    поломкой, откатывало код И БАЗУ (заливало дамп, снятый до сборки, поверх
    живой) и рапортовало «сломано». Всё, что владелец записал в CRM за это
    время — а он в ней работает, режим закрывает сайт от посетителей, не от
    него, — исчезало молча.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    updater = make_updater(
        tmp_path,
        config=config,
        shell=shell,
        # Приложение живо (schema сошлась), сайт закрыт, посетителю — 503.
        probe=FakeProbe(health=(True,), smoke=(False,), obsluzhivanie=True),
    )

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED, (
        f"выкатка на закрытом сайте объявлена провалом: {outcome.status}"
    )
    zalivki = [line for line, _ in shell.stdins if "mysql -uroot opencrm" in line]
    assert not zalivki, (
        "база откачена на закрытом сайте — потеряно всё, что владелец записал "
        "с момента съёмки копии"
    )
    assert not any(shag.name == "rollback-db" for shag in outcome.steps)


def test_pri_otkrytom_sayte_smoke_po_prezhnemu_sterezhet(tmp_path):
    """Пропуск smoke-тестов — только для закрытого сайта, и ни для чего больше.

    Иначе исправление сняло бы проверку вовсе: пятисотая на главной перестала
    бы останавливать выкатку.
    """
    updater = make_updater(
        tmp_path,
        probe=FakeProbe(health=(True,), smoke=(False,), obsluzhivanie=False),
    )

    outcome = updater.run_once()

    # Не `rolled-back`, а «что угодно, кроме успеха»: откат тут случается, но
    # СТАРЫЙ код тоже отдаёт посетителю ошибку (подстава отвечает 500 всегда),
    # и обновление честно зовёт это `broken`. Стережём здесь одно — что выкатка
    # НЕ засчитана; кто именно виноват, разбирают соседние проверки.
    assert outcome.status != STATUS_DEPLOYED, (
        "открытый сайт отдаёт посетителю ошибку, а выкатка засчитана"
    )
    assert any(shag.name.startswith("rollback") for shag in outcome.steps), (
        f"smoke-тест не остановил выкатку вовсе: {step_names(outcome)}"
    )


def test_smoke_ne_sprashivayut_kogda_sayt_zakryt(tmp_path):
    """И не спрашиваем вовсе: ответ известен заранее и ничего не значит."""
    probe = FakeProbe(health=(True,), smoke=(True,), obsluzhivanie=True)
    # Адрес задаём тем же ключом, каким его читает `UpdateConfig.from_env`.
    # Написанное как `smoke_urls=...` уехало бы в словарь окружения мёртвым
    # ключом, и проверка держалась бы на умолчании, читаясь так, будто задаёт
    # адрес сама.
    config = make_config(tmp_path, OPENCRM_UPDATE_SMOKE_URLS="http://sayt-zakryt.test/")
    updater = make_updater(tmp_path, config=config, probe=probe)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    assert config.smoke_urls == ("http://sayt-zakryt.test/",), (
        f"адрес не доехал до настроек — проверка держится на умолчании: {config.smoke_urls}"
    )
    assert not any("sayt-zakryt.test" in url for url in probe.calls), (
        f"smoke-адрес спрошен на закрытом сайте: {probe.calls}"
    )
    # И об этом сказано в отчёте, а не только в логе демона: зелёный шаг с
    # пустой подробностью читается как «страницы проверены».
    zdorovye = next(shag for shag in outcome.steps if shag.name == "health")
    assert "закрыт на работы" in zdorovye.detail, (
        f"пропуск smoke не виден в отчёте: {zdorovye.detail!r}"
    )


# --- копии базы не копятся без предела ----------------------------------------


def test_snimki_bazy_ne_kopyatsya_bez_predela(tmp_path):
    """НАЙДЕНО РАЗБОРОМ: обновления однажды вставали совсем.

    Каждое обновление кладёт `pre-update-<коммит>.sql`, каждый откат — ещё и
    `failed-update-<время>.sql`, и не удалялось из них ничего. На боевой базе
    дамп — гигабайт с лишним, а `preflight` требует свободных 2 ГБ. Три-четыре
    обновления — и обновление останавливается навсегда с сообщением про место,
    в котором про стопку дампов не сказано ни слова.
    """
    config = make_config(tmp_path)
    shell = FakeShell()
    # Восемь копий с разных прошлых обновлений, от старой к свежей.
    starye = []
    for nomer in range(8):
        put = config.state_dir / f"pre-update-staraya{nomer}.sql"
        put.write_text("INSERT INTO clients VALUES (1);\n", encoding="utf-8")
        os.utime(put, (1_600_000_000 + nomer, 1_600_000_000 + nomer))
        starye.append(put)

    updater = make_updater(tmp_path, config=config, shell=shell)
    outcome = updater.run_once()
    assert outcome.status == STATUS_DEPLOYED

    ostalis = sorted(p.name for p in config.state_dir.glob("pre-update-*.sql"))
    # `SNAPSHOTS_KEPT + 1`, и это не описка: уборка идёт ПЕРВЫМ шагом
    # обновления, до проверки места и до съёмки, — иначе на забитой дампами
    # машине preflight отказал бы раньше, чем уборка позвалась. Значит она
    # оставляет свои `SNAPSHOTS_KEPT`, а сверху ложится свежая копия.
    assert len(ostalis) == SNAPSHOTS_KEPT + 1, (
        f"копии не убираются: осталось {len(ostalis)} — {ostalis}"
    )
    # Свежая — та, что сняли сейчас; убрали именно САМЫЕ СТАРЫЕ.
    assert f"pre-update-{NEW[:12]}.sql" in ostalis, "убрали свежую копию — откатывать нечем"
    assert not starye[0].exists(), "самая старая копия осталась лежать"
    assert starye[-1].exists(), "убрали свежую копию вместо старой"


def test_neudachnye_dampy_ubirayutsya_toy_zhe_meroy(tmp_path):
    """`failed-update-*.sql` копятся так же и занимают столько же места."""
    config = make_config(tmp_path)
    for nomer in range(7):
        put = config.state_dir / f"failed-update-2026010{nomer}-000000.sql"
        put.write_text("INSERT INTO clients VALUES (1);\n", encoding="utf-8")
        os.utime(put, (1_600_000_000 + nomer, 1_600_000_000 + nomer))

    updater = make_updater(tmp_path, config=config)
    assert updater.run_once().status == STATUS_DEPLOYED

    # Здесь ровно `SNAPSHOTS_KEPT`: свежий `failed-update-*` кладёт только
    # неудачный откат, а эта выкатка удалась.
    ostalis = sorted(p.name for p in config.state_dir.glob("failed-update-*.sql"))
    assert len(ostalis) == SNAPSHOTS_KEPT, f"неудачные дампы копятся: {ostalis}"


def test_uborka_ne_valit_obnovlenie(tmp_path):
    """Беда с уборкой не смертельна: место кончится позже, обновление важнее.

    Неубираемое кладём каталогом с тем же именем: `unlink` на непустом каталоге
    отказывает по-настоящему, на любой системе, и подменять для этого ничего не
    нужно — подменённый `unlink` сорвал бы и съёмку свежей копии, то есть
    проверял бы совсем не то.
    """
    config = make_config(tmp_path)
    upryamyy = config.state_dir / "pre-update-upryamaya.sql"
    upryamyy.mkdir(parents=True)
    (upryamyy / "vnutri").write_text("x", encoding="utf-8")
    os.utime(upryamyy, (1_500_000_000, 1_500_000_000))
    for nomer in range(SNAPSHOTS_KEPT + 3):
        put = config.state_dir / f"pre-update-staraya{nomer}.sql"
        put.write_text("INSERT INTO clients VALUES (1);\n", encoding="utf-8")
        os.utime(put, (1_600_000_000 + nomer, 1_600_000_000 + nomer))

    outcome = make_updater(tmp_path, config=config).run_once()

    assert outcome.status == STATUS_DEPLOYED, (
        f"неубираемый файл остановил выкатку: {outcome.status}"
    )
    assert upryamyy.exists(), "каталог всё-таки снесли"
    assert (config.state_dir / f"pre-update-{NEW[:12]}.sql").is_file(), (
        "свежая копия не снята — откатывать нечем"
    )


def test_bez_polya_maintenance_sayt_schitaetsya_otkrytym(tmp_path):
    """Поля нет — проверяем страницы. Безопасная сторона именно эта.

    Случай не выдуманный: откат на коммит старше самого поля, переименование
    поля, чужая сборка. Прими мы «поля нет — значит закрыт», и smoke-тесты
    перестали бы спрашиваться НА КАЖДОЙ выкатке разом, молча и навсегда.

    Проверка нужна отдельная, потому что дубль `/healthz` теперь отдаёт поле
    всегда, и случай «старое приложение без него» иначе не проверялся бы вовсе:
    подлог `payload.get("maintenance") in ("on", None)` оставлял весь файл
    зелёным.
    """
    class BezPolya(FakeProbe):
        def get(self, url, follow=True):
            otvet = super().get(url, follow=follow)
            if "healthz" in url and otvet.status == 200:
                return Response(200, '{"status": "ok"}')
            return otvet

    probe = BezPolya(health=(True,), smoke=(False,))
    outcome = make_updater(tmp_path, probe=probe).run_once()

    assert outcome.status != STATUS_DEPLOYED, (
        "приложение не сказало про режим обслуживания, а страницы не проверили"
    )
    assert any("127.0.0.1" in url for url in probe.calls if "healthz" not in url), (
        f"smoke-адрес не спрашивали вовсе: {probe.calls}"
    )
