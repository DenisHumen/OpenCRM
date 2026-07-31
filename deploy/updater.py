"""Сценарий обновления: проверки → бэкап → деплой → health-check → откат.

Порядок шагов выбран так, чтобы у каждой неудачи была своя цена, и самые
дешёвые проверки шли первыми:

1. **Preflight.** Грязное рабочее дерево — стоп: правка, сделанная руками прямо
   на сервере, дороже автообновления, и затирать её молча нельзя.
2. **Тесты.** Гоняются на *новом* коде до того, как живой сайт вообще тронут:
   собирается образ `--target tests`, pytest внутри него. Красные тесты — деплой
   не начинался, посетители ничего не заметили.
3. **Снимок базы.** Только после зелёных тестов и обязательно до `alembic
   upgrade head`, который entrypoint запускает при старте контейнера. Миграции
   вперёд необратимы — без снимка откат кода вернул бы старое приложение к новой
   схеме.
4. **Деплой.** `docker compose up -d --build`.
5. **Health-check.** Не «контейнер поднялся», а живые ответы: `/healthz` с
   `status: ok` (он же ходит в базу) плюс smoke-запросы к настоящим страницам.
6. **Откат** при провале пятого шага: прошлый коммит, база из снимка, снова
   `up -d --build` и снова health-check. Если и он красный — статус `broken`,
   дальше нужен человек, и об этом приходит отдельное уведомление.

База возвращается из снимка **только если контейнер успели заменить**. Упади
сборка раньше — старое приложение всё это время обслуживало клиентов, миграции не
запускались, и откат базы стёр бы их работу за время сборки. Такая потеря тиха и
необнаружима, тогда как противоположная ошибка (старый код против новой схемы)
шумит и ловится тем же health-check'ом, а снимок остаётся лежать на диске.

Коммит, который не встал, запоминается (`failed_sha`) и сам собой больше не
пробуется: иначе демон каждые пять минут пересобирал бы заведомо сломанную
версию и слал бы об этом сообщения. Повтор — только `force-update` или
следующий коммит в ветке.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from deploy import notify
from deploy.config import UpdateConfig
from deploy.github import GitHub, GitHubError
from deploy.journal import Journal
from deploy.runner import HttpProbe, Result, Shell

STATUS_DISABLED = "disabled"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_DEPLOYED = "deployed"
STATUS_ABORTED = "aborted"  # остановились до подмены — живой сайт не трогали
STATUS_ROLLED_BACK = "rolled-back"
STATUS_BROKEN = "broken"  # откат тоже не поднялся

QUIET = {STATUS_DISABLED, STATUS_UP_TO_DATE}


class _Stop(Exception):
    """Шаг провалился; что делать дальше, решает `_deploy` по флагу `touched`."""


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Outcome:
    status: str
    from_sha: str = ""
    to_sha: str = ""
    summary: str = ""
    reason: str = ""
    steps: list[Step] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in {STATUS_DEPLOYED, STATUS_UP_TO_DATE, STATUS_DISABLED}

    def as_record(self) -> dict:
        record = asdict(self)
        record["seconds"] = round(self.seconds, 1)
        return record


class Updater:
    def __init__(
        self,
        config: UpdateConfig,
        journal: Journal | None = None,
        github: GitHub | None = None,
        shell: Shell | None = None,
        probe: HttpProbe | None = None,
        notifier=None,
        sleep=time.sleep,
        clock=time.monotonic,
        log=None,
    ) -> None:
        self.config = config
        self.log = log or (lambda _message: None)
        self.journal = journal or Journal(config.state_file, config.history_file)
        self.github = github or GitHub(config.repo, config.github_token)
        self.shell = shell or Shell(log=self.log)
        self.probe = probe or HttpProbe()
        self.notifier = notifier if notifier is not None else notify.from_config(config)
        self._sleep = sleep
        self._clock = clock

    # --- то, что вызывают снаружи ---

    def status(self) -> dict:
        state = self.journal.read()
        current = self.head_sha()
        available, error = "", ""
        try:
            # Без ETag: `status` спрашивают, чтобы узнать правду, а не сэкономить запрос.
            available = self.github.head(self.config.branch).sha
        except GitHubError as failure:
            error = str(failure)
        return {
            "repo": self.config.repo,
            "branch": self.config.branch,
            "deployed": current,
            "available": available,
            "update_available": bool(available and available != current),
            "autoupdate": self.journal.autoupdate_enabled,
            "failed_sha": state.get("failed_sha", ""),
            "github_error": error,
            "last": self.journal.last(),
        }

    def run_once(self, force: bool = False) -> Outcome:
        if not force and not self.journal.autoupdate_enabled:
            return Outcome(STATUS_DISABLED, from_sha=self.head_sha())

        try:
            head = self.github.head(self.config.branch, "" if force else self.journal.etag)
        except GitHubError as failure:
            # Сетевые сбои в историю не пишем: при опросе раз в пять минут они
            # затопили бы журнал. Видно их в логе демона и в `status`.
            self.log(f"опрос не удался: {failure}")
            return Outcome(STATUS_ABORTED, reason=str(failure))

        if not head.changed:
            return Outcome(STATUS_UP_TO_DATE, from_sha=self.head_sha())
        self.journal.write(etag=head.etag)

        current = self.head_sha()
        if head.sha == current and not force:
            self.journal.write(deployed_sha=current)
            return Outcome(STATUS_UP_TO_DATE, from_sha=current, to_sha=head.sha)

        if not force and head.sha == self.journal.read().get("failed_sha"):
            return Outcome(
                STATUS_ABORTED,
                from_sha=current,
                to_sha=head.sha,
                reason="этот коммит уже не встал — ждём следующий или force-update",
            )

        return self._deploy(current, head.sha)

    def watch(self, rounds: int | None = None) -> None:
        """Демон: опрашивать ветку, пока не остановят (`rounds` — предел для тестов)."""
        done = 0
        while rounds is None or done < rounds:
            try:
                outcome = self.run_once()
                if outcome.status not in QUIET:
                    self.log(f"{outcome.status}: {outcome.reason or outcome.to_sha[:12]}")
            except Exception as failure:  # noqa: BLE001 — демон не имеет права упасть
                self.log(f"неожиданная ошибка: {failure!r}")
            done += 1
            if rounds is None or done < rounds:
                self._sleep(self.config.poll_seconds)

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD").out.strip()

    # --- сам деплой ---

    def _deploy(self, previous: str, target: str) -> Outcome:
        started = self._clock()
        steps: list[Step] = []
        summary = self.github.summary(target)
        snapshot: Path | None = None
        touched = False  # тронули ли живой сайт
        migrated = False  # заменили ли контейнер, то есть могли ли пойти миграции
        self.log(f"обновление {previous[:12]} → {target[:12]} {summary}")

        try:
            self._preflight(steps)
            self._step(steps, "fetch", self._git("fetch", "--quiet", "origin", self.config.branch))
            self._step(steps, "checkout", self._git("checkout", "--detach", "--quiet", target))
            self._checks(steps)
            snapshot = self._snapshot(steps, target)

            touched = True
            self._step(
                steps,
                "deploy",
                self._compose("up", "-d", "--build", timeout=self.config.build_timeout),
            )
            migrated = True
            self._health(steps, "health")

            # Каждая сборка оставляет предыдущий образ висеть без тега. Демон
            # работает без присмотра месяцами, и забитый диск сломал бы не только
            # обновления, но и загрузку файлов. `prune` трогает только dangling.
            self._step(steps, "prune", self.shell.run(
                ["docker", "image", "prune", "-f"], cwd=self.config.project_dir, timeout=300,
            ), fatal=False)

            outcome = Outcome(STATUS_DEPLOYED, previous, target, summary, "", steps)
            self.journal.write(deployed_sha=target, failed_sha="")
        except _Stop as stop:
            if not touched:
                # Живой сайт не трогали — достаточно вернуть чекаут на место.
                # Пустой `previous` бывает, когда каталог вообще не репозиторий:
                # `git rev-parse HEAD` промолчал, и возвращаться некуда — команда
                # с пустым аргументом только мусорила бы в логе.
                if previous:
                    self._step(
                        steps, "restore-checkout",
                        self._git("checkout", "--detach", "--quiet", previous), fatal=False,
                    )
                outcome = Outcome(STATUS_ABORTED, previous, target, summary, str(stop), steps)
            else:
                broken = self._rollback(steps, previous, snapshot, restore_db=migrated)
                status = STATUS_BROKEN if broken else STATUS_ROLLED_BACK
                outcome = Outcome(status, previous, target, summary, str(stop), steps)
            self.journal.write(failed_sha=target)

        outcome.seconds = self._clock() - started
        self.journal.append(outcome.as_record())
        self._notify(outcome)
        return outcome

    def _preflight(self, steps: list[Step]) -> None:
        if not (self.config.project_dir / ".git").exists():
            steps.append(Step("preflight", False, "нет .git"))
            raise _Stop(f"{self.config.project_dir} — не git-репозиторий")

        dirty = self._git("status", "--porcelain")
        if not dirty.ok:
            steps.append(Step("preflight", False, dirty.tail(4)))
            raise _Stop(f"git status не отвечает: {dirty.tail(4)}")
        if dirty.out.strip() and not self.config.allow_dirty:
            steps.append(Step("preflight", False, dirty.out.strip()[:400]))
            raise _Stop("в рабочем дереве есть несохранённые правки — обновление затёрло бы их")
        steps.append(Step("preflight", True))

    def _checks(self, steps: list[Step]) -> None:
        """Тесты нового кода — до того, как живой сайт тронут.

        Гоняются в образе (`--target tests`), а не на хосте: на боевом сервере
        нет ни venv проекта, ни pytest, зато есть docker — тот же самый, которым
        через минуту собирается боевой образ.
        """
        if not self.config.run_checks:
            steps.append(Step("tests", True, "пропущены (OPENCRM_UPDATE_RUN_CHECKS=0)"))
            return
        dockerfile = self.config.project_dir / "docker" / "Dockerfile"
        self._step(
            steps,
            "tests",
            self.shell.run(
                ["docker", "build", "--target", "tests", "-f", str(dockerfile), "."],
                cwd=self.config.project_dir,
                timeout=self.config.checks_timeout,
            ),
        )

    def _snapshot(self, steps: list[Step], target: str) -> Path | None:
        """Копия базы перед миграциями. Без неё деплой не едет."""
        source = self.config.db_file
        if not source.exists():
            steps.append(Step("backup", True, "базы ещё нет — первый деплой"))
            return None

        destination = self.config.state_dir / f"pre-update-{target[:12]}.db"
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)

            # 1) Штатный путь: `.backup` в работающем контейнере даёт консистентную
            #    копию на горячую, не останавливая сайт (так же делают backup.sh и
            #    entrypoint.sh). Контейнер пишет в /app/data — это data_dir на хосте.
            inside = f"/app/data/{destination.name}"
            hot = self._compose("exec", "-T", "app", "sqlite3", "/app/data/" + self.config.db_name,
                                f".backup '{inside}'", timeout=300)
            landed = self.config.data_dir / destination.name
            if hot.ok and landed.exists():
                shutil.move(str(landed), str(destination))
                steps.append(Step("backup", True, destination.name))
                return destination

            # 2) Контейнер не отвечает — значит, в базу никто не пишет, и обычная
            #    копия файла вместе с журналом WAL тоже консистентна.
            landed.unlink(missing_ok=True)
            shutil.copy2(source, destination)
            for suffix in ("-wal", "-shm"):
                sidecar = source.with_name(source.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, destination.with_name(destination.name + suffix))
        except OSError as failure:
            steps.append(Step("backup", False, str(failure)))
            raise _Stop(f"не удалось снять копию базы: {failure}") from failure
        steps.append(Step("backup", True, f"{destination.name} (копия файла, контейнер не отвечал)"))
        return destination

    def _health(self, steps: list[Step], name: str) -> None:
        reason = self.wait_healthy()
        steps.append(Step(name, not reason, reason))
        if reason:
            raise _Stop(reason)

    def wait_healthy(self) -> str:
        """Пустая строка — сайт живой; иначе причина, годная для уведомления."""
        last = "нет ответа"
        for attempt in range(self.config.health_attempts):
            response = self.probe.get(self.config.health_url)
            if response.ok:
                try:
                    payload = json.loads(response.body)
                except ValueError:
                    last = f"{self.config.health_url}: ответ не JSON"
                else:
                    if payload.get("status") == "ok":
                        break
                    last = f"{self.config.health_url}: {payload}"
            else:
                last = f"{self.config.health_url}: {response.status or response.body[:120]}"
            if attempt + 1 < self.config.health_attempts:
                self._sleep(self.config.health_delay)
        else:
            return f"health-check не прошёл за {self.config.health_attempts} попыток — {last}"

        # Живая база — ещё не живой сайт: проверяем настоящие страницы.
        for url in self.config.smoke_urls:
            response = self.probe.get(url)
            if not response.ok:
                return f"smoke-тест {url}: {response.status or response.body[:120]}"
        return ""

    def _rollback(
        self, steps: list[Step], previous: str, snapshot: Path | None, restore_db: bool
    ) -> str:
        self.log(f"откат на {previous[:12]}")
        self._step(steps, "rollback-checkout",
                   self._git("checkout", "--detach", "--quiet", previous), fatal=False)
        # Контейнер держит файл базы открытым — до подмены его надо остановить.
        self._step(steps, "rollback-stop", self._compose("stop", "app", timeout=300), fatal=False)

        if not restore_db:
            # Деплой не дошёл до подмены контейнера: миграции не запускались, а
            # старое приложение всё это время обслуживало клиентов. Вернуть базу
            # к снимку значило бы молча стереть их работу за время сборки —
            # потеря, которую никто не заметит. Снимок остаётся на диске.
            kept = f", снимок {snapshot.name} на месте" if snapshot else ""
            steps.append(Step("rollback-db", True, f"миграции не запускались{kept}"))
        elif snapshot is None:
            steps.append(Step("rollback-db", True, "снимка не было — первый деплой"))
        else:
            failure = self._restore_db(snapshot)
            steps.append(Step("rollback-db", not failure, failure or f"из {snapshot.name}"))

        self._step(steps, "rollback-deploy",
                   self._compose("up", "-d", "--build", timeout=self.config.build_timeout),
                   fatal=False)
        reason = self.wait_healthy()
        steps.append(Step("rollback-health", not reason, reason))
        return reason

    def _restore_db(self, snapshot: Path) -> str:
        """Вернуть базу из снимка. Пустая строка — получилось."""
        target = self.config.db_file
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            if target.exists():
                # Неудачную базу откладываем, а не стираем: в ней данные за время
                # неудавшегося обновления, и разбираться с ними будет человек.
                target.replace(target.with_name(f"{target.name}.failed-update-{stamp}"))
            shutil.copy2(snapshot, target)
            # Журнал WAL от прежней базы SQLite доиграл бы поверх восстановленной —
            # снимок `.backup` самодостаточен, боковые файлы должны исчезнуть.
            for suffix in ("-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
                sidecar = snapshot.with_name(snapshot.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, target.with_name(target.name + suffix))
        except OSError as failure:
            return str(failure)
        return ""

    # --- мелочь ---

    def _git(self, *args: str) -> Result:
        return self.shell.run(["git", "-C", str(self.config.project_dir), *args], timeout=300)

    def _compose(self, *args: str, timeout: float | None = None) -> Result:
        return self.shell.run(
            ["docker", "compose", "-f", str(self.config.compose_file), *args],
            cwd=self.config.project_dir,
            timeout=timeout,
        )

    def _step(self, steps: list[Step], name: str, result: Result, fatal: bool = True) -> Result:
        steps.append(Step(name, result.ok, "" if result.ok else result.tail()))
        if not result.ok and fatal:
            raise _Stop(f"{name}: {result.tail(4)}")
        return result

    def _notify(self, outcome: Outcome) -> None:
        titles = {
            STATUS_DEPLOYED: "OpenCRM обновлён",
            STATUS_ROLLED_BACK: "OpenCRM: обновление откачено",
            STATUS_BROKEN: "OpenCRM: откат не поднялся, нужен человек",
            STATUS_ABORTED: "OpenCRM: обновление не начиналось",
        }
        title = titles.get(outcome.status)
        if not title:
            return
        lines = [title, f"{self.config.repo}@{self.config.branch}"]
        if outcome.to_sha:
            lines.append(f"{outcome.from_sha[:12] or '—'} → {outcome.to_sha[:12]}")
        if outcome.summary:
            lines.append(outcome.summary)
        if outcome.reason:
            lines.append("")
            lines.append(outcome.reason)
        failed = [step for step in outcome.steps if not step.ok]
        if failed:
            lines.append("")
            lines += [f"✗ {step.name}: {step.detail}".rstrip(": ") for step in failed]
        lines.append("")
        lines.append(f"{outcome.seconds:.0f} c")
        self.notifier.send("\n".join(lines))


def build(config: UpdateConfig, log=None) -> Updater:
    return Updater(config, log=log)
