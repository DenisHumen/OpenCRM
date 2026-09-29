"""Сценарий обновления: проверки → бэкап → деплой → health-check → откат.

Порядок шагов выбран так, чтобы у каждой неудачи была своя цена, и самые
дешёвые проверки шли первыми:

1. **Preflight.** Грязное рабочее дерево — стоп: правка, сделанная руками прямо
   на сервере, дороже автообновления, и затирать её молча нельзя.
2. **Проверки GitHub.** Тот же коммит уже прогнан в Actions; красный туда и
   приехал красным, и тратить на него полчаса сборки на боевом VPS незачем.
3. **Тесты.** Гоняются на *новом* коде до того, как живой сайт вообще тронут:
   образ `--target tests` поднимается рядом со СВОЕЙ базой
   (`docker/docker-compose.tests.yml`), pytest внутри него. Красные тесты —
   деплой не начинался, посетители ничего не заметили.
4. **Настройки.** Тесты знают, что код исправен, но не знают, заработает ли он
   *здесь*: `config/.env` в репозиторий не входит. Новый код собирается и
   спрашивается о своих требованиях (`python -m config.selfcheck`) — тоже до
   того, как сайт тронут. Иначе нехватка одной строки в настройках вылезает уже
   после подмены контейнера: 502, откат кода и базы, и так каждые полчаса.
5. **Снимок базы.** Только после зелёных тестов и обязательно до `alembic
   upgrade head`, который entrypoint запускает при старте контейнера. Миграции
   вперёд необратимы — без снимка откат кода вернул бы старое приложение к новой
   схеме.
6. **Деплой.** `docker compose up -d --build`.
7. **Health-check.** Не «контейнер поднялся», а живые ответы: `/healthz` с
   `status: ok` (он же ходит в базу) плюс smoke-запросы к настоящим страницам.
8. **Откат** при провале седьмого шага: прошлый коммит, база из снимка, снова
   `up -d --build` и снова health-check. Если и он красный — статус `broken`,
   дальше нужен человек, и об этом приходит отдельное уведомление.

База возвращается из снимка **только если контейнер успели заменить**. Упади
сборка раньше — старое приложение всё это время обслуживало клиентов, миграции не
запускались, и откат базы стёр бы их работу за время сборки. Такая потеря тиха и
необнаружима, тогда как противоположная ошибка (старый код против новой схемы)
шумит и ловится тем же health-check'ом, а снимок остаётся лежать на диске.

Пока всё это идёт, посетитель видит страницу обслуживания, и ход обновления ей
рассказывают файлом: шаги 5–7 отмечает этот модуль, миграции и старт —
`docker/entrypoint.sh` уже изнутри контейнера. Подробности — у `PROGRESS_NAME`.

Коммит, который не встал, запоминается (`failed_sha`) и сам собой больше не
пробуется: иначе демон каждые пять минут пересобирал бы заведомо сломанную
версию и слал бы об этом сообщения. Повтор — только `force-update` или
следующий коммит в ветке. Отказ СЕТИ до подмены (`SETEVOY_OTKAZ`) коммит не
помечает: опрос повторит сборку сам, до `SETEVYKH_POPYTOK` раз.

Все git-команды идут с `-c safe.directory=<чекаут>`. Каталог на боевом сервере
принадлежит человеку, который его клонировал, а обновлятор запускают то от него,
то от root через `sudo` — и тогда git отвечает `detected dubious ownership` и
отказывается работать вовсе. Это защита от чужого репозитория в общем каталоге;
здесь каталог свой и назван явно, поэтому исключение точечное, а не `git config
--global` на всю машину.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from deploy import kesh, notify, otchyot, vyzhimka
from deploy.config import UpdateConfig
from deploy.github import (
    CHECKS_FAILURE,
    CHECKS_PENDING,
    CHECKS_SUCCESS,
    GitHub,
    GitHubError,
    chas,
)
# _atomic_write, а не своя запись: гарантия ровно та же, что у состояния демона —
# недописанного JSON на диске не бывает. Ход обновления читают в произвольный
# момент, и половина файла разобралась бы у страницы как порча.
from deploy.journal import Journal, _atomic_write
from deploy.runner import HttpProbe, Result, Shell

try:
    import fcntl
except ImportError:  # Windows разработчика: замка нет, обновлятор там не живёт
    fcntl = None

STATUS_DISABLED = "disabled"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_DEPLOYED = "deployed"
STATUS_ABORTED = "aborted"  # остановились до подмены — живой сайт не трогали
STATUS_WAITING = "waiting-checks"  # коммит хороший, но CI ещё не досчитал
STATUS_ROLLED_BACK = "rolled-back"
STATUS_BROKEN = "broken"  # откат тоже не поднялся
#: Лимит GitHub исчерпан — круг пропущен, сети не касались.
#:
#: Отдельный исход, а не разновидность `aborted`, потому что отвечает на
#: другой вопрос. `aborted` значит «попробовали и не смогли», а здесь мы даже
#: не пробовали: до названного часа ответом будет тот же отказ. Смешай их — и
#: в логе не отличить «GitHub молчит» от «мы сами решили подождать».
STATUS_RATE_LIMITED = "rate-limited"
#: Идёт другое обслуживание — второе обновление, ночная копия, копия или
#: восстановление с экрана. Круг пропущен, ничего не тронуто.
STATUS_BUSY = "busy"

#: Замок обслуживания в каталоге состояния. `flock`: снимается сам, когда процесс
#: умер. Его же берёт `./opencrm.sh backup` и `restore` (docs/08, «Одно обслуживание за раз»).
ZAMOK_OBSLUZHIVANIYA = ".zamok-obsluzhivaniya"
#: Метка обновления в замке копий приложения (`backup_service._zanyat`, тот же файл).
METKA_OBNOVLENIYA = "obnovlenie"
#: Как у `backup_service.USTAREL_SEKUND`: работа старше — брошенная.
KOPIYA_USTARELA_SEKUND = 2 * 3600

#: Имя проекта compose для набора тестов. Флагом `-p`, а не строкой `name:` в
#: файле: `COMPOSE_PROJECT_NAME` перебивает вторую, и уборка после набора
#: (`down -v --remove-orphans`) сносит боевой стек установки вместе с томами.
PROEKT_NABORA = "opencrm-tests"

#: Исходы, о которых демон молчит: они означают «ничего не случилось», а
#: строка о том, что ничего не случилось, повторённая раз в пять минут,
#: прячет ту единственную, ради которой лог и читают.
QUIET = {STATUS_DISABLED, STATUS_UP_TO_DATE, STATUS_RATE_LIMITED}

#: Как часто в паузе между опросами смотреть, не попросили ли почистить кэш.
ZAPROS_SHAG = 15
#: Замер кэша для экрана — не чаще раза в полчаса.
ZAMER_KAZHDYE = 1800

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

# Метка конца дампа MySQL. Ровно та же строка, что пишет и проверяет
# `scripts/snapshot_db.py` (`METKA`), и повторена она здесь НАРОЧНО: пакет
# `deploy/` работает на хосте и не имеет права импортировать код приложения
# (см. шапку пакета — ни `core`, ни `web`, ни `scripts`). Чтобы удвоение не
# разъехалось молча, за совпадением следит `tests/test_pre_migrate_snapshot.py`.
SNAPSHOT_MARK = "-- opencrm snapshot complete"

# Сколько хвоста читаем в поисках метки. Дамп — гигабайты; метка идёт последней
# строкой, и читать ради неё весь файл незачем.
SNAPSHOT_TAIL_BYTES = 4096

#: Сколько копий базы держим в каталоге обновлений — КАЖДОГО вида отдельно.
#:
#: То есть на диске их до `2 * SNAPSHOTS_KEPT`: столько же `pre-update-*`,
#: сколько `failed-update-*`. Сказано отдельной строкой, потому что «держим
#: пять копий» и «держим пять копий каждого вида» при дампе в гигабайт с лишним
#: отличаются на шесть гигабайт, и в первом прочтении число выглядит безопасным.
#:
#: Два, а не пять. Считаем: `min_free_mb` требует свободных 2 ГБ, точка входа
#: держит рядом свои пять копий (`docker/entrypoint.sh`), а дамп боевой базы —
#: около 1,2 ГБ. Пять на вид дало бы десять дампов, двенадцать гигабайт, и
#: правка, написанная против переполнения диска, сама бы его и устроила.
#:
#: Два — это «есть куда вернуться и есть с чем сравнить»: к копии возвращаются в
#: первые часы после неудачного обновления, а не через неделю, и двух неудач
#: подряд для разбора хватает. Меньше нельзя: одна копия означает, что вторая
#: неудача стирает улику первой.
SNAPSHOTS_KEPT = 2

#: Отказ сети, а не коммита: реестр образов, пакетов или GitHub не ответил.
#: 29.09.2026 Docker Hub не ответил на TLS-рукопожатие, и исправный коммит был
#: записан в «не встал» — сам он больше не повторился бы никогда.
SETEVOY_OTKAZ = re.compile(
    r"TLS handshake timeout|i/o timeout|connection reset by peer|connection timed out"
    r"|Temporary failure in name resolution|no such host|Could not resolve host"
    r"|Failed to connect to|failed to fetch anonymous token|toomanyrequests"
    r"|context deadline exceeded|unexpected EOF|The remote end hung up|early EOF"
    r"|ETIMEDOUT|ECONNRESET|EAI_AGAIN|ENOTFOUND|ReadTimeoutError|Max retries exceeded"
    r"|NewConnectionError|502 Bad Gateway|503 Service Unavailable|504 Gateway",
    re.I,
)

#: Сколько сетевых отказов подряд на одном коммите терпим, прежде чем записать
#: его в «не встал» как обычно: 24 опроса по пять минут — два часа.
SETEVYKH_POPYTOK = 24

#: Тег, под которым до сборки нового кода сберегается образ работающего
#: приложения: откат поднимает ЕГО, а не пересобирает старый код заново.
OBRAZ_OTKATA = "opencrm-otkat:latest"


#: Строка, которой дампер открывает каждую таблицу.
#:
#: По ней и только по ней видно, какие таблицы копия ЗНАЕТ. Разбор текстовый
#: нарочно: пакет `deploy/` работает на хосте, драйвера базы у него нет и не
#: будет — по той же причине, по которой и сам дамп снимается заходом в
#: контейнер (разбор — в шапке `scripts/snapshot_db.py`).
_DROP = re.compile(r"^DROP TABLE IF EXISTS `([^`]+)`;", re.M)

#: Имя таблицы, которое не станет неожиданностью в командной строке.
_PROSTOE_IMYA = re.compile(r"[A-Za-z0-9_]+")

#: Обратная кавычка и экран для неё — по отдельности, чтобы не путались в
#: f-строках с кавычками shell'а.
BS = chr(92)
BT = chr(96)


def _tablicy_kopii(snapshot: Path) -> set[str]:
    """Имена таблиц, которые копия знает. Пусто — прочитать не вышло.

    **Строка за строкой, а не файл целиком.** На боевой базе дамп — гигабайт с
    лишним, и в нём кириллица: `read_text` развернул бы его в строку Python по
    два байта на знак, то есть под два с половиной гигабайта, да ещё со
    склейкой кусков при чтении. На VPS с двумя-четырьмя гигабайтами памяти это
    `MemoryError` — и прилетел бы он в САМОМ неудачном месте: посреди отката,
    сразу после заливки базы и ДО того, как контейнер подняли обратно. Сайт
    остался бы лежать, страница обслуживания — навсегда показывать «идёт», а
    демон через пять минут взялся бы за тот же коммит снова.

    Ровно так же и по той же причине читает дампер (`scripts/snapshot_db.py`):
    «прочитанная в список она занимает гигабайты».

    Беду чтения глотаем и возвращаем пустое множество: у вызывающего пустота
    уже значит «сносить нечего», и это верный ответ. Вернуть рабочую базу
    важнее, чем прибрать за упавшей миграцией.
    """
    nashli: set[str] = set()
    try:
        with snapshot.open(encoding="utf-8", errors="replace") as fayl:
            for stroka in fayl:
                # `DROP TABLE IF EXISTS` дампер пишет одной строкой на таблицу —
                # значит построчного разбора хватает, а `re.M` по всему файлу
                # больше не нужен.
                sovpalo = _DROP.match(stroka)
                if sovpalo:
                    nashli.add(sovpalo.group(1))
    except (OSError, MemoryError, ValueError) as beda:
        # Логировать отсюда нечем — функция не знает про журнал. Пустота скажет
        # за неё, а вызывающий про пустоту говорит вслух.
        del beda
        return set()
    return nashli


_REVIZIYA_KOPII = re.compile(r"^\('([0-9a-f]+)'\)")


def _reviziya_kopii(put: Path) -> str:
    """Ревизия alembic в копии или пусто. Построчно: копия бывает в гигабайт."""
    try:
        with put.open(encoding="utf-8", errors="replace") as f:
            for stroka in f:
                if stroka.startswith("INSERT INTO `alembic_version`"):
                    naydeno = _REVIZIYA_KOPII.match(next(f, ""))
                    return naydeno.group(1) if naydeno else ""
    except OSError:
        return ""
    return ""


def _celaya(put: Path) -> bool:
    """Дочитана ли копия до метки конца.

    Оборванный дамп (кончилось место, убили контейнер) — это обычный текстовый
    файл, и негодность у него не видна ничем, кроме отсутствующего хвоста. Ровно
    этого не хватало резервным копиям, пока их никто не читал.
    """
    try:
        razmer = put.stat().st_size
        if not razmer:
            return False
        with put.open("rb") as f:
            f.seek(max(0, razmer - SNAPSHOT_TAIL_BYTES))
            hvost = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return SNAPSHOT_MARK in hvost


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
    #: Итог ужатия кэша сборки после удачного обновления — строка в уведомлении.
    kesh: dict | None = None

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
        # Журнал копится в памяти РАДИ ОТЧЁТА: `log` сегодня только отдаёт строку
        # наружу и нигде её не держит, а отчёт о неудаче без вырезок из журнала
        # — это та же строчка «не поднялось», ради замены которой он и заведён.
        #
        # Кольцом на `STROK_ZHURNALA * 3`, а не списком без предела: обновление
        # с застрявшей сборкой пишет тысячи строк, и держать их все в процессе,
        # который живёт на боевой машине, незачем. Тройной запас — чтобы в отчёт
        # попал именно хвост, а не всё подряд.
        self._zhurnal: list[str] = []
        vneshniy = log or (lambda _message: None)

        def zapomnit(soobshchenie: str) -> None:
            self._zhurnal.append(str(soobshchenie))
            if len(self._zhurnal) > otchyot.STROK_ZHURNALA * 3:
                del self._zhurnal[: len(self._zhurnal) - otchyot.STROK_ZHURNALA * 3]
            vneshniy(soobshchenie)

        self.log = zapomnit
        self.journal = journal or Journal(config.state_file, config.history_file)
        self.github = github or GitHub(config.repo, config.github_token)
        self.shell = shell or Shell(log=self.log)
        self.probe = probe or HttpProbe()
        self.notifier = notifier if notifier is not None else notify.from_config(config)
        self._sleep = sleep
        self._nachalo_obnovleniya = 0.0
        self._obraz_otkata = ""
        self._clock = clock
        # Последний объявленный посетителю шаг и время начала обновления.
        # Пустые до первого объявления — по этому и видно, что показывать пока
        # нечего (см. `_progress_finish`).
        self._progress_step = ""
        self._progress_started = ""
        # Почему smoke-тесты не спрашивали. Заводим здесь, а не только в
        # `wait_healthy`: читает это поле `_health`, и отсутствующий атрибут
        # уронил бы отчёт о совершенно исправной выкатке.
        self._smoke_propushchen = ""
        self._kesh_zameren: float | None = None

    # --- то, что вызывают снаружи ---

    def status(self, fresh: bool = True) -> dict:
        """Что развёрнуто, что в ветке и чем кончился прошлый заход.

        **`fresh=False` не спрашивает GitHub вовсе** — отвечает тем, что
        запомнено с прошлого опроса. Заведено не ради скорости: живое меню
        обновляет шапку раз в пятнадцать секунд, и безусловный запрос из неё
        давал 240 обращений в час при лимите GitHub в 60 для анонимного
        клиента. Дальше 403 — и он ломал не только шапку, а настоящее
        обновление, потому что лимит один на весь IP. Снято с боевого сервера.

        **`fresh=True` спрашивает УСЛОВНО**, с прошлым ETag. Раньше здесь стояло
        «без ETag: `status` спрашивают, чтобы узнать правду, а не сэкономить
        запрос» — но правда и экономия тут не спорят: на 304 голова ветки та же,
        что запомнена, и ответ выходит тем же самым, только дешевле.

        ETag у `status` СВОЙ (`status_etag`), и это главное здесь. Возьми он
        общий с `run_once` — и первый же `status` съел бы изменение: запомнил
        новый ETag, не выкатив коммит, а демон следом получил бы 304 и решил,
        что обновлять нечего. Автообновление встало бы намертво, и виновата
        была бы команда, которая ничего не меняет.
        """
        state = self.journal.read()
        current = self.head_sha()
        available = str(state.get("available_sha") or "")
        provereno = float(state.get("available_at") or 0)
        error = ""
        checks = None
        if fresh:
            try:
                head = self.github.head(self.config.branch, str(state.get("status_etag") or ""))
                provereno = time.time()
                if head.changed:
                    available = head.sha
                    self.journal.write(
                        status_etag=head.etag,
                        available_sha=head.sha,
                        available_at=provereno,
                    )
                else:
                    self.journal.write(available_at=provereno)
                # Почему сайт не обновляется — вопрос, который задают этой
                # команде. Без строки о проверках «обновление: есть» и тишина
                # выглядят поломкой, хотя это гейт штатно ждёт зелёного CI.
                if available and available != current and self.config.require_ci:
                    checks = self.github.checks(available)
            except GitHubError as failure:
                error = str(failure)
                # Час сброса запоминаем и здесь: человек спросил `status`,
                # напоролся на лимит — демону незачем узнавать то же самое
                # заново собственным отказом.
                if getattr(failure, "do_kogda", 0):
                    self.journal.write(github_do=failure.do_kogda)
        return {
            "repo": self.config.repo,
            "branch": self.config.branch,
            "deployed": current,
            "available": available,
            "available_at": provereno,
            "update_available": bool(available and available != current),
            "autoupdate": self.journal.autoupdate_enabled,
            "failed_sha": state.get("failed_sha", ""),
            "checks": checks.state if checks else "",
            "checks_detail": checks.detail if checks else "",
            "github_error": error,
            "github_wait": float(self.journal.read().get("github_do") or 0),
            "last": self.journal.last(),
        }

    def run_once(self, force: bool = False) -> Outcome:
        # `force-update` из консоли — второй процесс рядом со службой: без замка два
        # деплоя шли разом, каждый со своим снимком, миграциями и откатом (разбор 28.09.2026).
        with self._zamok_obsluzhivaniya() as svoboden:
            if not svoboden:
                return Outcome(
                    STATUS_BUSY, from_sha=self.head_sha(),
                    reason="идёт другое обслуживание (обновление или копия) — попробую следующим кругом",
                )
            return self._run_once(force)

    @contextmanager
    def _zamok_obsluzhivaniya(self):
        """`True` — замок наш. Замок, который не открылся, обновлений не останавливает:
        иначе починить обновлятор было бы нечем, кроме рук на сервере."""
        if fcntl is None:
            yield True
            return
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            fayl = open(self.config.state_dir / ZAMOK_OBSLUZHIVANIYA, "a")
        except OSError as beda:
            self.log(f"замок обслуживания не открылся ({beda!r}) — работаю без него")
            yield True
            return
        try:
            try:
                fcntl.flock(fayl, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                svoboden = False
            except OSError as beda:
                self.log(f"замок обслуживания не взялся ({beda!r}) — работаю без него")
                svoboden = True
            else:
                svoboden = True
            yield svoboden
        finally:
            fayl.close()

    def _zanyat_kopii(self) -> bool | None:
        """Занять замок копий приложения на время обновления.

        Подмена контейнера обрывает копию или восстановление с экрана на середине, а
        полузалитая база — худшее, что может остаться. `False` — там идёт работа с
        экрана; `None` — замка не завести, обновлению это не мешает.
        """
        put = self.config.data_dir / "backups" / "sayt" / "zanyato"
        for _popytka in range(2):
            try:
                put.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(put, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    chey = put.read_text(encoding="utf-8").strip()
                    vozrast = time.time() - put.stat().st_mtime
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                if chey != METKA_OBNOVLENIYA and 0 <= vozrast < KOPIYA_USTARELA_SEKUND:
                    return False
                # Своя метка от оборванного обновления или брошенная работа.
                put.unlink(missing_ok=True)
                continue
            except OSError:
                return None
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(METKA_OBNOVLENIYA)
            return True
        return None

    def _osvobodit_kopii(self) -> None:
        put = self.config.data_dir / "backups" / "sayt" / "zanyato"
        try:
            if put.read_text(encoding="utf-8").strip() == METKA_OBNOVLENIYA:
                put.unlink(missing_ok=True)
        except OSError:
            pass

    def _run_once(self, force: bool) -> Outcome:
        if not force and not self.journal.autoupdate_enabled:
            return Outcome(STATUS_DISABLED, from_sha=self.head_sha())

        # Пока лимит GitHub исчерпан, спрашивать нечего: ответом будет тот же
        # 403. Молча пропускаем круг — иначе демон писал бы одну и ту же строку
        # каждые пять минут целый час, и в ней утонул бы тот, кто пришёл
        # разбираться, почему сайт не обновился. Момент сброса называет сам
        # GitHub; `force` проходит всегда — человек имеет право попробовать.
        zhdyom = float(self.journal.read().get("github_do") or 0)
        if not force and zhdyom > self._clock():
            return Outcome(STATUS_RATE_LIMITED, reason=_do_kogda(zhdyom))

        try:
            head = self.github.head(self.config.branch, "" if force else self.journal.etag)
        except GitHubError as failure:
            # Сетевые сбои в историю не пишем: при опросе раз в пять минут они
            # затопили бы журнал. Видно их в логе демона и в `status`.
            self.log(f"опрос не удался: {failure}")
            if getattr(failure, "do_kogda", 0):
                self.journal.write(github_do=failure.do_kogda)
            return Outcome(STATUS_ABORTED, reason=str(failure))
        # Спросили и ответили — значит дверь снова открыта.
        if zhdyom:
            self.journal.write(github_do=0)

        if not head.changed:
            return Outcome(STATUS_UP_TO_DATE, from_sha=self.head_sha())
        # Голову ветки запоминаем ЗДЕСЬ же, а не только в `status`: демон
        # опрашивает GitHub раз в пять минут, и живому меню этого хватает без
        # единого собственного запроса. Иначе шапка показывала бы «—» до тех
        # пор, пока человек сам не спросит `status`, — то есть ровно до того
        # момента, когда она уже не нужна.
        self.journal.write(etag=head.etag, available_sha=head.sha, available_at=time.time())

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
        # Откат залил базу не до конца: следующее обновление сняло бы копию с неё,
        # а уборка через два обновления удалила бы последнюю хорошую (29.09.2026).
        porvana = self.journal.read().get("baza_porvana")
        if porvana and not force:
            return Outcome(
                STATUS_ABORTED,
                from_sha=current,
                to_sha=head.sha,
                reason=f"откат залил базу не до конца ({porvana}) — автообновление ждёт человека: "
                "восстановите копию и дайте force-update",
            )

        gate = self._ci_gate(current, head.sha, force)
        if gate is not None:
            return gate

        zanyal = self._zanyat_kopii()
        if zanyal is False:
            # Метку забываем, как у ждущего CI: иначе следующий опрос ответил бы
            # «не изменилось», и коммит ждал бы следующего.
            self.journal.write(etag="")
            return Outcome(
                STATUS_BUSY, current, head.sha,
                reason="идёт копия или восстановление с экрана — обновление подождёт",
            )
        try:
            # Гейт выше пропускает дальше ТОЛЬКО зелёный CI — значит на обычном
            # пути спрашивать GitHub второй раз незачем, ответ уже есть.
            return self._deploy(
                current, head.sha, ci_zelyonye=not force and self.config.require_ci
            )
        finally:
            if zanyal:
                self._osvobodit_kopii()

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
                self.zamerit_kesh()
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
                self._podozhdat(self.config.poll_seconds)

    def _podozhdat(self, sekund: float) -> None:
        """Пауза между опросами ломтиками: просьба почистить кэш с экрана не ждёт пять минут."""
        ostalos = sekund
        while ostalos > 0:
            lomtik = min(ZAPROS_SHAG, ostalos)
            self._sleep(lomtik)
            ostalos -= lomtik
            try:
                # Под замком: чистка посреди сборки `force-update` снесла бы её слои.
                with self._zamok_obsluzhivaniya() as svoboden:
                    if svoboden:
                        self._prosba_s_ekrana()
            except Exception as failure:  # noqa: BLE001 — демон не имеет права упасть
                self.log(f"чистка кэша по просьбе не удалась: {failure!r}")

    # --- кэш сборки (docs/ekspluatatsiya/08-razvyortyvanie.md, «Кэш сборки») ---

    def _pochistit_kesh(self, steps: list[Step], *, vsyo: bool, kak: str, kto: str = "") -> dict:
        """Почистить кэш сборки и оставить итог там, где его прочтёт экран «Обслуживание»."""
        bylo = kesh.izmerit(self.shell, self.config.project_dir) or {}
        rezultat = None
        for argv in kesh.komandy(vsyo, self.config.cache_keep_gb):
            rezultat = self.shell.run(argv, cwd=self.config.project_dir, timeout=900)
            if rezultat.ok or not kesh.ne_znaet_flaga(rezultat):
                break
        self._step(steps, "cache", rezultat, fatal=False)
        stalo = kesh.izmerit(self.shell, self.config.project_dir) or {}
        osvobozhdeno = None
        if bylo.get("razmer") is not None and stalo.get("razmer") is not None:
            osvobozhdeno = max(0, bylo["razmer"] - stalo["razmer"])
        svobodno = self._svobodno_mb(self.config.data_dir)
        itog = {
            "at": _utc_now(),
            "kak": kak,
            "kto": kto,
            "ok": rezultat.ok,
            "oshibka": "" if rezultat.ok else (rezultat.err or rezultat.out).strip()[-300:],
            "osvobozhdeno": osvobozhdeno,
            "razmer": stalo.get("razmer"),
            "svobodno_mb": svobodno,
        }
        self._zapisat_kesh(stalo, svobodno, poslednyaya=itog)
        return itog

    def _zapisat_kesh(self, zamer: dict, svobodno_mb: int | None, poslednyaya: dict | None = None) -> None:
        put = kesh.papka(self.config.data_dir) / kesh.SOSTOYANIE
        bylo = kesh.prochest(put) or {}
        try:
            kesh.zapisat(self.config.data_dir, {
                "izmereno": _utc_now(),
                "razmer": zamer.get("razmer"),
                "osvobodimo": zamer.get("osvobodimo"),
                "svobodno_mb": svobodno_mb,
                "ostavlyat_gb": self.config.cache_keep_gb,
                "poslednyaya": poslednyaya or bylo.get("poslednyaya"),
            })
        except OSError as beda:
            self.log(f"состояние кэша не записалось: {beda}")

    def _prosba_s_ekrana(self) -> None:
        """Просьба «очистить кэш» с экрана настроек: чистим всё и сообщаем в бот."""
        put = kesh.papka(self.config.data_dir) / kesh.ZAPROS
        if not put.exists():
            return
        prosba = kesh.prochest(put) or {}
        kto = str(prosba.get("kto") or "")[:120]
        # Сначала убрать просьбу: упади чистка — второй раз по кругу она не пойдёт.
        put.unlink(missing_ok=True)
        steps: list[Step] = []
        itog = self._pochistit_kesh(steps, vsyo=True, kak="vruchnuyu", kto=kto)
        self.log(f"кэш сборки очищен по просьбе {kto or 'с экрана'}: {kesh.chelovecheski(itog['osvobozhdeno'])}")
        self.notifier.send(self._stroka_kesha(itog, zagolovok=True), tiho=itog["ok"])

    def _stroka_kesha(self, itog: dict, *, zagolovok: bool = False) -> str:
        """Одна строка о кэше — в сообщении об обновлении и в ответе на просьбу с экрана."""
        e = notify.ekranirovat
        if not itog.get("ok"):
            return f"✗ <b>Кэш сборки не очистился</b>: {e(itog.get('oshibka') or 'docker отказал')}"
        chasti = [f"освобождено {kesh.chelovecheski(itog.get('osvobozhdeno'))}",
                  f"осталось {kesh.chelovecheski(itog.get('razmer'))}"]
        if itog.get("svobodno_mb") is not None:
            chasti.append(f"на диске свободно {kesh.chelovecheski(itog['svobodno_mb'] * 1024 * 1024)}")
        stroka = "🧹 <b>Кэш сборки</b>: " + " · ".join(chasti)
        if zagolovok:
            kto = f", {e(itog['kto'])}" if itog.get("kto") else ""
            stroka = f"🧹 <b>Кэш сборки очищен вручную</b>{kto}\n" + " · ".join(chasti)
        return stroka

    def zamerit_kesh(self) -> None:
        """Свежий замер для экрана, не чаще `ZAMER_KAZHDYE` — `docker system df` не бесплатен."""
        teper = self._clock()
        if self._kesh_zameren is not None and teper - self._kesh_zameren < ZAMER_KAZHDYE:
            return
        self._kesh_zameren = teper
        zamer = kesh.izmerit(self.shell, self.config.project_dir)
        if zamer is not None:
            self._zapisat_kesh(zamer, self._svobodno_mb(self.config.data_dir))

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

    def _checkout_args(self, target: str) -> tuple[str, ...]:
        # `--force` только когда грязь — одни переводы строк: без него git
        # отказал бы «local changes would be overwritten» ровно на том файле,
        # который новая версия и чинит.
        if getattr(self, "_tolko_perevody_strok", False):
            return ("checkout", "--force", "--detach", "--quiet", target)
        return ("checkout", "--detach", "--quiet", target)

    # --- сам деплой ---

    def _deploy(self, previous: str, target: str, ci_zelyonye: bool = False) -> Outcome:
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

        povtor_seti: bool | None = None
        self._nachalo_obnovleniya = time.time()
        try:
            # Уборка копий — ПЕРВЫМ делом, ДО проверки свободного места.
            #
            # Стояла она в конце съёмки копии, и это отнимало у неё главный
            # смысл: `_preflight` отказывает по `min_free_mb` РАНЬШЕ, чем
            # `_snapshot` вообще начинается. На машине, которую старые дампы уже
            # забили, обновление вставало на preflight, уборка не звалась
            # никогда, и диск не освобождался сам — то есть заклиненное
            # состояние, ради которого правка написана, ею же и не лечилось.
            #
            # Прежний довод («удали мы лишнее до съёмки и сорвись съёмка —
            # остались бы совсем без копии») был ложным: уборка всегда оставляет
            # `SNAPSHOTS_KEPT` копий, когда бы её ни позвали, и до съёмки она
            # удалила бы только лишнюю с конца.
            self._pribrat_snimki()
            self._preflight(steps)
            self._step(steps, "fetch", self._git("fetch", "--quiet", "origin", self.config.branch))
            self._step(steps, "checkout", self._git(*self._checkout_args(target)))
            self._checks(steps, target, ci_zelyonye)
            self._config_check(steps)
            # Шаги объявляются ДО работы, а не после: страницу читают ровно в ту
            # минуту, когда шаг идёт, а «сделано» посетителю уже неинтересно.
            self._progress("backup")
            snapshot = self._snapshot(steps, target)

            touched = True
            self._progress("build")
            self._step(
                steps,
                "deploy",
                self._compose(
                    "up", "-d", "--build", "--remove-orphans",
                    timeout=self.config.build_timeout,
                ),
            )
            migrated = True
            self._peresobrat_izmenyonnye(steps, previous, target)
            self._reload_nginx(steps)
            self._progress("health")
            self._health(steps, "health")

            if self._obraz_otkata:
                self.shell.run(["docker", "image", "rm", OBRAZ_OTKATA], cwd=self.config.project_dir, timeout=60)
            # Каждая сборка оставляет предыдущий образ висеть без тега. Демон
            # работает без присмотра месяцами, и забитый диск сломал бы не только
            # обновления, но и загрузку файлов. `prune` трогает только dangling.
            self._step(steps, "prune", self.shell.run(
                ["docker", "image", "prune", "-f"], cwd=self.config.project_dir, timeout=300,
            ), fatal=False)
            # Кэш сборки — тоже: 28.09.2026 он занимал 45 ГБ при 21 ГБ свободных.
            itog_kesha = self._pochistit_kesh(steps, vsyo=False, kak="avto")

            outcome = Outcome(STATUS_DEPLOYED, previous, target, summary, "", steps, kesh=itog_kesha)
            self.journal.write(deployed_sha=target, failed_sha="", baza_porvana="", setevaya_sha="", setevykh_popytok=0)
        except _Stop as stop:
            if not touched:
                # Живой сайт не трогали — достаточно вернуть чекаут на место.
                # Пустой `previous` бывает, когда каталог вообще не репозиторий:
                # `git rev-parse HEAD` промолчал, и возвращаться некуда — команда
                # с пустым аргументом только мусорила бы в логе.
                if previous:
                    self._step(
                        steps, "restore-checkout",
                        self._git(*self._checkout_args(previous)), fatal=False,
                    )
                outcome = Outcome(STATUS_ABORTED, previous, target, summary, str(stop), steps)
            else:
                broken = self._rollback(steps, previous, snapshot, restore_db=migrated)
                status = STATUS_BROKEN if broken else STATUS_ROLLED_BACK
                outcome = Outcome(status, previous, target, summary, str(stop), steps)
            povtor_seti = self._setevoy_otkaz(stop, steps, target, touched)
            if povtor_seti is None:
                self.journal.write(failed_sha=target, setevaya_sha="", setevykh_popytok=0)

        outcome.seconds = self._clock() - started
        self._progress_finish(outcome, touched)
        if not povtor_seti:
            self.journal.append(outcome.as_record())
            self._notify(outcome)
        return outcome

    def _setevoy_otkaz(self, stop: Exception, steps: list[Step], target: str, touched: bool) -> bool | None:
        """Отказ сети до подмены: коммит не помечается «не встал», опрос повторит.

        None — отказ не сетевой (или сеть не встаёт уже два часа): решает обычный
        `failed_sha`. Иначе — повтор ли это того же сетевого отказа: о повторе не
        звоним и не пишем в историю, иначе каждые пять минут приходило бы то же.
        """
        tekst = " ".join([str(stop)] + [s.detail for s in steps if not s.ok])
        if touched or not SETEVOY_OTKAZ.search(tekst):
            return None
        bylo = self.journal.read()
        popytok = int(bylo.get("setevykh_popytok") or 0) + 1 if bylo.get("setevaya_sha") == target else 1
        if popytok > SETEVYKH_POPYTOK:
            return None
        # Метку опроса забываем, как у ждущего CI: иначе следующий опрос ответил
        # бы «не изменилось», и коммит ждал бы следующего.
        self.journal.write(etag="", setevaya_sha=target, setevykh_popytok=popytok)
        return popytok > 1

    def _gryaz_tolko_v_perevodakh_strok(self, porcelain: str) -> bool:
        """Все правки — отслеживаемые файлы, и без CR в конце строк их нет."""
        for stroka in porcelain.splitlines():
            if not stroka.strip():
                continue
            # `??` — лишний файл, ` M` — правка рабочей копии; всё прочее
            # (индекс, переименование, удаление) — настоящее.
            if stroka[:2] != " M":
                return False
        return self._git("diff", "--quiet", "--ignore-cr-at-eol").ok

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
        # Правки только в переводах строк — не правки. Блоб со смешанными CRLF/LF
        # (`*.py text`) на Linux «грязный» сразу после чекаута, и `checkout -- .`
        # это не лечит: чекаут пишет тот же блоб. Содержимого в такой правке нет,
        # значит и терять нечего — идём дальше, а чекаут делаем с `--force`.
        self._tolko_perevody_strok = bool(dirty.out.strip()) and self._gryaz_tolko_v_perevodakh_strok(dirty.out)
        if self._tolko_perevody_strok:
            steps.append(Step("preflight", True, "правки только в переводах строк — содержимое не тронуто"))
        elif dirty.out.strip() and not self.config.allow_dirty:
            steps.append(Step("preflight", False, dirty.out.strip()[:400]))
            # Отказ обязан назвать лекарство. Без него человек знает только, что
            # обновление не идёт, — и дальше либо ждёт, либо сносит каталог
            # целиком. Правку правил руками он же, значит и решение стереть её
            # принимать ему; наше дело — сказать, чем именно стирается.
            #
            # `checkout -- .` возвращает только ОТСЛЕЖИВАЕМОЕ. `git clean` тут
            # не назван намеренно: он снёс бы и то, чего в репозитории нет, а
            # у части установок в этом же каталоге лежат данные сайта.
            raise _Stop(
                "в рабочем дереве есть несохранённые правки — обновление затёрло бы их. "
                f"Стереть свои правки: git -C {self.config.project_dir} checkout -- . "
                "(и удалить лишние файлы, если они перечислены выше)"
            )

        # Место на диске — здесь, до первой записи.
        #
        # Обновление пишет трижды: копия базы (на боевом объёме гигабайт с
        # лишним), слои нового образа, образ ворот. Старый образ лежит до
        # `prune`, значит на пике их два. Кончившееся место — не гипотеза: образ
        # панели мониторинга разом вырос с 744 МБ до 1.16 ГБ.
        #
        # Отказ по месту дёшев ровно до первой записи и очень дорог посреди
        # дампа: оборванный дамп — обычный текстовый файл, и негодность у него
        # не видна ничем, кроме отсутствующего хвоста. Такую копию однажды уже
        # заливали «успешно» (см. `scripts/restore.sh`).
        #
        # Смотрим на раздел с ДАННЫМИ, а не на чекаут: копия базы и слои
        # docker'а живут там, и на VPS это часто разные разделы.
        svobodno = self._svobodno_mb(self.config.data_dir)
        nuzhno = self._nuzhno_mesta_mb()
        if svobodno is not None and svobodno < nuzhno:
            steps.append(Step("preflight", False, f"свободно {svobodno} МБ"))
            raise _Stop(
                f"на диске свободно {svobodno} МБ, нужно хотя бы "
                f"{nuzhno}. Освободить: docker image prune -f"
            )
        steps.append(Step("preflight", True))

    def _nuzhno_mesta_mb(self) -> int:
        """Сколько места нужно: не меньше трёх копий базы и гигабайта на слои образа.

        Обновление с миграциями пишет копию до подмены и копию в точке входа, откат —
        ещё отложенную неудачную. Порог в 2 ГБ при базе в 1,2 ГБ проходил, а дамп
        точки входа падал на ENOSPC уже после подмены (разбор 29.09.2026).
        """
        try:
            kopii = [p.stat().st_size for p in self.config.state_dir.glob("pre-update-*.sql")]
        except OSError:
            kopii = []
        baza_mb = max(kopii, default=0) // (1024 * 1024)
        return max(self.config.min_free_mb, 3 * baza_mb + 1024)

    def _svobodno_mb(self, put: Path) -> int | None:
        """Свободные мегабайты на разделе. `None` — спросить не удалось.

        `None` не считается отказом: не сумев узнать, обновление продолжает
        работать как раньше. Проверка места — страховка, и превращать её в
        новый способ заклинить обновления было бы хуже той беды, от которой она
        заведена.
        """
        try:
            return shutil.disk_usage(put).free // (1024 * 1024)
        except OSError:
            return None

    def _checks(self, steps: list[Step], target: str, ci_zelyonye: bool = False) -> None:
        """Тесты нового кода — до того, как живой сайт тронут.

        Гоняются в контейнере, а не на хосте: на боевом сервере нет ни venv
        проекта, ни pytest, зато есть docker — тот же самый, которым через
        минуту собирается боевой образ.

        Прежде это была одна строка — `docker build --target tests`, — и pytest
        шёл прямо в слое сборки. Так больше нельзя: набор гоняется против
        настоящей MySQL, а во время сборки сети между контейнерами не
        существует вовсе, сервера базы образу не видно.

        Отсюда отдельный compose-файл. Он поднимает СВОЮ базу — не боевую: своё
        имя проекта, своя сеть, данные в памяти, и после прогона не остаётся
        ничего.

        **Имя проекта задаётся флагом `-p`, а не строкой `name:` в файле.**
        Первое сильнее: `COMPOSE_PROJECT_NAME` из окружения или из `docker/.env`
        перебивает `name:` и складывает набор с БОЕВЫМ стеком в один проект. А
        уборка ниже — `down -v --remove-orphans`: под общим именем она сносит
        живые контейнеры установки и её тома. Поймано на стенде, где имя проекта
        задано ради трёх стеков на одной машине: обновление снесло собственное
        приложение и на копии базы встало.

        `--exit-code-from tests` — чтобы код возврата был кодом pytest, а не
        компоуза: иначе красный набор проехал бы дальше зелёным. `--build` тут
        законен (в отличие от `run --build`, см. `_config_check`): у `up` этот
        флаг был всегда.

        **И всё это не запускается, когда тот же набор на том же коммите уже
        прошёл в CI.** Прогон здесь — та же команда на тех же исходниках, только
        на боевой машине, где два прохода набора и два сервера базы делят
        пару ядер с работающим сайтом. Цена вышла наружу 21.09.2026: обновление
        уткнулось в получасовой потолок и не состоялось вовсе, хотя проверки
        GitHub на этом коммите были зелёными. Остаётся прогон там, где спросить
        не у кого: CI отключён, GitHub недоступен или проверок на коммите нет.
        """
        if not self.config.run_checks:
            steps.append(Step("tests", True, "пропущены (OPENCRM_UPDATE_RUN_CHECKS=0)"))
            return
        zelyonye = "зелёный в CI" if ci_zelyonye else self._ci_zelyonye(target)
        if zelyonye is not None:
            steps.append(Step("tests", True, f"не гоняли: тот же набор {zelyonye}"))
            self.log(f"tests: не гоняли, тот же набор {zelyonye}")
            return
        compose_tests = self.config.project_dir / "docker" / "docker-compose.tests.yml"
        result = self.shell.run(
            [
                "docker", "compose", "-p", PROEKT_NABORA, "-f", str(compose_tests),
                "up", "--build", "--abort-on-container-exit", "--exit-code-from", "tests",
            ],
            cwd=self.config.project_dir,
            timeout=self.config.checks_timeout,
        )
        # Убрать за собой надо в любом исходе: `up` оставляет остановленные
        # контейнеры и том tmpfs, и следующий прогон начался бы на чужих
        # остатках. Код возврата уборки не важен — важен код набора.
        self.shell.run(
            [
                "docker", "compose", "-p", PROEKT_NABORA, "-f", str(compose_tests),
                "down", "-v", "--remove-orphans",
            ],
            cwd=self.config.project_dir,
            timeout=300,
        )
        self._step(steps, "tests", result)

    def _ci_zelyonye(self, target: str) -> str | None:
        """Прошёл ли тот же набор на этом коммите в CI. Строка — да, `None` — гнать свой.

        Спрашиваем даже при `force-update`: «обновись сейчас» — это про ожидание
        зелёного, а не про отказ от проверки, которая уже посчитана. Любая
        заминка с ответом означает «гнать свой набор»: пропустить проверку из-за
        недоступного GitHub значило бы обменять её на тишину.
        """
        if not self.config.require_ci:
            return None
        try:
            checks = self.github.checks(target)
        except GitHubError as failure:
            self.log(f"проверки GitHub недоступны ({failure}) — гоню набор здесь")
            return None
        if checks.state != CHECKS_SUCCESS:
            return None
        return f"зелёный в CI ({checks.detail})"

    def _config_check(self, steps: list[Step]) -> None:
        """Поднимется ли новый код на ЭТОЙ машине — до того, как сайт тронут.

        Тесты нового кода уже прошли, но они ничего не знают про настройки
        конкретной установки: `config/.env` в репозиторий не входит. Между
        «код исправен» и «код здесь заработает» лежит целый класс отказов —
        новый код требует настройку, которой на машине нет.

        Так и вышло: Redis стал обязательным, а обновление никогда не дописывает
        `OPENCRM_REDIS_URL` (пароль оно взять неоткуда). Отказ вылезал уже после
        подмены контейнера — сайт лёг, `/healthz` вернул 502, откатились и код,
        и база. И так кругом, каждые полчаса, пока человек не зашёл руками.

        Отказ по конфигу заранее известен и ничего не стоит: спросить можно, не
        трогая живой сайт. Спрашиваем через `compose run` — то есть тем же
        способом, которым через минуту поднимется приложение: те же `env_file`,
        те же `environment` из `docker/.env`, тот же вшитый в образ
        `OPENCRM_DEPLOYED`. Читать `config/.env` глазами было бы неверно: часть
        значений приложение получает мимо файла.

        `--entrypoint python` тут обязателен, а не для красоты: `run` подменяет
        команду, но НЕ точку входа, а точка входа образа — `entrypoint.sh`. Он
        ждёт базу, накатывает миграции и кончается `exec python -m uvicorn`,
        аргументы дальше не передавая вовсе. Без флага «безобидная проверка
        настроек» мигрировала бы боевую базу раньше своей очереди и подняла бы
        второй веб-сервер, а самой проверки не случилось бы ни разу.

        `--no-deps` — чтобы не поднять заодно базу и redis: проверка их не
        трогает. `-T` — потому что демон работает без терминала.

        Образ собирается ОТДЕЛЬНОЙ командой, а не флагом `run --build`: флаг
        появился только в Compose v2.13, а от установки проект требует просто
        «compose v2». Проверка, которая на части машин валит обновление из-за
        своего же флага, хуже отсутствующей. Заодно и отказ сборки боевого слоя
        случается здесь — до подмены, а не вместо неё; слои общие с
        `--target tests`, так что стоит это секунды.

        Собрать надо обязательно: без этого `run` возьмёт образ, лежащий с
        прошлого раза, и проверит требования СТАРОГО кода — то есть ровно не то,
        ради чего проверка заведена.

        Шага на странице обслуживания у проверки нет намеренно: сайт в этот
        момент ещё работает, смотреть страницу некому, а незнакомый ключ она
        считает мусором и гасит показ целиком.
        """
        self._sberech_obraz()
        sborka = self._compose("build", "app", timeout=self.config.build_timeout)
        if not sborka.ok:
            steps.append(Step("config", False, sborka.tail(12)))
            raise _Stop(f"новый код не собирается: {sborka.tail(6)}")

        result = self._compose(
            "run", "--rm", "--no-deps", "-T",
            "--entrypoint", "python", "app",
            "-m", "config.selfcheck",
            timeout=self.config.build_timeout,
        )
        steps.append(Step("config", result.ok, result.tail(12 if not result.ok else 1)))
        if not result.ok:
            raise _Stop(
                "новый код не поднимется с нынешними настройками: "
                f"{result.tail(6)}"
            )

    def _peresobrat_izmenyonnye(self, steps: list[Step], previous: str, target: str) -> None:
        """Пересоздать службы, у которых изменился примонтированный файл.

        **Дыра, которую это закрывает.** `up -d` пересоздаёт контейнер, когда
        изменилось ОПИСАНИЕ службы: образ, переменные, список томов. Содержимое
        файла, лежащего внутри тома, для compose не изменение вовсе — он его не
        читает. А половина обвязки живёт именно так: точка входа Alertmanager,
        точка входа Prometheus, шаблоны nginx, конфиг promtail, правила
        blackbox. Все они примонтированы из чекаута.

        Значит правка такого файла доезжала на сервер и **не применялась**.
        Приложение обновлялось, а Alertmanager продолжал работать скриптом
        недельной давности — и заметить это было нечем: контейнер здоров, лог
        чист, версия кода новая. Ровно так починка дублирующихся сообщений
        приехала бы на сервер и не подействовала.

        Часть служб перечитывает себя сама (Grafana берёт дашборды раз в минуту,
        Prometheus и Alertmanager перерисовывают конфиг раз в пять минут), но
        перечитывает она ДАННЫЕ, а не собственный запускающий скрипт: процесс в
        контейнере стартовал старым и таким останется до пересоздания.

        **Список служб не ведётся руками — его называет сам compose.**
        `config --format json` отдаёт разложенное описание вместе с точками
        монтирования, и пересечение этого списка с `git diff` даёт ответ. Список,
        выписанный руками, устарел бы на первом же новом томе, и устарел бы
        молча — то есть завёл бы ту же беду обратно.

        Не фатально: пересоздание — улучшение, а не условие работоспособности.
        Упади оно, сайт уже поднят и здоров, и валить из-за этого обновление с
        откатом базы было бы лечением тяжелее болезни.
        """
        if not previous or previous == target:
            return

        izmeneno = self._git("diff", "--name-only", previous, target)
        if not izmeneno.ok:
            steps.append(Step("recreate", True, "нечего сравнить: git diff молчит"))
            return
        fayly = {line.strip() for line in izmeneno.out.splitlines() if line.strip()}
        if not fayly:
            return

        opisanie = self._compose("config", "--format", "json", timeout=120)
        if not opisanie.ok:
            steps.append(Step("recreate", False, opisanie.tail(4)))
            return
        try:
            razobrano = json.loads(opisanie.out)
        except ValueError as beda:
            steps.append(Step("recreate", False, f"описание стека не разобралось: {beda}"))
            return

        koren = self.config.project_dir.resolve()
        zadety: set[str] = set()
        for imya, sluzhba in (razobrano.get("services") or {}).items():
            for tom in sluzhba.get("volumes") or []:
                if tom.get("type") != "bind":
                    continue
                istochnik = Path(tom.get("source", ""))
                try:
                    otnositelno = istochnik.resolve().relative_to(koren).as_posix()
                except (ValueError, OSError):
                    continue  # том вне чекаута — обновление его не привозит
                # Монтируют и файл, и целый каталог: и то, и другое задето, если
                # изменившийся путь совпал или лежит внутри.
                if any(f == otnositelno or f.startswith(otnositelno + "/") for f in fayly):
                    zadety.add(imya)

        if not zadety:
            return
        imena = sorted(zadety)
        self.log(f"примонтированные файлы изменились — пересоздаю: {', '.join(imena)}")
        self._step(
            steps,
            "recreate",
            self._compose(
                "up", "-d", "--force-recreate", "--no-deps", *imena,
                timeout=self.config.build_timeout,
            ),
            fatal=False,
        )

    def _snapshot(self, steps: list[Step], target: str) -> Path:
        """Копия базы перед миграциями — тем же кодом, что и в точке входа.

        Пока баз было две, здесь стояла проверка «файл базы существует», и на
        установке, переехавшей на MySQL, она отвечала ПРАВДУ и означала ЛОЖЬ:
        файл SQLite оставался лежать нарочно, шаг отчитывался успехом, сняв
        копию базы, которой никто не пользуется, а откат «удавался»,
        перезаписав тот же ненужный файл. Зелёный отчёт при отсутствующей
        копии хуже, чем отсутствие копии: отсутствие видно.


        Клиента `mysqldump` в образе приложения нет и не будет (разбор — в шапке
        `scripts/snapshot_db.py`), поэтому дамп пишет сам проект, на `pymysql`.
        Зовём его заходом в работающий контейнер приложения: адрес базы и пароль
        лежат в его окружении, а на хосте их быть не должно.

        Дамп сначала ложится в каталог данных (он подключён в контейнер), и
        только оттуда переезжает в каталог состояния — ровно как горячая копия
        SQLite строкой выше.

        Отсутствие копии здесь — это СТОП, а не предупреждение: миграции вперёд
        необратимы, и откатывать неудачное обновление было бы нечем.
        """
        destination = self.config.state_dir / f"pre-update-{target[:12]}.sql"
        inside = f"/app/data/{destination.name}"
        landed = self.config.data_dir / destination.name
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
            landed.unlink(missing_ok=True)
        except OSError as failure:
            steps.append(Step("backup", False, str(failure)))
            raise _Stop(f"не удалось подготовить место под копию базы: {failure}") from failure

        result = self._compose(
            "exec", "-T", "app", "python", "-m", "scripts.snapshot_db", "dump", inside,
            timeout=self.config.snapshot_timeout,
        )
        if not result.ok:
            landed.unlink(missing_ok=True)
            steps.append(Step("backup", False, result.tail(6) or "дамп ничего не сказал"))
            raise _Stop(f"не удалось снять копию базы MySQL: {result.tail(4)}")

        # Дамп отчитался успехом, а файла нет — это РАЗНЫЕ каталоги, а не сбой
        # дампа. Раньше оба случая печатали `result.tail`, и владелец читал
        # «снято: таблиц 40, строк 555» в качестве ПРИЧИНЫ отказа: искать он шёл
        # беду в базе, которой там нет, а обновления стояли.
        if not landed.exists():
            steps.append(Step("backup", False, f"дамп снят, но не появился в {landed.parent}"))
            raise _Stop(
                f"копия базы не найдена в {landed.parent}. Дамп пишет её в /app/data внутри "
                f"контейнера, и это должен быть тот же каталог: сверьте OPENCRM_HOME в "
                f"autoupdate.env и в docker/.env — их два, и правят обычно один"
            )

        try:
            shutil.move(str(landed), str(destination))
        except OSError as failure:
            steps.append(Step("backup", False, str(failure)))
            raise _Stop(f"не удалось убрать копию базы в {self.config.state_dir}: {failure}") from failure

        # Годность проверяем САМИ, а не только по коду возврата. Оборванный дамп
        # — обычный текстовый файл, и негодность у него не видна ничем, кроме
        # отсутствующего хвоста; а между «снял» и «положил» стоит ещё и
        # перенос между каталогами.
        if not _celaya(destination):
            steps.append(Step("backup", False, f"{destination.name}: нет метки конца"))
            raise _Stop(f"копия {destination.name} снята не до конца — метки конца в ней нет")

        razmer = destination.stat().st_size
        steps.append(Step("backup", True, f"{destination.name} ({razmer} Б, MySQL)"))
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

        **Зовём скрипт, а не голый сигнал.** `nginx -s reload` перечитывает уже
        отрендеренный `default.conf` и его include-ы, но заново подставить домен
        в шаблон не умеет — правки `*.conf.template` им не применяются вообще
        никогда. Хуже того, он шлёт мастеру SIGHUP и завершается кодом НОЛЬ, не
        дожидаясь разбора конфига: мастер отвергает конфиг, пишет об этом в свой
        лог и продолжает работать со старым, а сюда приходит успех. Провала не
        существовало в терминах кода — вот почему пять суток никто не знал, что
        конфиг не применяется. `docker/nginx/reload.sh` рендерит шаблон, гоняет
        `nginx -t` ДО сигнала и при красной проверке возвращает прежний файл и
        отдаёт ненулевой код. Только с ним поломка вообще становится видимой.

        Перезагрузка мягкая: старые рабочие процессы дорабатывают начатые
        запросы, порты не переоткрываются, простоя нет. Шаг не смертельный —
        nginx мог быть не поднят вовсе (у кого-то свой снаружи), и валить из-за
        этого удавшееся обновление незачем. **Но и не молчаливый**: результат
        кладётся в `detail` шага при ЛЮБОМ исходе, а `_notify` называет
        провалившиеся шаги в самом сообщении. Не смертельный не значит
        незаметный — ровно эта подмена и стоила проекту пяти суток.
        """
        result = self._compose("exec", "-T", "nginx", "sh", "/opencrm/reload.sh", timeout=60)
        # Мимо `_step` намеренно: он заполняет `detail` только на провале, а
        # здесь ценна и удача — в истории видно, какой шаблон применён. Менять
        # ради этого поведение всех остальных шагов незачем.
        steps.append(Step("nginx-reload", result.ok, result.tail(2 if result.ok else 12)))

    def _health(self, steps: list[Step], name: str) -> None:
        reason = self.wait_healthy()
        # Пропуск smoke-тестов попадает В ОТЧЁТ, а не только в лог демона.
        #
        # Шаг зелёный с пустой подробностью читается как «страницы проверены», а
        # проверены они не были. Разница не косметическая: пока сайт закрыт (в
        # том числе если режим ЗАСТРЯЛ включённым — такое здесь уже случалось,
        # см. `web/main.py` про убитый посреди переноса переезд), каждая
        # следующая выкатка идёт без проверки страниц. Сломанная сборка получает
        # STATUS_DEPLOYED, `failed_sha` очищается, и когда владелец откроет сайт,
        # откатывать будет уже некому: обновлятор просыпается на новый коммит, а
        # не на «стало плохо».
        #
        # То же правило, что у перезагрузки nginx строкой ниже: не смертельный
        # шаг — не значит незаметный. Пять суток на прошлой такой немоте.
        podrobnost = reason or self._smoke_propushchen
        steps.append(Step(name, not reason, podrobnost))
        if reason:
            raise _Stop(reason)

    def wait_healthy(self) -> str:
        """Пустая строка — сайт живой; иначе причина, годная для уведомления."""
        last = "нет ответа"
        obsluzhivanie = False
        # Новая попытка — новый ответ: остаток от прошлой сказал бы про smoke,
        # которых в этот раз не было.
        self._smoke_propushchen = ""
        # Что сказал о себе сам контейнер. Снимаем на КАЖДОЙ попытке, а не один
        # раз в конце, и вот почему: у службы `app` стоит `restart:
        # unless-stopped`, то есть упавший контейнер поднимается снова и снова.
        # Каждый заход переписывает файл дважды — сперва `running migrate`,
        # потом `failed migrate`, — и одно чтение в конце с равной вероятностью
        # попадает в окно `running`, где причины нет. Поймано живым прогоном на
        # стенде: контейнер честно писал «Table ... already exists», а в отчёт
        # это не попадало ни разу.
        skazal = ""
        attempt = 0
        prodleno = 0
        while attempt < self.config.health_attempts:
            skazal = skazal or self._chto_skazal_konteyner()
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
                        # Заодно запоминаем, закрыт ли сайт на работы: ниже от
                        # этого зависит, спрашивать ли smoke-тесты. Спросить об
                        # этом больше некого — обновление идёт на хосте, а
                        # настройка живёт в базе приложения.
                        obsluzhivanie = payload.get("maintenance") == "on"
                        break
                    else:
                        last = f"{self.config.health_url}: {payload}"
            else:
                last = f"{self.config.health_url}: {response.status or response.body[:120]}"
            # Контейнер снимает копию и гонит миграции — это не «не отвечает»: на
            # большой базе одна копия идёт минуту с лишним, и исправное обновление
            # откатывалось по окну здоровья (разбор 29.09.2026). Записал `failed` —
            # не ждём: падающая миграция не должна тянуть откат.
            if not skazal and prodleno < self.config.migrate_attempts and self._idut_migratsii():
                prodleno += 1
            else:
                attempt += 1
            if attempt < self.config.health_attempts:
                self._sleep(self.config.health_delay)
        else:
            skazal = skazal or self._chto_skazal_konteyner()
            return (
                f"health-check не прошёл за {self.config.health_attempts} попыток — "
                f"{last}{skazal}"
            )

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

        # **Сайт, закрытый на работы, smoke-тесты пройти не может НИКОГДА.**
        #
        # Режим обслуживания отдаёт 503 всем, у кого нет сессии владельца, а
        # nginx подменяет ответ своей страницей с тем же кодом. То есть пока
        # владелец держит сайт закрытым, каждая выкатка выглядела провалом:
        # обновление откатывало КОД И БАЗУ — заливало дамп, снятый до сборки,
        # поверх живой базы, — и рапортовало «сломано». Всё, что владелец
        # записал в CRM за это время (а он в ней работает: режим закрывает сайт
        # от посетителей, не от него), исчезало молча.
        #
        # `/healthz` выше уже подтвердил, что приложение живо и схема сошлась,
        # и он же сказал, что сайт закрыт намеренно. Проверять после этого,
        # отдаются ли страницы посетителю, нечего: им сейчас и не положено.
        if obsluzhivanie:
            self._smoke_propushchen = (
                "сайт закрыт на работы — страницы посетителю не проверялись"
            )
            self.log(self._smoke_propushchen)
            return ""

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
                   self._git(*self._checkout_args(previous)), fatal=False)
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
            snapshot = self._svezhaya_kopiya(snapshot)
            failure, porvana = self._restore_db(snapshot)
            steps.append(Step("rollback-db", not failure, failure or f"из {snapshot.name}"))
            if porvana:
                # Заливка встала посреди таблиц: база смешанная. Старый код на ней
                # поднимался (`schema_check` ругается только на нехватку) со статусом
                # «откатили». Не поднимаем: nginx покажет страницу обслуживания.
                self.journal.write(baza_porvana=snapshot.name)
                steps.append(Step("rollback-deploy", False, "не поднимали: база залита не до конца"))
                return f"база залита не до конца, приложение не поднято: {failure}"

        self._step(steps, "rollback-deploy", self._podnyat_prezhniy(), fatal=False)
        reason = self.wait_healthy()
        steps.append(Step("rollback-health", not reason, reason))
        return reason

    def _pribrat_snimki(self) -> None:
        """Оставить последние `SNAPSHOTS_KEPT` копий базы КАЖДОГО вида.

        **Иначе обновления однажды встают совсем.** Каждое обновление кладёт
        `pre-update-<коммит>.sql`, каждый откат — ещё и
        `failed-update-<время>.sql`, и не удалялось из них ничего. На боевой
        базе дамп — это гигабайт с лишним (замер в шапке docker/entrypoint.sh),
        а `preflight` требует свободных 2 ГБ и считал их как «одна копия плюс
        слои образа», а не как растущую стопку. Три-четыре обновления — и
        обновление останавливается навсегда с сообщением про место, в котором
        про стопку дампов не сказано ни слова.

        Соседи так и делают: точка входа держит пять своих копий, `backup.sh`
        чистит по возрасту. Здесь — по счёту, потому что копии привязаны к
        коммитам, а не к дням: за день их может быть и десять, и ни одной.

        Зовётся ПЕРВЫМ шагом обновления, до проверки свободного места, и это не
        мелочь: `_preflight` отказывает по `min_free_mb` раньше, чем начинается
        съёмка копии. Убирай мы в конце съёмки — на машине, которую старые дампы
        уже забили, уборка не позвалась бы никогда, и правка не лечила бы ровно
        того состояния, против которого написана.

        Беда с уборкой не смертельна — место кончится позже, а обновление
        важнее.
        """
        # Черновик дампа, убитого на середине (`*.chernovik`, см. snapshot_db.snyat),
        # не подбирал никто: до гигабайта мусора навсегда. Обновлятор идёт под
        # замком обслуживания, так что живых черновиков этих имён сейчас нет.
        for papka in (self.config.data_dir, self.config.state_dir):
            for obrazets in ("pre-update-*.chernovik", "failed-update-*.chernovik"):
                try:
                    for chernovik in papka.glob(obrazets):
                        chernovik.unlink()
                        self.log(f"убран черновик оборванного дампа: {chernovik.name}")
                except OSError as beda:
                    self.log(f"не убрать черновики {obrazets}: {beda}")
        for obrazets in ("pre-update-*.sql", "failed-update-*.sql"):
            try:
                fayly = sorted(
                    self.config.state_dir.glob(obrazets),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError as beda:
                # `continue`, а не `return`: беда с одним видом копий не повод
                # бросать второй. Иначе неудача на `pre-update-*` тихо отменяла
                # бы уборку `failed-update-*` — и место продолжало бы кончаться.
                self.log(f"не перечислить копии базы ({obrazets}): {beda}")
                continue
            for lishniy in fayly[SNAPSHOTS_KEPT:]:
                try:
                    lishniy.unlink()
                    self.log(f"убрана старая копия базы: {lishniy.name}")
                except OSError as beda:
                    self.log(f"не убрать {lishniy.name}: {beda}")

    def _svezhaya_kopiya(self, snapshot: Path) -> Path:
        """Копия точки входа той же ревизии, если она снята за это обновление.

        `pre-update` снимается с РАБОТАЮЩЕГО сайта: записанное за время дампа и
        подмены в неё не попадает, и откат стирал это молча. Точка входа снимает
        свою уже после остановки старого приложения. Берём её, только если ревизия
        та же (частично мигрированная база дала бы другую), копия целая и снята за
        это обновление (разбор 29.09.2026).
        """
        reviziya = _reviziya_kopii(snapshot)
        if not reviziya:
            return snapshot
        kandidat = self.config.data_dir / f"mysql.pre-migrate-{reviziya}.sql"
        try:
            svezhaya = kandidat.stat().st_mtime >= self._nachalo_obnovleniya
        except OSError:
            return snapshot
        if svezhaya and _celaya(kandidat):
            self.log(f"откат берёт копию точки входа: {kandidat.name}")
            return kandidat
        return snapshot

    def _restore_db(self, snapshot: Path) -> tuple[str, bool]:
        """Вернуть базу из копии: (причина отказа или пусто, оборвана ли заливка).

        Клиент `mysql` живёт в образе базы, поэтому дамп отдаётся ему на вход
        заходом в службу `db` — тем же способом, каким восстанавливают обычные
        копии (`scripts/backup.sh`, шапка `scripts/snapshot_db.py`).

        Порядок внутри важен, и каждый шаг здесь отвечает на «а если нет»:

        1. **Проверяем метку конца ДО заливки.** Оборванный дамп не отличим от
           целого ничем другим, а залитый наполовину он оставит базу в состоянии
           хуже исходного — с частью таблиц от старой схемы и частью от новой.
        2. **Сначала снимаем дамп ТЕКУЩЕГО состояния.** У SQLite неудачная база
           откладывается файлом (`*.failed-update-*`) — здесь то же самое стоит
           одного дампа. В ней данные за время неудавшегося обновления, и
           разбираться с ними будет человек. Шаг не смертельный: не вышло —
           сказали и поехали дальше, потому что вернуть рабочую базу важнее.
        """
        if not self.config.mysql_db:
            return "не разобрать имя базы из OPENCRM_DB_URL — заливать дамп некуда", False
        if not _celaya(snapshot):
            return f"копия {snapshot.name} оборвана (нет метки конца) — заливать её нельзя", False

        stamp = time.strftime("%Y%m%d-%H%M%S")
        otlozhennaya = self.config.state_dir / f"failed-update-{stamp}.sql"
        # Контейнер приложения на этот момент уже остановлен (`rollback-stop`),
        # поэтому дамп снимает ОДНОРАЗОВЫЙ контейнер из того же образа: он
        # читает `config/.env` заново и живёт полминуты.
        # `-T`, как и у всех прочих заходов: без него compose просит терминал,
        # а обновление идёт из systemd, где терминала нет.
        self._compose(
            "run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "app", "-c",
            f"python -m scripts.snapshot_db dump /app/data/{otlozhennaya.name}",
            timeout=self.config.snapshot_timeout,
        )
        upala = self.config.data_dir / otlozhennaya.name
        if upala.exists():
            try:
                shutil.move(str(upala), str(otlozhennaya))
            except OSError as failure:
                self.log(f"не удалось отложить неудачную базу: {failure}")

        result = self._compose(
            "exec", "-T", self.config.db_service, "sh", "-c",
            # Пароль раскрывается ВНУТРИ контейнера: в `ps` на хосте ему не место.
            # Предел пакета клиента — 16 МБ, а одно письмо бывает крупнее: без флага
            # заливка вставала посреди таблиц, и база оставалась наполовину старой.
            f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --max-allowed-packet=1G {self.config.mysql_db}',
            stdin=snapshot,
            timeout=self.config.snapshot_timeout,
        )
        if not result.ok:
            return f"заливка дампа в MySQL не удалась: {result.tail(4)}", True
        return self._snyat_lishnie(snapshot), False

    def _snyat_lishnie(self, snapshot: Path) -> str:
        """Убрать таблицы, которых в копии нет. Пустая строка — получилось.

        **Найдено живым откатом на стенде, а не рассуждением.** Копия снимается
        ДО миграций и открывает каждую свою таблицу строкой `DROP TABLE IF
        EXISTS` — то есть заливка полностью пересобирает всё, что копия знает.
        Таблицу, которую упавшая миграция успела создать уже ПОСЛЕ снимка,
        копия не знает, и заливка её не трогает. `alembic_version` при этом
        откатывается.

        Дальше беда тихая. Схема с лишней таблицей поднимается (schema_check
        ругается только на нехватку — это осознанно, иначе откат перестал бы
        быть способом починки), сайт живёт, отчёт зелёный. А ПОВТОРНАЯ попытка
        того же коммита упирается в `(1050, "Table 'x' already exists")` — и
        будет упираться всегда, пока человек не удалит таблицу руками. Замерено
        на стенде: два прогона подряд, оба откатились, второй — уже по этой
        причине, и в отчёте оба раза стояло одно и то же «health-check не
        прошёл».

        Сносим только БАЗОВЫЕ таблицы и только те, которых копия не знает, —
        то есть по построению ровно те, которых на момент снимка не было.
        Представления не трогаем: дампер их и не снимает.

        Не смертельно: не вышло перечислить таблицы — говорим и едем дальше.
        Вернуть рабочую базу важнее, чем прибрать за миграцией.
        """
        znaet = _tablicy_kopii(snapshot)
        if not znaet:
            # Копия без единой таблицы — это либо пустая база (первый деплой),
            # либо разбор разошёлся с дампером, либо файл не прочитался вовсе.
            # Сносить во всех случаях нечего и опасно: разница вышла бы «вся
            # база». Говорим вслух: молчаливый пропуск уборки выглядит как
            # успешная уборка, а разница между ними — та самая «Table already
            # exists» на следующем заходе.
            self.log(f"уборка за миграцией пропущена: в {snapshot.name} не нашлось таблиц")
            return ""

        est = self._compose(
            "exec", "-T", self.config.db_service, "sh", "-c",
            f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -B {self.config.mysql_db} '
            "-e \"SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'\"",
            timeout=self.config.snapshot_timeout,
        )
        if not est.ok:
            self.log("не перечислить таблицы базы — оставшиеся от миграций не снимаю")
            return ""

        zhivye = {stroka.split("\t")[0] for stroka in est.out.splitlines() if stroka.strip()}
        lishnie = sorted(zhivye - znaet)
        # Имена уезжают в командную строку, поэтому пропускаем только простые.
        # Чужое в нашей схеме — не то, что стоит сносить вслепую, а имя с
        # кавычкой или пробелом собрало бы команду не ту, что задумана. То же
        # правило и по той же причине стоит на имени базы (`_imya_bazy`).
        strannye = [imya for imya in lishnie if not _PROSTOE_IMYA.fullmatch(imya)]
        if strannye:
            self.log(f"не трогаю таблицы с непростыми именами: {', '.join(strannye)}")
        lishnie = [imya for imya in lishnie if _PROSTOE_IMYA.fullmatch(imya)]
        if not lishnie:
            return ""

        # Обратные кавычки ЭКРАНИРОВАНЫ, и это не придирка: строка целиком
        # уезжает в `sh -c`, где голая обратная кавычка открывает подстановку
        # команды. Поймано живым откатом на стенде: `sh` ответил
        # «otkat_sled: command not found», в mysql уехало пустое имя, и снос
        # молча не состоялся при зелёном по виду шаге.
        spisok = ", ".join(BS + BT + imya + BS + BT for imya in lishnie)
        vidno = ", ".join(lishnie)
        # FOREIGN_KEY_CHECKS=0: у таблицы из упавшей миграции могут быть ссылки
        # на восстановленные, и порядок сноса нас не касается.
        snos = self._compose(
            "exec", "-T", self.config.db_service, "sh", "-c",
            f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot {self.config.mysql_db} '
            f'-e "SET FOREIGN_KEY_CHECKS=0; DROP TABLE IF EXISTS {spisok};"',
            timeout=self.config.snapshot_timeout,
        )
        if not snos.ok:
            return f"не снять оставшиеся от миграций таблицы ({vidno}): {snos.tail(3)}"
        self.log(f"сняты таблицы, оставшиеся от неудавшихся миграций: {vidno}")
        return ""

    def _sberech_obraz(self) -> None:
        """Пометить образ работающего приложения тегом отката.

        Откат пересобирал старый код из исходников, а зависимости не закреплены и
        `pip install .` стоит после копирования кода: при ужатом кэше сборки откат
        шёл в PyPI за самыми свежими версиями — сломавший обновление выпуск
        зависимости доставался и старому коду, а без сети откат не собирался вовсе
        (разбор 29.09.2026). Не вышло пометить — откат соберёт по-старому.
        """
        self._obraz_otkata = ""
        konteyner = self._compose("ps", "-q", "app", timeout=60).out.strip().splitlines()
        if not konteyner:
            return
        svedeniya = self.shell.run(
            ["docker", "inspect", "-f", "{{.Image}} {{.Config.Image}}", konteyner[0]],
            cwd=self.config.project_dir, timeout=60,
        )
        chasti = svedeniya.out.split()
        if not svedeniya.ok or len(chasti) != 2:
            return
        obraz, imya = chasti
        if self.shell.run(["docker", "tag", obraz, OBRAZ_OTKATA], cwd=self.config.project_dir, timeout=60).ok:
            self._obraz_otkata = imya

    def _podnyat_prezhniy(self) -> Result:
        """Поднять прежний код: сбережённым образом, а без него — сборкой, как раньше."""
        if self._obraz_otkata:
            vernuli = self.shell.run(
                ["docker", "tag", OBRAZ_OTKATA, self._obraz_otkata], cwd=self.config.project_dir, timeout=60,
            )
            if vernuli.ok:
                return self._compose("up", "-d", "--no-build", timeout=self.config.build_timeout)
            self.log(f"образ отката не вернулся ({vernuli.tail(2)}) — собираю прежний код")
        return self._compose("up", "-d", "--build", timeout=self.config.build_timeout)

    def _idut_migratsii(self) -> bool:
        """Контейнер на шаге копии и миграций: `running migrate` пишет только он."""
        try:
            zapis = json.loads(self._progress_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(zapis, dict) and zapis.get("phase") == "running" and zapis.get("step") == "migrate"

    def _chto_skazal_konteyner(self) -> str:
        """Причина от самого контейнера, если он успел её записать.

        **Зачем.** Снаружи «сайт не отвечает» выглядит одинаково у трёх разных
        бед: код сломан, миграции упали, база не пускает. Отчёт про все три
        говорил «health-check не прошёл за N попыток», и человек шёл разбираться
        с нуля — при том что контейнер причину ЗНАЕТ и уже записал:
        `write_state failed migrate "<хвост журнала миграций>"` в
        docker/entrypoint.sh.

        Поймано живым откатом на стенде: два обновления подряд откатились по
        РАЗНЫМ причинам (первое — сломанный код, второе — «таблица уже есть»
        от таблицы, оставшейся после первого), а в отчёте оба раза стояла одна
        и та же строка.

        Своё от чужого отличаем фазой. К этому моменту обновлятор писал в тот
        же файл только `running`; `failed` там может стоять лишь от контейнера,
        который стартовал и упал. Пусто, нечитаемо, не тот вид — молчим: файл
        удобство, а не источник правды, и лишнего слова в аварии не надо.
        """
        try:
            zapis = json.loads(self._progress_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(zapis, dict) or zapis.get("phase") != "failed":
            return ""
        shag = str(zapis.get("step") or "").strip()
        prichina = " ".join(str(zapis.get("error") or "").split())
        if not shag and not prichina:
            return ""
        # Хвост причины короткий: он уезжает в Telegram, где длинное сообщение
        # отбивается целиком. Подробности лежат в журнале контейнера.
        if len(prichina) > 300:
            prichina = prichina[:300] + "…"
        nazvanie = {"migrate": "миграции", "start": "старт приложения"}.get(shag, shag)
        return f". Контейнер сказал: {nazvanie}" + (f" — {prichina}" if prichina else "")

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

    def _compose(self, *args: str, timeout: float | None = None, stdin: Path | None = None) -> Result:
        return self.shell.run(
            ["docker", "compose", "-f", str(self.config.compose_file), *args],
            cwd=self.config.project_dir,
            timeout=timeout,
            stdin=stdin,
        )

    def _step(self, steps: list[Step], name: str, result: Result, fatal: bool = True) -> Result:
        """Записать шаг, а при неудаче — сказать, ЧТО именно не вышло.

        Раньше здесь стоял `result.tail()` — последние строки вывода. На
        `docker build` это работало, на `docker compose up` не работает
        никогда: компоуз пишет ход контейнеров в поток ошибок, а `Result.text`
        приклеивает поток ошибок ПОСЛЕ обычного, и хвост — это гарантированно
        «Container … Stopped», а не упавший тест.

        Так и вышло 25.08.2026: обновление откатилось на шаге `tests`, а в
        сообщении владельцу про тесты не было ни слова. Разбор — в
        `deploy/vyzhimka.py`.
        """
        steps.append(Step(name, result.ok, "" if result.ok else vyzhimka.vyzhat_strokoy(result.text)))
        if not result.ok and fatal:
            raise _Stop(f"{name}: {vyzhimka.vyzhat_strokoy(result.text, 4)}")
        return result

    #: Как выглядит исход: значок, заголовок и надо ли звенеть на телефоне.
    #:
    #: Значок стоит первым не для красоты: в списке чатов Telegram показывает
    #: начало последней строки, и по одному символу видно, идти смотреть или нет.
    #:
    #: STATUS_WAITING в списке нет намеренно: ожидание зелёного CI — не новость,
    #: а нормальный ход дела, и уведомлять о нём каждые пять минут значило бы
    #: приучить не читать эти сообщения.
    ISHODY = {
        STATUS_DEPLOYED: ("✅", "Обновлено", True),
        STATUS_ROLLED_BACK: ("↩️", "Обновление откачено", False),
        STATUS_BROKEN: ("🆘", "Откат не поднялся — нужен человек", False),
        STATUS_ABORTED: ("⏸", "Обновление не начиналось", True),
    }

    def _notify(self, outcome: Outcome) -> None:
        ishod = self.ISHODY.get(outcome.status)
        if not ishod:
            return
        znachok, zagolovok, tiho = ishod
        e = notify.ekranirovat

        # --- первая строка: она же и всё, что видно в списке чатов ---
        #
        # Заголовок исхода и заголовок коммита стоят вместе намеренно. «✅
        # Обновлено» отвечает на половину вопроса; вторая половина — «чем
        # именно», и без неё чат приходится открывать всегда. Значок впереди
        # отвечает на главное — идти смотреть или нет.
        zaglavie = f"{znachok} <b>{e(zagolovok)}</b>"
        if outcome.summary:
            zaglavie += f" · <b>{e(outcome.summary)}</b>"
        stroki = [zaglavie]

        # Вторая строка — откуда и что приехало, и она же ссылка.
        #
        # Ссылка ведёт на СРАВНЕНИЕ, а не на один коммит: между двумя
        # обновлениями в ветку обычно попадает несколько коммитов, и страница
        # одного последнего показывает не то, что приехало. Когда предыдущего
        # номера нет (первый деплой), сравнивать не с чем — тогда коммит.
        # Карточку предпросмотра мы гасим в `notify`, поэтому ссылка не съедает
        # пол-экрана.
        adres = f"{self.config.repo}@{self.config.branch}"
        stroka = f"<code>{e(adres)}</code>"
        if outcome.to_sha:
            bylo = (outcome.from_sha or "")[:12]
            stalo = outcome.to_sha[:12]
            if bylo:
                podpis = f"{bylo} → {stalo}"
                ssylka = (
                    f"https://github.com/{self.config.repo}"
                    f"/compare/{outcome.from_sha}...{outcome.to_sha}"
                )
            else:
                podpis = stalo
                ssylka = f"https://github.com/{self.config.repo}/commit/{outcome.to_sha}"
            stroka += f" · <a href=\"{e(ssylka)}\"><code>{e(podpis)}</code></a>"
        stroki.append(stroka)
        if outcome.kesh:
            stroki.append(self._stroka_kesha(outcome.kesh))

        # --- причина: главное, ради чего сообщение читают ---
        if outcome.reason:
            stroki.append("")
            stroki.append(f"<b>{e(outcome.reason)}</b>")

        # --- шаги: провалившиеся видно сразу, остальные под кат ---
        #
        # Провалившийся шаг называется ВСЕГДА, в том числе когда обновление в
        # целом удалось. Не смертельный шаг — не значит незаметный: заголовок
        # «Обновлено» читают как «всё хорошо», а под ним может лежать
        # непримененный конфиг nginx. Ровно так пять суток никто не знал, что
        # сайт работает со старым конфигом.
        upali = [shag for shag in outcome.steps if not shag.ok]
        if upali:
            stroki.append("")
            if outcome.status == STATUS_DEPLOYED:
                stroki.append("<b>Но не всё прошло гладко:</b>")
            for shag in upali:
                # Подробность не повторяем, если она уже сказана причиной выше:
                # один и тот же текст трижды подряд (причина, шаг, список
                # ходов) читается как три разные беды.
                podrobno = ""
                if shag.detail and shag.detail not in (outcome.reason or ""):
                    podrobno = f": {shag.detail}"
                stroki.append(f"✗ <b>{e(shag.name)}</b>{e(podrobno)}")

        # Полный список ходов — сворачиваемой цитатой. Разбирают его редко, но
        # когда разбирают, идти за ним в журнал на сервер неоткуда: сообщение
        # приходит туда, где человек уже есть.
        if outcome.steps:
            stroki.append("")
            stroki.append(self._hod_shagov(outcome))

        # --- подвал: сколько заняло и не устарело ли само сообщение ---
        stroki.append("")
        stroki.append(f"<i>{e(_dlitelnost(outcome.seconds))}</i>")
        if self._sam_ustarel(outcome):
            stroki.append(
                "<i>♻️ Обновление задело код самого обновлятора: служба сейчас "
                "перезапустится, а это сообщение написано ещё прежней её версией. "
                "Правки в оформлении уведомлений видны со следующего обновления.</i>"
            )

        self.notifier.send("\n".join(stroki), tiho=tiho and not upali)
        self._prilozhit_otchyot(outcome, tiho=tiho and not upali)

    def _prilozhit_otchyot(self, outcome: Outcome, *, tiho: bool) -> None:
        """Приложить к переписке PDF и Word с разбором того, что произошло.

        **Под `try/except` целиком, и это не перестраховка.** Отчёт — приятная
        добавка; сообщение владельцу важнее её, а работающий сайт важнее их
        обоих. Собрался отчёт наполовину — уйдёт половина; не собрался вовсе —
        останется обычное сообщение, ровно как было до отчётов. Единственное,
        чего эта добавка не имеет права сделать, — уронить обновление, которое
        уже прошло.

        Молчим и о собственной беде тоже: писать в телеграм «не смог сделать
        отчёт» значит слать второе сообщение вместо содержания. Строка уходит в
        журнал демона, и этого достаточно.
        """
        if not getattr(self.notifier, "configured", False):
            return
        try:
            fayly = otchyot.sdelat_fayly(outcome, self.config, self._zhurnal)
            podpis = "Разбор обновления" if outcome.ok else "Разбор неудачи"
            for imya, soderzhimoe in fayly:
                self.notifier.send_document(imya, soderzhimoe, podpis, tiho=tiho)
        except Exception as beda:  # noqa: BLE001 — добавка не роняет обновление
            self.log(f"отчёт об обновлении не собрался: {beda}")

    def _sam_ustarel(self, outcome: Outcome) -> bool:
        """Написано ли это сообщение кодом, который обновление только что заменило.

        Так и есть всегда, когда обновление везёт правку `deploy/`: Python
        загрузил модули при старте демона, сообщение об исходе собирает
        `_notify` из памяти, а на диске уже лежит новый код. Дальше `watch`
        увидит это же расхождение (`_self_changed`) и выйдет, systemd поднимет
        службу заново — но сообщение к тому моменту уже отправлено СТАРЫМ
        оформлением.

        Со стороны это выглядит как «обновление приехало, а ничего не
        изменилось», и объяснить это нечем, кроме как сказав прямо. Молчание
        здесь стоит дороже строчки: владелец идёт искать поломку там, где её
        нет.
        """
        if outcome.status != STATUS_DEPLOYED:
            return False
        return self._self_changed(outcome.from_sha)

    def _hod_shagov(self, outcome: Outcome) -> str:
        """Все шаги одной сворачиваемой цитатой.

        `expandable` — то самое «показать ещё» Telegram: список из
        четырнадцати ходов не занимает пол-экрана, но и не потерян. Первая
        строка цитаты видна всегда, поэтому в неё выносится счёт.
        """
        e = notify.ekranirovat
        proshli = sum(1 for shag in outcome.steps if shag.ok)
        vsego = len(outcome.steps)
        vnutri = [f"<b>Ход обновления: {proshli} из {vsego}</b>"]
        for shag in outcome.steps:
            znak = "✓" if shag.ok else "✗"
            podrobno = f" — {shag.detail}" if shag.detail else ""
            vnutri.append(f"{znak} {e(shag.name)}{e(podrobno)}")
        return "<blockquote expandable>" + "\n".join(vnutri) + "</blockquote>"


def _do_kogda(kogda: float) -> str:
    """«жду до 14:05» — человеку нужен час, а не число секунд от эпохи."""
    return f"лимит GitHub исчерпан, жду до {chas(kogda)}"


def _dlitelnost(sekund: float) -> str:
    """«1136 c» глазами человека — «18 мин 56 с».

    Секундами меряют то, что укладывается в минуту; всё остальное человек
    всё равно переводит в уме, и делать это за него дешевле, чем заставлять.
    """
    vsego = int(sekund)
    if vsego < 60:
        return f"{vsego} с"
    minut, sekundy = divmod(vsego, 60)
    if minut < 60:
        return f"{minut} мин {sekundy:02d} с"
    chasov, minut = divmod(minut, 60)
    return f"{chasov} ч {minut:02d} мин"


def _utc_now() -> str:
    """Время начала — в UTC и с явной `Z`.

    Часовой пояс сервера странице неизвестен, а показать надо местное время
    посетителя: без пометки зоны браузер разобрал бы строку как своё локальное
    время и «начало» уехало бы на несколько часов.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build(config: UpdateConfig, log=None) -> Updater:
    return Updater(config, log=log)
