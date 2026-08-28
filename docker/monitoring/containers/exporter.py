"""Состояние контейнеров: жив, здоров, сколько ест, сколько раз перезапускался.

**Почему свой, а не cAdvisor.** Обычный ответ на этот вопрос — cAdvisor, и он
здесь был. Снят после проверки живьём: на Docker 29, где хранилище образов по
умолчанию переехало на containerd (`Storage Driver: overlayfs`), cAdvisor не
находит `/var/lib/docker/image/overlay2/layerdb` и **не заводит обработчик ни
для одного контейнера**. Наружу это выглядит хуже всего, что могло быть: служба
поднята, `/metrics` отвечает, `up` зелёный — и ровно один ряд, про машину
целиком. То есть «сколько ест каждый» и «кто перезапускается по кругу» молчат,
ничем себя не выдавая. Проверено на v0.49.1 и v0.52.1, с privileged и без.

Что получилось взамен:

* **счётчик перезапусков — настоящий.** Docker хранит `RestartCount` сам, и это
  ровно то число, которое нужно правилу о циклическом перезапуске. cAdvisor
  такого поля не знает, и там его приходилось угадывать по скачкам времени
  старта — способ, который путает перезапуск с обновлением;
* **здоровье — то самое, что показывает `docker ps`.** Healthcheck в
  docker-compose.yml уже написан, и второй способ спросить «здоров ли» тут не
  нужен;
* образ — `python:3.12-alpine`, полсотни мегабайт против 115 у cAdvisor, а
  памяти уходит меньше двадцати.

Цена — сокет docker, примонтированный **на чтение**. Она та же, что была у
cAdvisor: без сокета про контейнеры не узнать вовсе, а доступ к нему
равносилен root на машине. Размен назван в docs/08-deployment.md.

Наружу отдаётся только то, что видно в `docker ps`: имя службы, состояние,
числа. Ни переменных окружения, ни команд запуска, ни меток образа — в них
лежат пароли базы и ключ подписи сессий.
"""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import sys
import time
import urllib.parse

SOCKET_PATH = "/var/run/docker.sock"
LISTEN_PORT = 9110

#: Чьи контейнеры считаем своими. Совпадает с `name:` в docker-compose.yml.
PROJECT = "opencrm"

#: Сколько ждать docker. Он отвечает мгновенно, но подвиснуть на сокете, пока
#: Prometheus ждёт ответа, значит уронить сбор целиком.
TIMEOUT = 5.0


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP поверх unix-сокета: стандартная библиотека этого не умеет сама."""

    def __init__(self, path: str, timeout: float = TIMEOUT) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.path)
        self.sock = sock


def docker(path: str):
    """Запрос к API docker. Версия в адресе не указывается намеренно.

    Демон в этом случае отвечает своей текущей версией API, и одна и та же
    строка работает и на Docker 24, и на Docker 29. Прибитая версия — это ровно
    тот способ, которым такие вещи ломаются при обновлении сервера.
    """
    connection = UnixHTTPConnection(SOCKET_PATH)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return None
        return json.loads(body)
    finally:
        connection.close()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def add(self, name: str, value, *, help_text: str, kind: str, labels: dict) -> None:
        if name not in self._declared:
            self._declared.add(name)
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} {kind}")
        rendered = ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items())
        number = int(value) if float(value).is_integer() else float(value)
        self.lines.append(f"{name}{{{rendered}}} {number}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _started_at(raw: str) -> float:
    """Время старта из строки docker: `2026-08-07T13:33:25.123456789Z`.

    Наносекунды приходится обрезать до микросекунд — больше `fromisoformat` не
    принимает. Никогда не запускавшийся контейнер получает от docker
    `0001-01-01T00:00:00Z`; такой ряд не отдаём вовсе, ноль на графике выглядел
    бы как «запущен в семидесятом году».
    """
    if not raw or raw.startswith("0001-"):
        return 0.0
    text = raw.rstrip("Z")
    head, dot, frac = text.partition(".")
    if dot:
        text = f"{head}.{frac[:6]}"
    try:
        from datetime import datetime, timezone

        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


#: Сколько сборов сорвалось с прошлого запуска. Растёт, а не гасит всё разом.
SRYVOV = 0


def docker_myagko(path: str, chto: str):
    """Запрос к docker, который НЕ роняет весь сбор.

    Прежде любая заминка на одном контейнере выбрасывала исключение наверх, и
    наружу уходило `opencrm_containers_exporter_up 0` — то есть метрик не
    оставалось НИ ПО ОДНОМУ контейнеру. А заминки рядовые: `stats` докер
    считает сам, и на занятой машине (сборка образа, перезапуск стека) один
    ответ приходит секунды. Снято с боевого: три сорванных сбора подряд
    `TimeoutError('timed out')`, и в эти минуты панель контейнеров была пуста
    целиком — ровно тогда, когда на неё и смотрят.

    Частичный ответ лучше пустого: по остальным контейнерам данные верные, а
    о пропаже видно по счётчику срывов.
    """
    global SRYVOV
    try:
        return docker(path)
    except Exception as failure:  # noqa: BLE001 — сборщик не имеет права упасть
        SRYVOV += 1
        print(
            f"[containers-exporter] {chto} не ответил: {failure!r}",
            file=sys.stderr,
            flush=True,
        )
        return None


def collect() -> str:
    report = Report()
    report.add(
        "opencrm_containers_exporter_up",
        1,
        help_text="Сборщик состояния контейнеров отвечает.",
        kind="gauge",
        labels={},
    )

    # Второй фильтр не лишний, и это проверено живьём. Контейнер наследует
    # метки СВОЕГО ОБРАЗА, а образ приложения собран компоузом и метку проекта
    # несёт в себе. Поэтому любой разовый `docker run opencrm-app …` (отладка,
    # разовая команда, чужой скрипт) попадал в список наравне со службами и
    # приезжал в метрики под случайным именем вроде `inspiring_tesla`.
    # `container-number` метку ставит только компоуз и только настоящей службе.
    filters = urllib.parse.quote(
        json.dumps(
            {
                "label": [
                    f"com.docker.compose.project={PROJECT}",
                    "com.docker.compose.container-number",
                ]
            }
        )
    )
    # Список — единственный запрос, без которого сбора не бывает вовсе:
    # не зная контейнеров, отдавать нечего. Он же самый дешёвый.
    listing = docker_myagko("/containers/json?all=1&filters=" + filters, "список контейнеров")
    if not listing:
        return report.text()

    for item in listing:
        container_id = item.get("Id", "")
        name = (item.get("Names") or ["/?"])[0].lstrip("/")
        service = (item.get("Labels") or {}).get("com.docker.compose.service", "")
        labels = {"name": name, "service": service}

        running = 1 if item.get("State") == "running" else 0
        report.add(
            "opencrm_container_up",
            running,
            help_text="1 — контейнер запущен, 0 — нет.",
            kind="gauge",
            labels=labels,
        )

        details = docker_myagko(f"/containers/{container_id}/json", f"inspect {name}") or {}
        state = details.get("State") or {}

        # Счётчик перезапусков ведёт сам docker. Именно по нему видно цикл
        # перезапуска: контейнер поднимается, отвечает несколько секунд и падает
        # снова, а снаружи это выглядит как работающий сайт.
        report.add(
            "opencrm_container_restarts_total",
            details.get("RestartCount", 0),
            help_text="Сколько раз docker перезапускал контейнер.",
            kind="counter",
            labels=labels,
        )

        health = (state.get("Health") or {}).get("Status")
        if health:
            report.add(
                "opencrm_container_healthy",
                1 if health == "healthy" else 0,
                help_text="1 — проверка здоровья проходит; ряда нет, если проверки не задано.",
                kind="gauge",
                labels=labels,
            )

        started = _started_at(state.get("StartedAt", ""))
        if started:
            report.add(
                "opencrm_container_started_timestamp_seconds",
                started,
                help_text="Когда контейнер запущен в последний раз (unix-время).",
                kind="gauge",
                labels=labels,
            )

        if not running:
            continue

        # `one-shot` обязателен: без него docker снимает ДВА замера подряд с
        # секундной паузой, чтобы посчитать проценты за нас. На дюжине
        # контейнеров это дюжина секунд на каждый опрос — дольше, чем весь
        # таймаут сбора. Проценты нам и не нужны: наружу уходит накопленный
        # счётчик, а скорость по нему считает уже Prometheus (`rate`).
        stats = docker_myagko(
            f"/containers/{container_id}/stats?stream=false&one-shot=true",
            f"stats {name}",
        ) or {}
        cpu = ((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage")
        if isinstance(cpu, (int, float)):
            report.add(
                "opencrm_container_cpu_seconds_total",
                cpu / 1e9,
                help_text="Процессорное время контейнера, секунды.",
                kind="counter",
                labels=labels,
            )

        memory = stats.get("memory_stats") or {}
        used = memory.get("usage")
        if isinstance(used, (int, float)):
            # «Рабочий набор», а не `usage`: последний включает файловый кэш,
            # который ядро отдаст по первому требованию. Без вычета кэша любой
            # контейнер, читавший файлы, выглядит съевшим свой лимит целиком.
            inactive = (memory.get("stats") or {}).get("inactive_file", 0)
            report.add(
                "opencrm_container_memory_bytes",
                max(used - inactive, 0),
                help_text="Память контейнера без вытесняемого файлового кэша.",
                kind="gauge",
                labels=labels,
            )
        limit = memory.get("limit")
        if isinstance(limit, (int, float)) and limit:
            report.add(
                "opencrm_container_memory_limit_bytes",
                limit,
                help_text="Потолок памяти контейнера (mem_limit).",
                kind="gauge",
                labels=labels,
            )

    report.add(
        "opencrm_containers_exporter_failures_total",
        SRYVOV,
        help_text="Сколько запросов к docker сорвалось с запуска сборщика.",
        kind="counter",
        labels={},
    )
    return report.text()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        if self.path.rstrip("/") not in ("", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = collect().encode("utf-8")
        except Exception as failure:  # noqa: BLE001 — сборщик не имеет права упасть
            print(f"[containers-exporter] сбор не удался: {failure!r}", file=sys.stderr)
            body = b"opencrm_containers_exporter_up 0\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Молчим: Prometheus ходит сюда дважды в минуту вечно, и строка на
        каждый опрос — это мегабайты лога ни о чём."""


def main() -> None:
    print(f"[containers-exporter] слушаю :{LISTEN_PORT}, проект {PROJECT}", flush=True)
    while True:
        try:
            http.server.HTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
        except OSError as failure:
            print(f"[containers-exporter] {failure!r}, пробую снова", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
