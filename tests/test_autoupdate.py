"""Автообновление сайта (пакет `deploy`).

Сценарий деплоя целиком проверяется здесь, а не руками на сервере: откат
случается ровно тогда, когда всё уже плохо, и «проверю, когда понадобится» для
него не работает. Внешний мир — команды, HTTP, GitHub, Telegram — подменяется
двойниками из `runner`-контракта, поэтому прогон не собирает образ и не поднимает
сервер.
"""

import json
import shutil
import subprocess

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

    def fail(self, needle: str, err: str = "boom"):
        self.rules.append((needle, 1, "", err))

    def effect(self, needle: str, action):
        self.effects.append((needle, action))

    def run(self, argv, cwd=None, timeout=None):
        line = " ".join(str(part) for part in argv)
        self.calls.append(line)
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

    def __init__(self, health=(True,), smoke=(True,), smoke_status=200):
        self.health = list(health)
        self.smoke = list(smoke)
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
            return Response(200, '{"status": "ok"}') if ok else Response(502, "bad gateway")
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

    def send(self, text):
        self.messages.append(text)
        return True


# --- обвязка ---


def make_config(tmp_path, **extra) -> UpdateConfig:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    env = {
        "OPENCRM_HOME": str(tmp_path / "home"),
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


def make_updater(tmp_path, *, shell=None, probe=None, github=None, config=None, **extra):
    config = config or make_config(tmp_path, **extra)
    return Updater(
        config,
        journal=Journal(config.state_file, config.history_file),
        github=github or FakeGitHub(),
        shell=shell or FakeShell(),
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
    assert config.db_file == tmp_path / "opencrm" / "data" / "opencrm.db"
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
        "preflight", "fetch", "checkout", "tests", "backup", "deploy",
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

    checks = next(i for i, call in enumerate(updater.shell.calls) if "--target tests" in call)
    deploy = next(i for i, call in enumerate(updater.shell.calls) if "up -d --build" in call)
    assert checks < deploy


def test_the_database_is_copied_before_the_migrations(tmp_path):
    """Миграции накатывает entrypoint при старте контейнера — снимок нужен раньше."""
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("старая база"))
    updater = make_updater(tmp_path, config=config)

    outcome = updater.run_once()

    assert step_names(outcome).index("backup") < step_names(outcome).index("deploy")
    snapshot = config.state_dir / f"pre-update-{NEW[:12]}.db"
    assert snapshot.read_bytes() == blob("старая база")


def test_a_hot_snapshot_is_taken_through_the_running_container(tmp_path):
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("живая база"))
    shell = FakeShell()
    # sqlite3 .backup внутри контейнера пишет в /app/data — это data_dir на хосте
    shell.effect(
        "sqlite3",
        lambda: (config.data_dir / f"pre-update-{NEW[:12]}.db").write_bytes(blob("снимок")),
    )
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    snapshot = config.state_dir / f"pre-update-{NEW[:12]}.db"
    assert snapshot.read_bytes() == blob("снимок")
    assert not (config.data_dir / snapshot.name).exists()  # перенесён, а не скопирован


def test_the_first_deploy_has_no_database_to_copy(tmp_path):
    updater = make_updater(tmp_path)  # data_dir пуст

    outcome = updater.run_once()

    assert outcome.status == STATUS_DEPLOYED
    backup = next(step for step in outcome.steps if step.name == "backup")
    assert backup.ok and "первый деплой" in backup.detail


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
    shell.fail("--target tests", err="2 failed")
    updater = make_updater(tmp_path, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert not shell.ran("up -d --build")
    assert shell.ran(f"checkout --detach --quiet {OLD}")  # чекаут вернули на место
    assert "2 failed" in outcome.reason


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
    assert not updater.shell.ran("--target tests")
    tests_step = next(step for step in outcome.steps if step.name == "tests")
    assert "пропущены" in tests_step.detail


def test_a_deploy_without_a_snapshot_does_not_start(tmp_path):
    """Без копии базы миграции необратимы — лучше не обновляться вовсе."""
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("база"))
    # Класть копию некуда: путь снимка занят каталогом.
    (config.state_dir / f"pre-update-{NEW[:12]}.db").mkdir()
    updater = make_updater(tmp_path, config=config)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ABORTED
    assert "копию базы" in outcome.reason
    assert not updater.shell.ran("up -d --build")


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
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("база до обновления"))
    updater = make_updater(tmp_path, config=config, probe=FakeProbe(health=(False, True)))

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    assert config.db_file.read_bytes() == blob("база до обновления")


def test_the_rollback_drops_the_stale_write_ahead_log(tmp_path):
    """База работает в WAL; журнал новой версии доиграл бы поверх восстановленной."""
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("база"))
    wal = config.db_file.with_name(config.db_file.name + "-wal")
    shell = FakeShell()
    # снимок через контейнер самодостаточен — боковых файлов у него нет
    shell.effect(
        "sqlite3",
        lambda: (config.data_dir / f"pre-update-{NEW[:12]}.db").write_bytes(blob("снимок")),
    )
    shell.effect("up -d --build", once(lambda: wal.write_bytes(blob("журнал новой версии"))))
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    updater.run_once()

    assert not wal.exists()


def test_the_failed_database_is_kept_aside(tmp_path):
    """В ней данные за время неудавшегося обновления — разбирается человек."""
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("было"))
    shell = FakeShell()
    # миграции нового кода переписали базу — их работа и должна отложиться в сторону
    shell.effect("up -d --build", once(lambda: config.db_file.write_bytes(blob("стало после миграций"))))
    updater = make_updater(
        tmp_path, config=config, shell=shell, probe=FakeProbe(health=(False, True))
    )

    updater.run_once()

    kept = list(config.data_dir.glob("opencrm.db.failed-update-*"))
    assert len(kept) == 1
    assert kept[0].read_bytes() == blob("стало после миграций")
    assert config.db_file.read_bytes() == blob("было")


def test_a_build_failure_leaves_the_database_alone(tmp_path):
    """Сборка упала — старое приложение всё это время работало и принимало записи.

    Откатить базу к снимку значило бы молча стереть работу клиентов за время
    сборки. Миграции не запускались, возвращать нечего.
    """
    config = make_config(tmp_path)
    config.db_file.write_bytes(blob("снимок"))
    shell = FakeShell()
    shell.effect("up -d --build", once(lambda: config.db_file.write_bytes(blob("живые записи"))))
    shell.fail("up -d --build", err="build failed")
    updater = make_updater(tmp_path, config=config, shell=shell)

    outcome = updater.run_once()

    assert outcome.status == STATUS_ROLLED_BACK
    assert config.db_file.read_bytes() == blob("живые записи")
    restore = next(step for step in outcome.steps if step.name == "rollback-db")
    assert restore.ok and "миграции не запускались" in restore.detail
    # снимок никуда не делся — если что, восстановить можно руками
    assert (config.state_dir / f"pre-update-{NEW[:12]}.db").exists()


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
