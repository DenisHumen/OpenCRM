"""Состояние наблюдения — глазами приложения, изнутри сети compose.

Зачем это вообще нужно. Панель Grafana живёт вне приложения и своего порта
наружу не имеет намеренно (docker-compose.yml: `expose`, а не `ports`):
единственный вход — тот же nginx, что отдаёт сайт. У такого решения есть цена, и
12 августа 2026 её заплатили: все восемь контейнеров были подняты и здоровы, а
`/monitoring/` отвечал так, будто мониторинг выключен, — потому что nginx работал
с конфигом, отрендеренным шестью днями раньше, и блока `/monitoring/` в нём не
было вовсе. Опубликованный порт ломается ГРОМКО (соединение отвергнуто), путь
через nginx — ТИХО. Здесь эта тишина и превращается в слова.

**Ни одного запроса к базе.** Не только из-за границы `database/` (CLAUDE.md,
раздел 3), но и по существу: спрашивать нечего — всё, что нужно, знают сами
службы наблюдения, и знают о себе, а не о заявках.

**Ни одного секрета.** Пароль панели (`OPENCRM_GRAFANA_PASSWORD`) и токен
Telegram уезжают только в свои контейнеры, приложению их не передают — и
передавать нельзя. Поэтому «канал настроен» читается по ОТСУТСТВИЮ приёмника в
конфиге Alertmanager, а не по чтению токена, а про пароль экран говорит, где он
лежит, а не какой он.

**Отказ пробы — это строка «не отвечает», а не 500.** Отчёт о наблюдении,
падающий пятисоткой, — это наблюдение, которое выключило само себя. Плюс
практическое: перебор всех GET-адресов в `tests/test_modules.py` требует, чтобы
ни один не отвечал 500, и зовёт их десятки раз в окружении, где имён
`nginx`/`grafana`/`prometheus` не существует вовсе.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from config.settings import get_settings
from core.utils import now_utc

#: Адрес панели с точки зрения человека. Здесь же, а не во фронтенде: путь
#: задаётся конфигом nginx, а не экраном, и разъехаться им нельзя.
PANEL_PATH = "/monitoring/"

#: Имена служб в сети compose. Все контейнеры стоят в одной сети без явного
#: `networks:`, поэтому обращение идёт по именам служб и наружу не выходит ни
#: одним байтом — ни одного нового порта для этого не открывается.
GRAFANA_HEALTH = "http://grafana:3000/api/health"
PROMETHEUS_TARGETS = "http://prometheus:9090/api/v1/targets?state=any"
PROMETHEUS_ALERTS = "http://prometheus:9090/api/v1/alerts"
ALERTMANAGER_STATUS = "http://alertmanager:9093/api/v2/status"

#: Задание Prometheus, по которому видно, поднят ли профиль `monitoring-logs`
#: (docker/monitoring/prometheus/prometheus.yml.template). Без него молчит
#: правило про долю ответов 5xx: считать её не по чему.
LOGS_JOB = "nginx-logs"

#: Задание внешней проверки сайта. Его метка `instance` — единственное место,
#: откуда приложение вообще может узнать, ПО КАКОМУ адресу его проверяют:
#: `docker/.env` контейнеру приложения не примонтирован и примонтирован не будет
#: (там же лежат пароль панели и токен Telegram).
SITE_JOB = "site"

#: Сколько ждём каждую пробу. Все службы стоят рядом, в той же сети, и отвечают
#: за миллисекунды; секунда с небольшим — это уже «не отвечает», а не «медленно».
#: Держать больше нельзя: экран ждёт человека, а не наоборот.
PROBE_TIMEOUT = 1.2

#: Общий потолок на весь отчёт. Пробы идут разом, поэтому это страховка от
#: зависшего соединения, а не сумма таймаутов.
DEADLINE = 3.0

#: Насколько отчёт остаётся свежим. Состояние стека меняется командой на сервере,
#: то есть медленно; а экран может опрашиваться. Тот же порядок, что у кэша
#: состава блоков (`modules_service`, 2 секунды).
CACHE_SECONDS = 5.0

#: Последний отчёт и когда он снят. Кэш общий на процесс: отчёт ни от кого не
#: зависит — ни от сотрудника, ни от его прав, — и раскладывать его по
#: пользователям значило бы опрашивать стек по разу на каждого.
_cache: tuple[float, dict[str, Any]] | None = None


def invalidate() -> None:
    """Забыть отчёт. Нужна проверкам: иначе один прогон отвечает за другой."""
    global _cache
    _cache = None


async def status() -> dict[str, Any]:
    """Что сейчас со стеком наблюдения. Никогда не бросает."""
    global _cache
    cached = _cache
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    report = await _collect()
    _cache = (time.monotonic(), report)
    return report


def _blank() -> dict[str, Any]:
    """Отчёт, в котором не удалось ничего. Форма ответа одна при любом исходе.

    Ключи не исчезают, а приходят с честным «не отвечает»: пропавший ключ
    заставил бы экран знать, какие поля сегодня бывают, — тот же довод, по
    которому сводка без досок сужается, а не меняет форму (docs/11-modules.md).
    """
    return {
        "panel": {"path": PANEL_PATH, "state": "no_answer"},
        "grafana": {"reachable": False, "version": None},
        "targets": {"reachable": False, "up": 0, "total": 0, "down": []},
        # Куда идёт внешняя проверка сайта и проходит ли она. Отдельной строкой,
        # а не пунктом в `targets`: за NAT сервер не дозванивается до
        # собственного публичного адреса, проверку переводят на локальный — и
        # тогда она перестаёт видеть роутер. Сказать об этом обязан экран, иначе
        # отвалившийся проброс портов кладёт сайт для всего мира при зелёном
        # мониторинге.
        "site": {"url": None, "up": None},
        "alerts": {"reachable": False, "firing": []},
        "channel": {"reachable": False, "configured": None},
        "logs": {"on": None},
    }


async def _collect() -> dict[str, Any]:
    report = _blank()
    try:
        # `verify=False` — по той же причине, по которой `./opencrm.sh doctor`
        # ходит с `curl -k`: сертификат выписан на домен, а стучимся мы по имени
        # службы внутри сети compose. Проверять здесь нечего и не у кого: сеть
        # своя, наружу запрос не выходит.
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, follow_redirects=False, verify=False
        ) as client:
            await asyncio.wait_for(
                asyncio.gather(
                    _probe_panel(client, report),
                    _probe_grafana(client, report),
                    _probe_targets(client, report),
                    _probe_alerts(client, report),
                    _probe_channel(client, report),
                    return_exceptions=True,
                ),
                timeout=DEADLINE,
            )
    except Exception:  # noqa: BLE001 — отчёт о наблюдении не имеет права упасть
        pass
    report["checked_at"] = now_utc().isoformat()
    return report


# --- пробы. Каждая молчит, если не получилось --------------------------------


async def _probe_panel(client: httpx.AsyncClient, report: dict[str, Any]) -> None:
    """Самая важная проба: работает ли ПУТЬ, а не служба на его конце.

    Три исхода различает только она, и человек их сам не различит никак:

    * `open` — nginx довёл до Grafana;
    * `off` — конфиг свежий, а контейнеров нет (`@opencrm_monitoring_off`);
    * `stale` — блока `/monitoring/` в конфиге нет вовсе, запрос ушёл в
      `location /`, и приложение отдало SPA. Это и есть живой случай 12 августа:
      снаружи неотличимо от «мониторинг выключен».

    Стучимся на порт 80: он слушается в обоих шаблонах nginx. В варианте с TLS
    оттуда приходит 301 на https — тогда, и только тогда, повторяем по https.
    Схему НЕ берём из `base_url`: она говорит, каким сайт объявлен наружу, а
    здесь нужен тот конфиг, с которым nginx работает на самом деле.
    """
    state = await _panel_answer(client, "http")
    if state == "other_scheme":
        state = await _panel_answer(client, "https")
        if state == "other_scheme":
            state = "no_answer"
    report["panel"]["state"] = state


async def _panel_answer(client: httpx.AsyncClient, scheme: str) -> str:
    # Host подставляем свой: в шаблонах стоит `server_name ${OPENCRM_DOMAIN}`, и
    # запрос с чужим именем попал бы не в тот server-блок (а при HTTPS — ещё и
    # мимо сертификата).
    host = urlsplit(get_settings().base_url).hostname or "localhost"
    try:
        response = await client.get(
            f"{scheme}://nginx{PANEL_PATH}", headers={"Host": host}
        )
    except Exception:  # noqa: BLE001 — nginx рядом нет: dev, или он лежит
        return "no_answer"

    if response.is_redirect:
        # Grafana без сессии уводит на свою форму входа — это и есть «работает».
        if "/login" in response.headers.get("location", ""):
            return "open"
        # Всё остальное на этом месте — редирект самого nginx с http на https.
        return "other_scheme"

    body = response.text[:4000].lower()
    if response.status_code == 503 and "monitoring is switched off" in body:
        return "off"
    if response.status_code == 200:
        # Grafana отдаёт свой index.html; SPA приложения — свой. Различие
        # существенное: второе означает, что пути `/monitoring/` в конфиге нет.
        return "open" if "grafana" in body else "stale"
    return "no_answer"


async def _probe_grafana(client: httpx.AsyncClient, report: dict[str, Any]) -> None:
    """Жива ли сама панель. `/api/health` отвечает без пароля и называет версию."""
    payload = await _json(client, GRAFANA_HEALTH)
    if not isinstance(payload, dict):
        return
    version = payload.get("version")
    report["grafana"] = {
        "reachable": True,
        "version": version if isinstance(version, str) else None,
    }


async def _probe_targets(client: httpx.AsyncClient, report: dict[str, Any]) -> None:
    """Кого Prometheus видит, а кого нет.

    Сюда попадает и самодиагноз: при выключенном блоке `monitoring` цель
    `opencrm-app` красная, потому что `/api/v1/metrics` отвечает
    `module_disabled`, — и вместе с ней молча перестают работать правила про
    схему базы, резервные копии и откат обновления.

    Молчащие цели называем по заданию, а не по адресу: `app:8000` человеку
    ничего не говорит, а «opencrm-app» — говорит.
    """
    payload = await _json(client, PROMETHEUS_TARGETS)
    if not isinstance(payload, dict):
        return
    active = (payload.get("data") or {}).get("activeTargets")
    if not isinstance(active, list):
        return

    down: list[str] = []
    up = 0
    jobs: set[str] = set()
    zhivye: set[str] = set()
    site_url: str | None = None
    site_up: bool | None = None
    for target in active:
        if not isinstance(target, dict):
            continue
        labels = target.get("labels") or {}
        job = str(labels.get("job") or "?")
        jobs.add(job)
        zdorov = target.get("health") == "up"
        if zdorov:
            up += 1
            zhivye.add(job)
        elif job not in down:
            down.append(job)
        if job == SITE_JOB:
            # Целей у задания две (`/healthz` и `/`), адрес у них общий —
            # показываем его без пути, иначе строка на экране читается как
            # «проверяется только healthz».
            site_url = site_url or _origin(str(labels.get("instance") or ""))
            site_up = zdorov if site_up is None else (site_up and zdorov)

    report["targets"] = {
        "reachable": True,
        "up": up,
        "total": len(active),
        "down": down,
    }
    report["site"] = {"url": site_url, "up": site_up}
    # Поиск по логам — по ЖИВОЙ цели, а не по наличию задания в списке.
    #
    # Задание `nginx-logs` стоит в конфиге Prometheus безусловно: профиль
    # `monitoring-logs` меняет состав контейнеров, а не конфиг. Значит при
    # выключенных логах задание в списке ЕСТЬ, просто не отвечает, — и проверка
    # «есть ли оно» говорила «включён» всегда. Экран показывал бы разом
    # «Поиск по логам: включён» и «Цели молчат: nginx-logs», а человек выбирал
    # бы, какой из двух строк верить.
    report["logs"] = {"on": LOGS_JOB in zhivye}


async def _probe_alerts(client: httpx.AsyncClient, report: dict[str, Any]) -> None:
    """Что горит прямо сейчас — то есть почему пришло сообщение в Telegram."""
    payload = await _json(client, PROMETHEUS_ALERTS)
    if not isinstance(payload, dict):
        return
    raw = (payload.get("data") or {}).get("alerts")
    if not isinstance(raw, list):
        return

    firing = []
    for alert in raw:
        if not isinstance(alert, dict) or alert.get("state") != "firing":
            continue
        labels = alert.get("labels") or {}
        firing.append(
            {
                "name": str(labels.get("alertname") or "?"),
                "severity": str(labels.get("severity") or ""),
            }
        )
    report["alerts"] = {"reachable": True, "firing": firing}


async def _probe_channel(client: httpx.AsyncClient, report: dict[str, Any]) -> None:
    """Настроен ли канал оповещений — по конфигу, а не по секрету.

    Без токена entrypoint подставляет `alertmanager-silent.yml` с приёмником
    `nowhere`: тревоги видны на панели и не приходят никому. Отличить это от
    настроенного канала можно по самому конфигу, который Alertmanager отдаёт в
    своём состоянии; читать оттуда нечего, кроме факта, что приёмник есть.
    """
    payload = await _json(client, ALERTMANAGER_STATUS)
    if not isinstance(payload, dict):
        return
    config = (payload.get("config") or {}).get("original")
    report["channel"] = {
        "reachable": True,
        "configured": ("telegram_configs" in config) if isinstance(config, str) else None,
    }


def _origin(url: str) -> str | None:
    """`https://site.example/healthz` → `https://site.example`. Пустое — это None."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


async def _json(client: httpx.AsyncClient, url: str) -> Any:
    """GET с разбором JSON. Любой отказ — это `None`, а не исключение."""
    try:
        response = await client.get(url)
        if response.status_code != 200:
            return None
        return response.json()
    except Exception:  # noqa: BLE001 — служба не отвечает, и это ответ
        return None
