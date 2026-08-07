"""Сценарий обновления: проверки → бэкап → деплой → health-check → откат.

Порядок шагов выбран так, чтобы у каждой неудачи была своя цена, и самые
дешёвые проверки шли первыми:

1. **Preflight.** Грязное рабочее дерево — стоп: правка, сделанная руками прямо
   на сервере, дороже автообновления, и затирать её молча нельзя.
2. **Проверки GitHub.** Тот же коммит уже прогнан в Actions; красный туда и
   приехал красным, и тратить на него полчаса сборки на боевом VPS незачем.
3. **Тесты.** Гоняются на *новом* коде до того, как живой сайт вообще тронут:
   собирается образ `--target tests`, pytest внутри него. Красные тесты — деплой
   не начинался, посетители ничего не заметили.
4. **Снимок базы.** Только после зелёных тестов и обязательно до `alembic
   upgrade head`, который entrypoint запускает при старте контейнера. Миграции
   вперёд необратимы — без снимка откат кода вернул бы старое приложение к новой
   схеме.
5. **Деплой.** `docker compose up -d --build`.
6. **Health-check.** Не «контейнер поднялся», а живые ответы: `/healthz` с
   `status: ok` (он же ходит в базу) плюс smoke-запросы к настоящим страницам.
7. **Откат** при провале шестого шага: прошлый коммит, база из снимка, снова
   `up -d --build` и снова health-check. Если и он красный — статус `broken`,
   дальше нужен человек, и об этом приходит отдельное уведомление.

База возвращается из снимка **только если контейнер успели заменить**. Упади
сборка раньше — старое приложение всё это время обслуживало клиентов, миграции не
запускались, и откат базы стёр бы их работу за время сборки. Такая потеря тиха и
необнаружима, тогда как противоположная ошибка (старый код против новой схемы)
шумит и ловится тем же health-check'ом, а снимок остаётся лежать на диске.

Пока всё это идёт, посетитель видит страницу обслуживания, и ход обновления ей
рассказывают файлом: шаги 4–6 отмечает этот модуль, миграции и старт —
`docker/entrypoint.sh` уже изнутри контейнера. Подробности — у `PROGRESS_NAME`.

Коммит, который не встал, запоминается (`failed_sha`) и сам собой больше не
пробуется: иначе демон каждые пять минут пересобирал бы заведомо сломанную
версию и слал бы об этом сообщения. Повтор — только `force-update` или
следующий коммит в ветке.

Все git-команды идут с `-c safe.directory=<чекаут>`. Каталог на боевом сервере
принадлежит человеку, который его клонировал, а обновлятор запускают то от него,
то от root через `sudo` — и тогда git отвечает `detected dubious ownership` и
отказывается работать вовсе. Это защита от чужого репозитория в общем каталоге;
здесь каталог свой и назван явно, поэтому исключение точечное, а не `git config
--global` на всю машину.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from deploy import notify
from deploy.config import UpdateConfig
from deploy.github import CHECKS_FAILURE, CHECKS_PENDING, GitHub, GitHubError
# _atomic_write, а не своя запись: гарантия ровно та же, что у состояния демона —
# недописанного JSON на диске не бывает. Ход обновления читают в произвольный
# момент, и половина файла разобралась бы у страницы как порча.
from deploy.journal import Journal, _atomic_write
from deploy.runner import HttpProbe, Result, Shell

STATUS_DISABLED = "disabled"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_DEPLOYED = "deployed"
STATUS_ABORTED = "aborted"  # остановились до подмены — живой сайт не трогали
STATUS_WAITING = "waiting-checks"  # коммит хороший, но CI ещё не досчитал
STATUS_ROLLED_BACK = "rolled-back"
STATUS_BROKEN = "broken"  # откат тоже не поднялся

QUIET = {STATUS_DISABLED, STATUS_UP_TO_DATE}

# Файлы, из которых работает сам демон обновления. Изменились — процессу нужен
# перезапуск: Python загрузил их при старте и правку на диске не заметит.
SELF_PATHS = ("deploy/", "scripts/autoupdate.py")

# --- ход обновления для страницы обслуживания ---
#
# Пока идёт обновление, приложения нет по определению: заглушку отдаёт nginx, и
# спросить «на каком мы шаге» ему не у кого. Значит шаги надо положить в файл,
# который nginx отдаст сам, статикой.
#
# Файл кладётся в `storage/branding` — тот самый каталог, который любой уже
# развёрнутый nginx раздаёт по адресу `/branding/` (docker/nginx/templates/
# locations.inc). Своего `location` под это не заводим, и не из аккуратности:
# конфиг nginx примонтирован из чекаута, а работающий nginx держит в памяти тот,
# что прочитал при своём запуске (об этом же — `_reload_nginx` ниже). Новый
# location начал бы действовать только со СЛЕДУЮЩЕГО обновления, то есть ровно
# на том обновлении, которое эту страницу привозит, ход был бы не виден. То же и
# с новым томом: docker-compose.yml перечитывается только на `up`, а каталог
# данных в nginx не примонтирован вовсе.
#
# Файл по устройству публичен: его читает страница, за которой нет ни сессии, ни
# приложения. Поэтому в нём только то, что не жалко показать постороннему — имя
# шага и короткий хвост причины; подробности уходят в историю и в Telegram.
PROGRESS_NAME = "update-state.json"

# Шаги, которые видит посетитель. Порядок настоящий, а не «по здравому смыслу»:
# мигрировать базу до того, как собран образ с новым кодом, нечем. `migrate` и
# `start` пишет не этот файл, а docker/entrypoint.sh изнутри контейнера — он
# один и знает, когда миграции пошли и когда приложение отправилось на старт.
PROGRESS_STEPS = ("backup", "build", "migrate", "start", "health")

# Длиннее в файл не пишем: он публичен, а простыня из лога сборки не помогает
# ни посетителю, ни отладке — для отладки есть история и уведомление. Столько же
# отрезает docker/entrypoint.sh, чтобы правило было одно на обоих писателей.
PROGRESS_ERROR_LIMIT = 200


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
        # Последний объявленный посетителю шаг и время начала обновления.
        # Пустые до первого объявления — по этому и видно, что показывать пока
        # нечего (см. `_progress_finish`).
        self._progress_step = ""
        self._progress_started = ""

    # --- то, что вызывают снаружи ---

    def status(self) -> dict:
        state = self.journal.read()
        current = self.head_sha()
        available, error = "", ""
        checks = None
        try:
            # Без ETag: `status` спрашивают, чтобы узнать правду, а не сэкономить запрос.
            available = self.github.head(self.config.branch).sha
            # Почему сайт не обновляется — вопрос, который задают этой команде.
            # Без строки о проверках «обновление: есть» и тишина выглядят
            # поломкой, хотя это гейт штатно ждёт зелёного CI.
            if available and available != current and self.config.require_ci:
                checks = self.github.checks(available)
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
            "checks": checks.state if checks else "",
            "checks_detail": checks.detail if checks else "",
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

        gate = self._ci_gate(current, head.sha, force)
        if gate is not None:
            return gate

        return self._deploy(current, head.sha)

    def _ci_gate(self, current: str, target: str, force: bool) -> Outcome | None:
        """Проверки GitHub на входящем коммите. `None` — путь свободен.

        `force-update` гейт не проходит вовсе: это сознательное решение человека,
        и запирать его здесь значило бы отнять возможность выкатить фикс ровно
        тогда, когда CI и сломан.

        Ожидание и отказ разведены нарочно, и разница между ними — не косметика:

        * **pending** — коммит не виноват, он просто ещё считается. В `failed_sha`
          его писать нельзя (он больше не попробуется никогда), в историю и в
          Telegram — тоже: при опросе раз в пять минут получилось бы несколько
          одинаковых сообщений на каждый коммит. Заодно сбрасываем ETag: иначе
          следующий опрос получит от GitHub 304 «ничего не изменилось», выйдет
          через `up-to-date` и не дойдёт до этой проверки — CI позеленел бы, а
          обновление осталось бы стоять до самого следующего коммита.
        * **failure** — коммит виноват. Это событие, о нём пишем в историю и
          уведомляем, и `failed_sha` не даёт повторять новость каждые пять минут.
        """
        if force or not self.config.require_ci:
            return None

        try:
            checks = self.github.checks(target)
        except GitHubError as failure:
            # «Не смогли спросить» — не то же самое, что «тесты красные».
            # Деплоить вслепую нельзя, но и клеймить коммит не за что.
            self.log(f"проверки GitHub недоступны: {failure}")
            self.journal.write(etag="")
            return Outcome(
                STATUS_WAITING, current, target, reason=f"проверки GitHub недоступны: {failure}"
            )

        if checks.state == CHECKS_PENDING:
            self.log(f"жду проверки GitHub: {checks.detail}")
            self.journal.write(etag="")
            return Outcome(
                STATUS_WAITING, current, target, reason=f"проверки GitHub ещё идут: {checks.detail}"
            )

        if checks.state == CHECKS_FAILURE:
            outcome = Outcome(
                STATUS_ABORTED,
                current,
                target,
                self.github.summary(target),
                f"проверки GitHub красные: {checks.detail}",
                [Step("github-checks", False, checks.detail)],
            )
            self.journal.write(failed_sha=target)
            self.journal.append(outcome.as_record())
            self._notify(outcome)
            return outcome

        return None

    def watch(self, rounds: int | None = None) -> None:
        """Демон: опрашивать ветку, пока не остановят (`rounds` — предел для тестов)."""
        done = 0
        # SHA, на котором демон стартовал: с него и загружен его собственный код.
        started_from = self.head_sha()
        while rounds is None or done < rounds:
            try:
                outcome = self.run_once()
                if outcome.status not in QUIET:
                    self.log(f"{outcome.status}: {outcome.reason or outcome.to_sha[:12]}")
            except Exception as failure:  # noqa: BLE001 — демон не имеет права упасть
                self.log(f"неожиданная ошибка: {failure!r}")
            done += 1
            if self._self_changed(started_from):
                # Выходим, а не продолжаем: systemd поднимет службу заново
                # (Restart=always), и следующий круг пойдёт уже новым кодом.
                self.log(
                    "код обновлятора изменился — выхожу, чтобы systemd поднял меня новым"
                )
                return
            if rounds is None or done < rounds:
                self._sleep(self.config.poll_seconds)

    def _self_changed(self, since: str) -> bool:
        """Обновился ли код, из которого работает сам демон.

        Python читает исходники один раз, при импорте. Обновление правит файлы
        на диске, но уже запущенный процесс продолжает работать тем, что загрузил
        при старте, — и правка деплоя вступает в силу только после перезапуска
        службы. На боевом сервере это стоило отдельного разбора: smoke-тест
        чинили дважды, а деплой оба раза падал старым кодом, потому что демон
        крутился с прошлой недели.

        Проверяем именно свои файлы, а не любое изменение: перезапуск ради чужой
        правки — лишний простой в опросе.
        """
        if not since:
            return False
        now = self.head_sha()
        if not now or now == since:
            return False
        result = self._git("diff", "--name-only", since, now)
        if result.code != 0:
            return False
        return any(line.strip().startswith(SELF_PATHS) for line in result.out.splitlines())

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
        # Новая попытка — новый отсчёт: время начала прошлой на странице
        # выглядело бы как «обновляемся уже четвёртый час».
        self._progress_step = ""
        self._progress_started = ""
        self.log(f"обновление {previous[:12]} → {target[:12]} {summary}")

        try:
            self._preflight(steps)
            self._step(steps, "fetch", self._git("fetch", "--quiet", "origin", self.config.branch))
            self._step(steps, "checkout", self._git("checkout", "--detach", "--quiet", target))
            self._checks(steps)
            # Шаги объявляются ДО работы, а не после: страницу читают ровно в ту
            # минуту, когда шаг идёт, а «сделано» посетителю уже неинтересно.
            self._progress("backup")
            snapshot = self._snapshot(steps, target)

            touched = True
            self._progress("build")
            self._step(
                steps,
                "deploy",
                self._compose("up", "-d", "--build", timeout=self.config.build_timeout),
            )
            migrated = True
            self._reload_nginx(steps)
            self._progress("health")
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
        self._progress_finish(outcome, touched)
        self.journal.append(outcome.as_record())
        self._notify(outcome)
        return outcome

    def _preflight(self, steps: list[Step]) -> None:
        if not (self.config.project_dir / ".git").exists():
            steps.append(Step("preflight", False, "нет .git"))
            raise _Stop(f"{self.config.project_dir} — не git-репозиторий")

        # `core.fileMode=false` — только здесь, и только для этой проверки.
        # Бит исполнения — не правка: содержимого в нём нет, и `checkout` всё
        # равно расставит его из дерева. А считать его правкой — значит намертво
        # заклинить обновления, потому что взводит его сама установка
        # (`chmod +x opencrm.sh`) и любой распаковщик архива. На боевом сервере
        # это выглядело как вечное «в рабочем дереве есть несохранённые правки —
        # M opencrm.sh», которое нечем было объяснить и не за что откатить.
        dirty = self._git("status", "--porcelain", config=("core.fileMode=false",))
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

    def _reload_nginx(self, steps: list[Step]) -> None:
        """Попросить nginx перечитать конфиг после обновления кода.

        **Без этого правки конфига не применяются вовсе.** Файлы nginx
        примонтированы из чекаута, а не лежат в его образе: `git checkout`
        меняет их на диске мгновенно. Но `docker compose up -d --build`
        пересоздаёт только те службы, у которых изменилось описание или образ, —
        у nginx не меняется ни то, ни другое, и он остаётся работать с
        конфигом, прочитанным при своём запуске. Сам он за файлами не следит.

        Проверено репетицией обновления: после `up -d --build` compose тронул
        только `app`, и nginx продолжал раздавать `/media/` прямо с диска, хотя
        новый конфиг на диске уже проксировал этот путь в приложение. То есть
        починка, закрывшая файлы витрины после отзыва ссылки, молча не
        действовала — до ближайшего перезапуска nginx.

        Само по себе оно чинится: у nginx есть свой цикл `reload` раз в шесть
        часов (продление сертификата). Но «применится когда-нибудь в течение
        шести часов» — не то, на что можно опираться, проверяя обновление.

        Перезагрузка мягкая: старые рабочие процессы дорабатывают начатые
        запросы, порты не переоткрываются, простоя нет. Шаг не смертельный —
        nginx мог быть не поднят вовсе (у кого-то свой снаружи), и валить из-за
        этого удавшееся обновление незачем.
        """
        self._step(
            steps,
            "nginx-reload",
            self._compose("exec", "-T", "nginx", "nginx", "-s", "reload", timeout=60),
            fatal=False,
        )

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
                    # Схема базы — отдельный вопрос от «живо ли приложение», и
                    # спрашиваем мы его явно. Само приложение с несошедшейся
                    # схемой не поднимается вовсе, то есть до сюда дело обычно не
                    # доходит; но если однажды дойдёт — обновление обязано
                    # откатиться, а не объявить успех. Незамеченное расхождение
                    # стоит рабочего дня, в течение которого раздел отвечает 500.
                    if payload.get("schema") not in (None, "ok"):
                        last = (
                            f"{self.config.health_url}: база не соответствует моделям "
                            f"(schema={payload.get('schema')})"
                        )
                    elif payload.get("status") == "ok":
                        break
                    else:
                        last = f"{self.config.health_url}: {payload}"
            else:
                last = f"{self.config.health_url}: {response.status or response.body[:120]}"
            if attempt + 1 < self.config.health_attempts:
                self._sleep(self.config.health_delay)
        else:
            return f"health-check не прошёл за {self.config.health_attempts} попыток — {last}"

        # Живая база — ещё не живой сайт: проверяем настоящие страницы.
        #
        # Без хождения по редиректу и с зачётом 3xx. Проверки идут с самой
        # машины по http://127.0.0.1/, а сайт на HTTPS отвечает на это
        # перенаправлением на https://127.0.0.1/ — где сертификат, выписанный на
        # домен, к IP-адресу не подходит по определению. Пойти по такому
        # редиректу значит всегда упереться в ошибку сертификата: после переезда
        # на HTTPS деплой падал и откатывался при полностью работающем сайте.
        #
        # Само перенаправление и есть доказательство жизни: его выдаёт
        # настроенный nginx, а живость приложения уже подтвердил /healthz выше.
        for url in self.config.smoke_urls:
            response = self.probe.get(url, follow=False)
            if not response.alive:
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

    # --- ход обновления для страницы обслуживания ---

    def _progress_path(self) -> Path:
        # `data/` и `storage/` compose кладёт рядом, под одним `OPENCRM_HOME`
        # (docker/docker-compose.yml), а в UpdateConfig из этой пары назван
        # только первый. Отсюда `parent`: заводить вторую переменную окружения
        # ради того же каталога значило бы создать способ развести их между
        # собой — и в тот же день обновление начало бы писать ход туда, откуда
        # nginx ничего не отдаёт.
        return self.config.data_dir.parent / "storage" / "branding" / PROGRESS_NAME

    def _progress(self, step: str, phase: str = "running", error: str = "") -> None:
        """Сообщить странице обслуживания, на каком мы шаге.

        Ход — удобство для посетителя, а не часть обновления: любая беда с
        записью гасится здесь же. Уронить деплой из-за файла, который нужен
        только чтобы нарисовать список, было бы обменом наоборот.
        """
        if phase == "running":
            self._progress_step = step
            self._progress_started = self._progress_started or _utc_now()
        payload = {
            "scope": "update",
            "phase": phase,
            "step": step,
            "started_at": self._progress_started or _utc_now(),
            "error": error.strip()[:PROGRESS_ERROR_LIMIT],
        }
        try:
            _atomic_write(
                self._progress_path(),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        except OSError as failure:
            self.log(f"не удалось записать ход обновления: {failure}")

    def _progress_finish(self, outcome: Outcome, touched: bool) -> None:
        """Итог на ту же страницу — или чистый лист, если показывать нечего.

        Пока обновление не тронуло живой сайт, страницы обслуживания никто не
        видел: приложение работало и отвечало само. Оставить там «не удалось»
        значило бы положить на диск испуг, который всплывёт при следующем — уже
        постороннем — падении сайта и соврёт про его причину. Поэтому такой
        след стирается, а не переписывается.
        """
        if not self._progress_step:
            return
        if not touched:
            try:
                self._progress_path().unlink(missing_ok=True)
            except OSError as failure:
                self.log(f"не удалось убрать ход обновления: {failure}")
        elif outcome.status == STATUS_DEPLOYED:
            self._progress(self._progress_step, phase="done")
        else:
            self._progress(self._progress_step, phase="failed", error=outcome.reason)

    # --- мелочь ---

    def _git(self, *args: str, config: tuple[str, ...] = ()) -> Result:
        options: list[str] = []
        for item in (f"safe.directory={self.config.project_dir}", *config):
            options += ["-c", item]
        return self.shell.run(
            ["git", *options, "-C", str(self.config.project_dir), *args], timeout=300
        )

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
        # STATUS_WAITING в списке нет намеренно: ожидание зелёного CI — не
        # новость, а нормальный ход дела, и уведомлять о нём каждые пять минут
        # значило бы приучить не читать эти сообщения.
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


def _utc_now() -> str:
    """Время начала — в UTC и с явной `Z`.

    Часовой пояс сервера странице неизвестен, а показать надо местное время
    посетителя: без пометки зоны браузер разобрал бы строку как своё локальное
    время и «начало» уехало бы на несколько часов.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build(config: UpdateConfig, log=None) -> Updater:
    return Updater(config, log=log)
