"""Числа о самом приложении — в том виде, в каком их читает Prometheus.

Здесь ровно то, чего снаружи не видно и о чём не спросишь ни у ядра, ни у
докера: размер базы, свежесть резервной копии, чем кончился последний деплой.
Всё остальное — доступность сайта, доли ответов, память, диск, рестарты
контейнеров — меряется **снаружи** (blackbox, node-exporter, cAdvisor, логи
nginx), и это не деление по вкусу. Метрика, которую отдаёт само приложение,
пропадает ровно тогда, когда приложение легло, то есть в единственный момент,
когда она нужна. Поэтому «жив ли сайт» здесь не измеряется вовсе.

**Что сюда не попадает — и почему это правило, а не аккуратность.** Метрики
отдаются без сессии (Prometheus её взять негде) и живут в Prometheus месяцами.
Любое значение, попавшее в метку, переживает и запрос, и того, кто его послал.
Поэтому:

* меток с данными клиентов, адресами, email, номерами заявок и путями запросов
  здесь нет. Набор меток закрытый: он собирается из констант этого файла, а не
  из того, что пришло снаружи, — иначе первый же `path`-лейбл превратил бы
  Prometheus в журнал посещений с бесконечной кардинальностью;
* секретов нет тем более. Рядом с копиями базы лежит `secret-*.env` с ключом
  подписи (`scripts/backup.sh`), поэтому файлы в каталоге копий **только
  считаются и стятся** — ни один из них не читается, кроме отчёта проверки,
  где секретов нет по устройству.

**Ни одного запроса к базе.** Не только из-за границы `database/` (CLAUDE.md,
раздел 3), но и по существу: метрики опрашивают раз в полминуты вечно, и
запрос, который стоит копейки на пустой базе, на населённой станет постоянной
фоновой нагрузкой. Всё, что здесь считается, — это `stat` по файлам и одна
строка JSON.

**Отдельная ошибка не имеет права уронить весь ответ.** Отчёт мониторинга,
отвечающий 500 из-за отсутствующего каталога, — это мониторинг, который сам
себя выключил. Каждый сборщик закрыт своим `except`, и не собравшаяся метрика
просто отсутствует в ответе; отсутствие тоже видно — в Prometheus оно
выражается пропавшим рядом, и правила это учитывают.

Наружу маршрут не публикуется: nginx закрывает `/api/v1/metrics` (см.
`docker/nginx/templates/locations.inc`), а Prometheus ходит к приложению
напрямую по сети compose, минуя nginx.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from config.settings import BASE_DIR, get_settings
from database.repositories import engine_info
from database.session import SessionLocal
from web.api.deps import require_module

# Блок мониторинга закрывает этот маршрут целиком: выключенный блок исчезает и
# из API тоже, а не только из меню (CLAUDE.md, раздел 3). Стоять ему здесь
# безопасно — контейнеры мониторинга при выключенном блоке не подняты и
# спрашивать метрики некому.
router = APIRouter(
    prefix="/metrics",
    tags=["monitoring"],
    dependencies=[Depends(require_module("monitoring"))],
)

#: Формат текстового изложения Prometheus. Версия в типе — часть протокола, без
#: неё некоторые клиенты разбирают ответ как обычный текст.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Исходы обновления, какие бывают (deploy/updater.py). Список закрытый: метка
#: `status` берётся отсюда, а не из файла, поэтому испорченная история не может
#: наплодить рядов с произвольными именами.
DEPLOY_STATUSES = (
    "deployed",
    "rolled-back",
    "broken",
    "aborted",
    "waiting-checks",
    "up-to-date",
    "disabled",
)


def _data_dir() -> Path:
    """Каталог состояния: там же база, там же копии.

    В контейнере это `/app/data` (том из docker-compose.yml), на ноутбуке —
    `data/` рядом с репозиторием. Берётся из адреса базы, когда она файловая, и
    иначе — от корня проекта: на MySQL файла базы нет, а копии `backup.sh`
    кладёт в тот же `data/backups`.
    """
    url = get_settings().db_url
    if url.startswith("sqlite"):
        return _sqlite_path(url).parent
    return Path(BASE_DIR) / "data"


def _sqlite_path(url: str) -> Path:
    """Файл базы из URL SQLAlchemy.

    Третий слэш отделяет (всегда пустой) хост от пути, поэтому снимаем ровно
    один: у `sqlite:///./data/x.db` не остаётся ничего лишнего, а у
    `sqlite:////app/data/opencrm.db` остаётся ведущий слэш абсолютного пути.
    Снимать все подряд (`lstrip`) нельзя — именно так абсолютный путь и
    превращается в относительный, указывающий на несуществующий файл рядом.
    """
    tail = url.partition("://")[2]
    return Path(tail[1:] if tail.startswith("/") else tail)


def _updates_dir() -> Path:
    """Где лежит журнал обновлений.

    Каталог примонтирован в контейнер **только на чтение** (docker-compose.yml):
    пишет в него демон обновления на хосте, а приложение всего лишь смотрит,
    чем кончился прошлый деплой.
    """
    return Path(os.environ.get("OPENCRM_UPDATES_DIR") or Path(BASE_DIR) / "updates")


class Metrics:
    """Накопитель строк. Имена и метки складываются только здесь.

    Метки принимаются словарём из литералов вызывающего кода — снаружи в них
    ничего не попадает. Значения экранируются на всякий случай: строка с
    кавычкой сломала бы разбор всего ответа, а не одной метрики.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(
        self,
        name: str,
        value: float | int,
        *,
        help_text: str,
        kind: str = "gauge",
        labels: dict[str, str] | None = None,
    ) -> None:
        if not any(line.startswith(f"# TYPE {name} ") for line in self.lines):
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} {kind}")
        rendered = ""
        if labels:
            inside = ",".join(f'{key}="{_escape(val)}"' for key, val in labels.items())
            rendered = "{" + inside + "}"
        # Целые печатаем целыми: Prometheus принимает и float, но метки времени
        # в экспоненциальной записи невозможно читать глазами при разборе.
        text = str(int(value)) if float(value).is_integer() else repr(float(value))
        self.lines.append(f"{name}{rendered} {text}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# --- сборщики. Каждый молчит, если собрать не удалось --------------------------


def _collect_build(out: Metrics) -> None:
    out.add("opencrm_up", 1, help_text="Приложение отвечает на запрос метрик.")
    try:
        from web.main import app as application

        version = application.version
    except Exception:  # noqa: BLE001 — версия не стоит отказа всего ответа
        version = "unknown"
    out.add(
        "opencrm_build_info",
        1,
        help_text="Версия приложения. Значение всегда 1, смысл — в метке.",
        labels={"version": version},
    )


def _collect_schema(out: Metrics) -> None:
    """Сошлась ли база с моделями.

    Отчёт снят на старте и лежит в состоянии приложения (web/main.py) — ровно
    как у `/api/v1/system/schema`. Пересчитывать его на каждый опрос нельзя:
    сверка обходит всю схему, а метрики спрашивают раз в полминуты вечно.
    """
    try:
        from web.main import app as application

        report = getattr(application.state, "schema_report", None)
        if report is None:
            return
        out.add(
            "opencrm_schema_ok",
            1 if report.ok else 0,
            help_text="1 — схема базы соответствует моделям, 0 — нет.",
        )
    except Exception:  # noqa: BLE001
        return


def _collect_database(out: Metrics) -> None:
    """Размер базы — на обоих движках, но спрашивается он по-разному.

    У SQLite ответ лежит в файловой системе, у MySQL — внутри сервера, и там его
    берёт репозиторий (`database/repositories/engine_info.py`): запросы живут
    только в `database/` (CLAUDE.md, раздел 3).

    Прежде на MySQL метрика просто ИСЧЕЗАЛА — молчание было выбрано осознанно
    (ноль на графике неотличим от «база исчезла»), но спросить сервер тогда было
    нечем. После переезда это молчание стало бы вечным: за ростом базы перестал
    бы следить кто бы то ни было ровно на том движке, где она и растёт.
    """
    settings = get_settings()
    if not settings.db_url.startswith("sqlite"):
        db = SessionLocal()
        try:
            razmer = engine_info.database_size_bytes(db)
        finally:
            db.close()
        if razmer:
            out.add(
                "opencrm_database_size_bytes",
                razmer,
                help_text="Размер базы вместе с индексами (оценка information_schema).",
            )
        return
    try:
        path = _sqlite_path(settings.db_url)
        if not path.is_file():
            return
        # Журнал WAL — часть базы, а не мусор рядом: в нём лежат последние
        # записанные страницы. Без него размер занижен ровно на свежие данные.
        total = path.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                total += sidecar.stat().st_size
        out.add(
            "opencrm_database_size_bytes",
            total,
            help_text="Размер файла базы вместе с журналом WAL.",
        )
    except OSError:
        return


def _collect_storage(out: Metrics) -> None:
    """Диск и медиа — глазами того самого процесса, который пишет файлы.

    Дублирует ли это node-exporter? Про свободное место — да, и намеренно:
    приложение блокирует загрузку файлов по СВОИМ порогам
    (`OPENCRM_DISK_MIN_FREE_MB`), и в разборе «почему не грузятся файлы» важно
    видеть ту же цифру, по которой оно приняло решение, а не соседнюю.
    """
    try:
        from core.services import storage_service

        usage = storage_service.disk_usage()
        out.add(
            "opencrm_disk_total_bytes", usage["total_bytes"], help_text="Размер раздела со storage."
        )
        out.add("opencrm_disk_free_bytes", usage["free_bytes"], help_text="Свободно на разделе.")
        out.add(
            "opencrm_uploads_blocked",
            0 if storage_service.has_room_for(0) else 1,
            help_text="1 — загрузка файлов остановлена нехваткой места.",
        )
        out.add(
            "opencrm_storage_bytes",
            storage_service.dir_size(get_settings().storage_dir, cache_key="storage"),
            help_text="Занято медиа, файлами клиентов, брендингом и аватарами.",
        )
    except (OSError, KeyError):
        return


def _collect_backups(out: Metrics) -> None:
    """Когда снята последняя копия и какого она размера.

    Файлы **только считаются и стятся**. Читать их нельзя: рядом с копией базы
    лежит `secret-*.env` с ключом подписи сессий (scripts/backup.sh), и
    заглядывать туда ради метрики незачем.

    Отсутствие копий — не ошибка сбора, а факт, и он тоже метрика: ноль в
    `opencrm_backup_count` отличается от пропавшего ряда, по которому не
    понять, сломался мониторинг или бэкап.
    """
    daily = _data_dir() / "backups" / "daily"
    try:
        copies = sorted(
            [p for p in daily.glob("db-*") if p.suffix in (".db", ".sql")],
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return

    out.add(
        "opencrm_backup_count",
        len(copies),
        help_text="Сколько ежедневных копий базы лежит на диске.",
    )
    if not copies:
        return
    last = copies[-1]
    try:
        stat = last.stat()
    except OSError:
        return
    out.add(
        "opencrm_backup_last_timestamp_seconds",
        int(stat.st_mtime),
        help_text="Когда снята последняя копия базы (unix-время).",
    )
    out.add(
        "opencrm_backup_last_size_bytes",
        stat.st_size,
        help_text="Размер последней копии базы.",
    )

    # Отчёт проверки годности (scripts/verify_backup.py). Копия, которую никто
    # не проверял, — это надежда, а не копия: оборванный `.backup` выглядит как
    # база и читается до первой битой страницы.
    report = _data_dir() / "backups" / "last-check.json"
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(payload.get("ok"), bool):
        out.add(
            "opencrm_backup_verified",
            1 if payload["ok"] else 0,
            help_text="1 — последняя копия прошла проверку годности, 0 — нет.",
        )


def _collect_deploy(out: Metrics) -> None:
    """Чем кончилось последнее обновление.

    Читается последняя строка `history.jsonl` — файла, который только
    дописывается (deploy/journal.py). Состояние (`state.json`) для этого не
    годится: оно перезаписывается и не помнит, каким был исход.

    Исход отдаётся набором рядов по одному на каждый известный статус, из
    которых ровно один равен единице. Так правило «деплой откатился» пишется
    сравнением с единицей, а не строковым сопоставлением, которого в PromQL нет.
    Метка берётся из `DEPLOY_STATUSES`, а не из файла: строка с чужим статусом
    иначе завела бы в Prometheus ряд с произвольным именем.
    """
    history = _updates_dir() / "history.jsonl"
    try:
        # Файл дописывается по строке на попытку и за годы не вырастет больше
        # чем на сотни килобайт — читаем целиком, это дешевле возни с seek.
        lines = [line for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return
    if not lines:
        return
    try:
        record = json.loads(lines[-1])
    except ValueError:
        return
    if not isinstance(record, dict):
        return

    status = str(record.get("status", ""))
    for known in DEPLOY_STATUSES:
        out.add(
            "opencrm_deploy_last_status",
            1 if known == status else 0,
            help_text="Исход последнего обновления: 1 у своего статуса, 0 у остальных.",
            labels={"status": known},
        )

    seconds = record.get("seconds")
    if isinstance(seconds, (int, float)):
        out.add(
            "opencrm_deploy_last_duration_seconds",
            float(seconds),
            help_text="Сколько длилось последнее обновление.",
        )

    # Время окончания берём по времени изменения файла: в самой записи его нет,
    # а дописывается она сразу по завершении попытки (deploy/journal.py).
    try:
        out.add(
            "opencrm_deploy_last_timestamp_seconds",
            int(history.stat().st_mtime),
            help_text="Когда закончилось последнее обновление (unix-время).",
        )
    except OSError:
        pass


#: Момент импорта модуля — он же момент старта процесса: маршруты собираются
#: при создании приложения, до первого запроса.
_STARTED = time.time()


def _collect_started(out: Metrics) -> None:
    """Когда поднялся процесс.

    По разнице этого значения с `time()` виден незамеченный перезапуск
    приложения: контейнер, который тихо падает и поднимается заново, снаружи
    выглядит работающим, а «время жизни» у него каждый раз начинается с нуля.
    """
    out.add(
        "opencrm_process_start_timestamp_seconds",
        int(_STARTED),
        help_text="Когда стартовал процесс приложения (unix-время).",
    )

def _collect_ratelimiter(out: Metrics) -> None:
    """Жив ли общий счётчик попыток.

    Две метрики, а не одна, и обе нужны. `opencrm_ratelimiter_shared` отвечает
    на вопрос «счётчик вообще общий?»: ноль означает, что он лежит в памяти
    процесса, и при нескольких процессах порог защиты от подбора молча
    умножается на их число. `opencrm_ratelimiter_unavailable_total` считает
    случаи, когда до Redis не достучались, — по нему видно аварию, которую
    иначе пришлось бы искать в логах: вход в этот момент отвечает 503.
    """
    from core import ratelimit, redis_client

    out.add(
        "opencrm_ratelimiter_shared",
        1 if redis_client.configured() else 0,
        help_text="1 — счётчик попыток общий на все процессы, 0 — в памяти процесса.",
    )
    out.add(
        "opencrm_ratelimiter_unavailable_total",
        ratelimit.unavailable_total(),
        help_text="Сколько раз ограничитель не достучался до общего счётчика.",
        kind="counter",
    )
    # Живой опрос Redis стоит ЗДЕСЬ, а не в `/healthz`, и это разница по цене
    # ошибки. `/healthz` опрашивает docker каждые десять секунд с потолком в
    # пять, и медленный ответ означает «контейнер приложения нездоров» — то
    # есть авария соседней службы роняла бы сайт (с остановленным контейнером
    # разрешение имени в этот потолок не укладывается, проверено). Здесь
    # медленный ответ стоит одного пропущенного среза метрик, и это правильная
    # цена за то, чтобы про лежащий Redis узнать без единого входа на сайт.
    if redis_client.configured():
        out.add(
            "opencrm_redis_up",
            1 if redis_client.ping() else 0,
            help_text="1 — Redis отвечает на ping, 0 — нет.",
        )


COLLECTORS = (
    _collect_build,
    _collect_started,
    _collect_ratelimiter,
    _collect_schema,
    _collect_database,
    _collect_storage,
    _collect_backups,
    _collect_deploy,
)


@router.get("", response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    """Метрики приложения в текстовом формате Prometheus.

    В схеме маршрут остаётся, хотя рассказать ему о себе почти нечего: ответ не
    JSON, и описывать в OpenAPI его форму бессмысленно. Прятать нельзя по другой
    причине — по схеме идёт перебор в `tests/test_modules.py`, которым
    проверяется, что выключенный блок действительно закрывает свои адреса.
    Скрытый маршрут выпал бы из перебора, и «блок мониторинга ничего не
    закрывает» осталось бы незамеченным.
    """
    out = Metrics()
    for collect in COLLECTORS:
        try:
            collect(out)
        except Exception:  # noqa: BLE001 — один сборщик не роняет весь отчёт
            continue
    return PlainTextResponse(out.render(), media_type=CONTENT_TYPE)
