"""Мониторинг: то, что можно проверить без единого контейнера.

Стек наблюдения проверяется живьём — иначе никак, — но живой прогон делают
руками и делают его не каждый день. Здесь сторожа на те свойства, потеря которых
не видна глазами и обнаруживается либо счётом за забитый диск, либо чужой
панелью мониторинга, открытой из интернета:

* `/metrics` отвечает и не выносит наружу ничего лишнего;
* наружу не опубликован ни один порт мониторинга;
* у каждого хранилища есть потолок;
* службы стоят под профилем, то есть выключаются вместе со своими контейнерами;
* обещанные поводы для тревоги существуют, и у каждого есть выдержка.

Конфиги читаются как текст, а не разбираются YAML-парсером, — тем же приёмом и
по той же причине, что в `test_deploy_config.py`: словарь зависимостей
приложения не должен расти ради тестов.
"""

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker" / "docker-compose.yml"
LOCATIONS = ROOT / "docker" / "nginx" / "templates" / "locations.inc"
LOGGING_INC = ROOT / "docker" / "nginx" / "templates" / "logging.inc"
MONITORING = ROOT / "docker" / "monitoring"
PROMETHEUS = MONITORING / "prometheus" / "prometheus.yml.template"
RULES = MONITORING / "prometheus" / "rules" / "opencrm.yml"
ALERTMANAGER = MONITORING / "alertmanager" / "alertmanager.yml.template"
LOKI = MONITORING / "loki" / "loki.yml"
PROMTAIL = MONITORING / "promtail" / "promtail.yml"
SCRIPT = ROOT / "opencrm.sh"

MODULE_KEY = "monitoring"

#: Службы мониторинга. Список руками — и это осознанно: он и есть то, что
#: проверяется. Новая служба, добавленная мимо него, обязана уронить проверку,
#: а не молча остаться без потолка памяти и без профиля.
SERVICES = (
    "prometheus",
    "alertmanager",
    "node-exporter",
    "containers",
    "blackbox",
    "grafana",
    "loki",
    "promtail",
)

EXPORTER = MONITORING / "containers" / "exporter.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _script() -> str:
    """Текст `opencrm.sh`.

    Внутри образа его нет: Dockerfile копирует только то, что нужно приложению.
    А тесты гоняются и там — их прогоняет автообновление перед каждым деплоем
    (`docker build --target tests`). Поэтому проверки установщика в образе
    пропускаются, а не краснеют: красный тест, который никто не может починить,
    приучает не смотреть на цвет.
    """
    if not SCRIPT.exists():
        pytest.skip("вне репозитория: opencrm.sh не входит в образ")
    return _read(SCRIPT)


# --- /metrics -----------------------------------------------------------------
#
# Маршрут закрыт блоком `monitoring`, а сам блок пишется в `core/modules.py`
# централизованно. Пока строки там нет, роутер не импортируется вовсе
# (`require_module` роняет неизвестный ключ на этапе сборки приложения — так и
# задумано, опечатка в имени блока не должна тихо открывать раздел). Поэтому
# проверка подставляет реестру недостающую запись сама и на время своего
# прогона: так она работает и до правки реестра, и после неё.


@pytest.fixture()
def metrics_module(monkeypatch):
    from core import modules as core_modules
    from core.services import modules_service

    if core_modules.get(MODULE_KEY) is None:
        extra = core_modules.Module(key=MODULE_KEY, default=False)
        fake = core_modules.MODULES + (extra,)
        monkeypatch.setattr(core_modules, "MODULES", fake)
        monkeypatch.setattr(core_modules, "BY_KEY", {m.key: m for m in fake})
        modules_service.invalidate()
        module = importlib.reload(importlib.import_module("web.api.routes.metrics"))
    else:
        module = importlib.import_module("web.api.routes.metrics")
    yield module
    modules_service.invalidate()


def _client(module, *, enabled: bool, monkeypatch):
    """Мини-приложение с одним этим роутером.

    Настоящее приложение здесь не годится: роутер в него подключают
    централизованно (web/main.py), и до этой правки его там нет. А проверять
    надо сам роутер — он уже написан.
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from core.exceptions import DomainError
    from core.services import modules_service

    monkeypatch.setattr(
        modules_service,
        "is_enabled",
        lambda _db, key: enabled if key == MODULE_KEY else True,
    )

    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error(_request, exc):  # noqa: ANN202
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(module.router, prefix="/api/v1")
    return TestClient(app)


def _samples(body: str) -> list[tuple[str, dict[str, str], str]]:
    """Разбор текстового изложения Prometheus: имя, метки, значение."""
    found = []
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)$", line)
        assert match, f"строка не разбирается как метрика: {line!r}"
        labels = {}
        if match.group(3):
            for pair in re.findall(r'(\w+)="([^"]*)"', match.group(3)):
                labels[pair[0]] = pair[1]
        found.append((match.group(1), labels, match.group(4)))
    return found


def test_metrics_otvechaet(metrics_module, monkeypatch):
    response = _client(metrics_module, enabled=True, monkeypatch=monkeypatch).get("/api/v1/metrics")
    assert response.status_code == 200, response.text
    # Версия формата в типе — часть протокола: без неё часть клиентов разбирает
    # ответ как обычный текст.
    assert "version=0.0.4" in response.headers["content-type"]

    samples = _samples(response.text)
    assert samples, "ответ пуст — собирать оказалось нечего"
    names = {name for name, _labels, _value in samples}
    assert "opencrm_up" in names
    for name in names:
        assert name.startswith("opencrm_"), f"чужое имя метрики: {name}"
    # У каждой метрики обязаны быть HELP и TYPE: без них человек, разбирающий
    # аварию в три часа ночи, гадает, что означает число.
    for name in names:
        assert f"# HELP {name} " in response.text, f"{name} без пояснения"
        assert f"# TYPE {name} " in response.text, f"{name} без типа"


def test_metrics_ne_vynosit_lishnego(metrics_module, monkeypatch):
    """Главное свойство этого маршрута.

    Метрики отдаются без сессии и лежат в Prometheus месяцами. Всё, что попало
    в метку, переживает и запрос, и того, кто его послал, — поэтому набор меток
    закрытый, а секретов и адресов в ответе нет ни в каком виде.
    """
    body = _client(metrics_module, enabled=True, monkeypatch=monkeypatch).get(
        "/api/v1/metrics"
    ).text

    from config.settings import get_settings

    settings = get_settings()
    for secret in (settings.secret_key, settings.ip_hash_salt, settings.root_password):
        assert secret and secret not in body, "в метрики попал секрет из настроек"
    assert settings.root_email not in body
    assert "@" not in body, "в метриках есть почтовый адрес"

    # Адрес клиента — то, ради чего в проекте вообще заведена соль хэширования
    # IP. Открытым текстом в соседнем хранилище он свёл бы эту защиту на нет.
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", body), "в метриках есть IP-адрес"

    #: Метки, которые вообще бывают. Список закрытый: `path`, `client`, `user`
    #: или `email` здесь означали бы журнал посещений, разложенный по рядам
    #: Prometheus, — с бесконечной кардинальностью и без срока давности.
    allowed = {"version", "status"}
    for name, labels, _value in _samples(body):
        extra = set(labels) - allowed
        assert not extra, f"{name}: метки вне закрытого списка: {sorted(extra)}"


def test_metrics_zakryvaetsya_vmeste_s_blokom(metrics_module, monkeypatch):
    """Выключенный блок исчезает целиком, включая прямые адреса."""
    response = _client(metrics_module, enabled=False, monkeypatch=monkeypatch).get(
        "/api/v1/metrics"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "module_disabled"


def test_metrics_ne_khodit_v_bazu(metrics_module):
    """Метрики опрашивают раз в полминуты вечно.

    Запрос, который стоит копейки на пустой базе, на населённой станет
    постоянной фоновой нагрузкой — и появится он незаметно, одной строкой.
    Заодно это та же граница, что стережёт `tests/test_db_boundary.py`:
    запросы живут только в `database/`.
    """
    source = _read(ROOT / "web" / "api" / "routes" / "metrics.py")
    body = source[source.index("from __future__") :]
    for forbidden in ("db.execute", "db.query", "select(", "session.execute"):
        assert forbidden not in body, f"метрики ходят в базу: {forbidden}"


# --- наружу ничего не опубликовано -------------------------------------------


def test_monitoring_ne_publikuet_ni_odnogo_porta():
    """Открытая панель мониторинга — это карта всей системы, выданная любому.

    Единственная служба с публикацией портов — nginx: через него и только через
    него виден весь сайт, включая Grafana на /monitoring/.
    """
    compose = _read(COMPOSE)
    assert compose.count("\n    ports:") == 1, (
        "порты публикует не только nginx — мониторинг обязан ходить через него"
    )
    for port in ("3000:", "9090:", "9093:", "9100:", "9115:", "3100:", "8080:"):
        assert f'"{port}' not in compose, f"наружу опубликован порт {port}"


def test_grafana_zakryta_parolem_iz_okruzheniya():
    """Пароль генерируется установщиком и лежит в docker/.env — как у MySQL.

    Записанный в репозиторий, он был бы одинаков на всех установках сразу, а
    открытый анонимный вход не нуждается и в этом.
    """
    compose = _read(COMPOSE)
    assert "GF_SECURITY_ADMIN_PASSWORD: ${OPENCRM_GRAFANA_PASSWORD" in compose
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "false"' in compose, "анонимный вход в панель открыт"
    assert 'GF_USERS_ALLOW_SIGN_UP: "false"' in compose, "в панели открыта регистрация"

    script = _script()
    assert "OPENCRM_GRAFANA_PASSWORD" in script, "установщик не заводит пароль панели"
    assert "seed_grafana_password" in script


def test_metrics_zakryty_snaruzhi_nginx():
    """Сессии у Prometheus нет, поэтому маршрут открыт без входа.

    Закрывает его граница сети: Prometheus ходит к приложению напрямую по сети
    compose, а снаружи адрес не отдаётся вовсе.
    """
    config = _read(LOCATIONS)
    block = re.search(r"location = /api/v1/metrics \{(.*?)\}", config, re.S)
    assert block, "nginx больше не закрывает /api/v1/metrics"
    assert "deny all" in block.group(1)


def test_grafana_vidna_tolko_cherez_nginx():
    config = _read(LOCATIONS)
    block = re.search(r"location /monitoring/ \{(.*?)\n\}", config, re.S)
    assert block, "нет прохода к панели через nginx"
    # Тот же урок, что у приложения: имя, записанное литералом, nginx резолвит
    # один раз при разборе конфига и запоминает адрес до перезапуска.
    assert re.search(r"proxy_pass\s+http://\$", block.group(1)), (
        "адрес Grafana записан литералом — после пересоздания контейнера будет 502"
    )
    assert "resolver 127.0.0.11" in config


# --- потолки хранения --------------------------------------------------------


def test_u_kazhdogo_khranilishcha_est_potolok():
    """Иначе первым, что сломает диск, окажется мониторинг диска.

    Логи контейнеров в проекте ограничены с самого начала (10 МБ × 3); ряды
    Prometheus и чанки Loki обязаны быть ограничены так же.
    """
    compose = _read(COMPOSE)
    assert "--storage.tsdb.retention.time=" in compose, "у Prometheus нет предела по времени"
    assert "--storage.tsdb.retention.size=" in compose, "у Prometheus нет предела по размеру"
    assert "--data.retention=" in compose, "у Alertmanager нет предела хранения"

    loki = _read(LOKI)
    assert "retention_period:" in loki, "у Loki нет срока хранения"
    assert "retention_enabled: true" in loki, (
        "без retention_enabled срок хранения Loki — просто число: чанки лежат вечно"
    )
    assert "ingestion_rate_mb:" in loki, (
        "нет предела скорости приёма — контейнер в цикле перезапуска забьёт диск"
    )


def test_u_kazhdoy_sluzhby_monitoringa_est_potolok_pamyati():
    """Без потолка OOM-killer уносит не наблюдателя, а самый крупный процесс —
    то есть сайт."""
    compose = _read(COMPOSE)
    assert compose.count("mem_limit:") >= len(SERVICES), (
        "какая-то служба мониторинга осталась без предела памяти"
    )


def test_logi_monitoringa_rotiruyutsya_kak_vse():
    compose = _read(COMPOSE)
    anchor = re.search(r"x-monitoring: &monitoring_defaults\n(.*?)\n\n", compose, re.S)
    assert anchor, "исчез общий якорь служб мониторинга"
    assert "logging: *logging" in anchor.group(1)
    assert 'profiles: ["monitoring"]' in anchor.group(1)
    assert compose.count("<<: *monitoring_defaults") == len(SERVICES), (
        "служба мониторинга подключена мимо общего якоря — без профиля и без ротации логов"
    )


# --- блок выключается вместе со своими контейнерами --------------------------


def test_monitoring_stoit_pod_profilem():
    """Как MySQL: выключенный блок — это не остановленные контейнеры, а
    контейнеры, которых в стеке нет."""
    compose = _read(COMPOSE)
    for service in ("loki", "promtail"):
        block = re.search(rf"\n  {service}:\n(.*?)(?=\n  \w|\Z)", compose, re.S)
        assert block, f"службы {service} нет в compose"
        assert 'profiles: ["monitoring-logs"]' in block.group(1), (
            f"{service} обязан жить в отдельном профиле: логи — самая тяжёлая часть набора"
        )


def test_vybor_profilya_ne_zatiraet_sosedniy():
    """COMPOSE_PROFILES общий на базу и на мониторинг.

    Запись целиком (`env_set COMPOSE_PROFILES monitoring`) молча вынесла бы из
    стека службу базы — и сайт после ближайшего `up` поднялся бы на пустом
    файле рядом.
    """
    script = _script()
    assert "compose_profile mysql on" in script
    assert "compose_profile mysql off" in script
    assert "compose_profile monitoring on" in script
    assert not re.search(r'env_set "\$DOCKER_ENV" COMPOSE_PROFILES (mysql|monitoring)', script), (
        "профиль снова пишется целиком, затирая чужое решение"
    )


def test_menu_i_komanda_monitoringa_na_meste():
    script = _script()
    assert "cmd_monitoring()" in script
    assert "monitoring) cmd_monitoring" in script, "команда monitoring не разбирается в main"
    assert "16) cmd_monitoring ;;" in script, "мониторинга нет в меню"
    # Профиль убирает службу из ОПИСАНИЯ стека, но уже поднятый контейнер сам не
    # исчезает — и «лишним» compose его не считает: службу он знает, просто не
    # выбрал. Проверено на стенде: после снятия профиля все контейнеры
    # мониторинга продолжали работать и есть память. Поэтому выключение сносит
    # их поимённо.
    assert "monitoring_remove" in script, "выключение мониторинга не снимает контейнеры"
    assert "compose rm -s -f" in script, "контейнеры мониторинга не удаляются, а только гасятся"
    for service in SERVICES:
        assert service in script[script.index("MONITORING_SERVICES=") :][:400], (
            f"{service} не попал в список снимаемых служб — останется работать после выключения"
        )


def test_pravila_perechityvayutsya_bez_peresozdaniya_kontenera():
    """Тот же урок, что стоил проекту молча не применявшегося конфига nginx.

    Файлы правил примонтированы из чекаута, `git checkout` меняет их мгновенно,
    а `docker compose up -d --build` пересоздаёт только службы с изменившимся
    описанием или образом — Prometheus не трогает вовсе. Новое правило после
    обновления не действовало бы, и узнать об этом можно было бы только по
    несработавшей тревоге.
    """
    for name in ("prometheus", "alertmanager"):
        entrypoint = _read(MONITORING / name / "entrypoint.sh")
        assert "kill -HUP 1" in entrypoint, f"{name} не перечитывает конфиг на ходу"
        assert "sleep 300" in entrypoint, f"{name} перечитывает конфиг слишком редко"


# --- оповещения --------------------------------------------------------------


def test_vse_obeshchannye_povody_dlya_trevogi_est():
    """Список поводов — это и есть обещание, данное владельцу сервера."""
    rules = _read(RULES)
    promised = {
        "SiteDown": "сайт не отвечает дольше двух минут",
        "HighErrorRate": "доля 5xx выше порога",
        "DiskAlmostFull": "места меньше 10%",
        "CertificateExpiringSoon": "сертификат истекает через 14 дней",
        "ContainerRestartLoop": "контейнер перезапускается циклически",
        "DeployRolledBack": "деплой откатился",
        "BackupTooOld": "бэкап не снимался больше суток",
    }
    for alert, why in promised.items():
        assert f"- alert: {alert}" in rules, f"пропал повод «{why}»"


def test_u_kazhdoy_trevogi_est_vyderzhka_i_obyasnenie():
    """Без выдержки одна неудачная проверка в момент обновления присылала бы
    «сайт лёг» на каждом деплое — и через неделю эти сообщения перестали бы
    читать. Без объяснения сообщение бесполезно в три часа ночи."""
    rules = _read(RULES)
    blocks = re.split(r"\n      - alert: ", rules)[1:]
    assert len(blocks) >= 7, "правил стало подозрительно мало — разбор смотрит не туда"
    for block in blocks:
        name = block.splitlines()[0].strip()
        assert re.search(r"^\s+for: ", block, re.M), f"{name}: нет выдержки"
        assert re.search(r"^\s+severity: (critical|warning)", block, re.M), f"{name}: нет важности"
        assert "summary:" in block, f"{name}: нечего показать человеку"


def test_dva_minuty_na_padenie_sayta():
    """Порог назван в задаче прямо: дольше двух минут."""
    rules = _read(RULES)
    block = rules.split("- alert: SiteDown", 1)[1].split("- alert:")[0]
    assert "probe_success" in block, "падение сайта ловится не внешней проверкой"
    assert re.search(r"for:\s*2m", block), "выдержка не равна двум минутам"


def test_proverka_sayta_idet_snaruzhi():
    """Тот самый случай: приложение отвечало на localhost, nginx не поднялся,
    443 не слушал никто. Проверка изнутри контейнера показала бы «всё хорошо»."""
    config = _read(PROMETHEUS)
    site = config.split("job_name: site", 1)[1].split("job_name:")[0]
    assert "__SITE_URL__" in site, "адрес проверки перестал быть внешним"
    assert "blackbox:9115" in site, "проверка идёт мимо blackbox"
    assert "app:8000" not in site and "http://nginx" not in site, (
        "сайт проверяется изнутри сети — такая проверка зелёная и при лежащем сайте"
    )
    # Адрес приходит снаружи, из docker/.env, и его туда кладёт установщик.
    assert "OPENCRM_MONITOR_URL" in _script()


def test_kanal_trevog_tot_zhe_bot_chto_u_obnovleniy():
    """Второй бот означал бы второй чат, который перестанут читать."""
    script = _script()
    assert "OPENCRM_UPDATE_TELEGRAM_TOKEN" in script, (
        "канал оповещений заводится заново, а не берётся у автообновления"
    )
    assert "sync_alert_channel" in script


def test_v_repozitorii_net_ni_tokena_ni_parolya():
    """Секреты подставляются при старте контейнера, а в git не попадают."""
    template = _read(ALERTMANAGER)
    assert "__TELEGRAM_TOKEN__" in template and "__TELEGRAM_CHAT__" in template
    # Токен бота Telegram выглядит как `123456789:AA...` — ищем именно форму, а
    # не конкретное значение: конкретное мы бы и не узнали.
    for path in MONITORING.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b", text), f"токен бота в {path.name}"


def test_alertmanager_podnimaetsya_i_bez_kanala():
    """Alertmanager с пустым токеном не стартует и уходит в цикл перезапуска —
    то есть ломает ровно то правило, которое сам должен сторожить."""
    entrypoint = _read(MONITORING / "alertmanager" / "entrypoint.sh")
    assert "alertmanager-silent.yml" in entrypoint
    assert (MONITORING / "alertmanager" / "alertmanager-silent.yml").is_file()


# --- логи --------------------------------------------------------------------


def test_v_zhurnale_nginx_net_ni_adresov_ni_tokenov():
    """Логи лежат в Loki неделю; всё, что туда попало, переживает запрос.

    Адрес клиента проект хэширует даже в своём журнале просмотров — класть его
    открытым текстом в соседнее хранилище значило бы обойти собственную защиту
    сбоку. А `/b/<токен>` — это работающая без входа ссылка на чужую доску.
    """
    logging_inc = _read(LOGGING_INC)
    fmt = logging_inc[logging_inc.index("log_format opencrm_json") :]
    for leak in ("$remote_addr", "$http_x_forwarded_for", "$http_user_agent",
                 "$http_referer", "$query_string", "$request_uri", "$args"):
        assert leak not in fmt, f"в журнал доступа попадает {leak}"
    # Путь пишется через карту, которая обрезает адреса с секретами.
    assert "$opencrm_log_path" in fmt
    assert '"~^/b/"' in logging_inc, "ссылка на витрину попадает в лог целиком"

    # Формат обязан быть подключён обоими шаблонами, иначе nginx не поднимется
    # вовсе: `access_log ... opencrm_json` сошлётся на неизвестный формат.
    for name in ("http.conf.template", "https.conf.template"):
        template = _read(ROOT / "docker" / "nginx" / "templates" / name)
        assert "include /opencrm/templates/logging.inc;" in template, name
    assert "access_log /var/log/nginx/access.log opencrm_json;" in _read(LOCATIONS)


def test_sostoyanie_konteynerov_beryotsya_u_samogo_dockera():
    """Счётчик перезапусков ведёт docker, и брать его надо у него.

    Прежде здесь стоял cAdvisor. Он снят после проверки живьём: на Docker 29,
    где хранилище образов переехало на containerd, он не заводит обработчик ни
    для одного контейнера и отдаёт ровно один ряд — про машину целиком. При этом
    служба поднята, `/metrics` отвечает, цель зелёная. Молчание, неотличимое от
    исправной работы, — худший из возможных исходов для мониторинга.
    """
    source = EXPORTER.read_text(encoding="utf-8")
    assert "RestartCount" in source, "перезапуски снова угадываются, а не берутся у docker"
    assert "one-shot=true" in source, (
        "без one-shot docker делает два замера с паузой в секунду на каждый контейнер"
    )
    # Версия API в адресе — это способ сломаться при обновлении сервера.
    assert not re.search(r'"/v1\.\d+/', source), "версия API docker прибита гвоздями"

    rules = _read(RULES)
    assert "opencrm_container_restarts_total" in rules
    assert "container_start_time_seconds" not in rules, (
        "правило снова считает перезапуски по времени старта — оно не отличит "
        "перезапуск от штатного обновления"
    )


def test_sborshchik_konteynerov_ne_vynosit_nastroek():
    """В переменных окружения контейнеров лежат пароль базы и ключ подписи
    сессий, а в метках образа — что угодно. Наружу уходит только то, что и так
    видно в `docker ps`."""
    source = EXPORTER.read_text(encoding="utf-8")
    for leak in ('"Env"', "get(\"Env\")", '"Cmd"', "Entrypoint", "docker.sock:rw"):
        assert leak not in source, f"сборщик читает лишнее: {leak}"
    # Единственная метка, которую он берёт у docker, — имя службы compose.
    labels = re.findall(r'Labels.*?\.get\("([^"]+)"', source)
    assert labels == ["com.docker.compose.service"], labels

    compose = _read(COMPOSE)
    block = re.search(r"\n  containers:\n(.*?)(?=\n  \w|\Z)", compose, re.S)
    assert block, "службы containers нет в compose"
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in block.group(1), (
        "сокет docker примонтирован на запись — сборщику метрик это не нужно"
    )


def test_doli_otvetov_schitayutsya_po_zhurnalu_nginx():
    """Только nginx видит 502: приложение о них не знает по определению — его в
    этот момент нет."""
    promtail = _read(PROMTAIL)
    assert "opencrm_nginx_responses" in promtail
    assert "source: status" in promtail, (
        "счётчик щёлкает и на строках журнала ошибок — знаменатель доли 5xx будет врать"
    )
    rules = _read(RULES)
    assert "promtail_custom_opencrm_nginx_responses_total" in rules, (
        "правило про долю 5xx считает по другому имени, чем то, которое рождается"
    )


def test_u_kazhdogo_khranilishcha_est_vladelets():
    """Каталог под данные службы обязан достаться тому, от кого она работает.

    Это тот отказ, который свалил стек на боевом сервере 12 августа 2026.
    Prometheus, Alertmanager, Grafana и Loki compose запускает от `OPENCRM_UID`
    (`user:` у каждой), а каталоги под их данные создаёт `mkdir` от того, кто
    запустил установку, — под `sudo` это root. Дальше все четверо падают на
    первой же записи в собственное хранилище:

        prometheus   open /prometheus/queries.active: permission denied → panic
        grafana      GF_PATHS_DATA='/var/lib/grafana' is not writable
        loki         mkdir /loki/rules: permission denied

    Снаружи это выглядит как «мониторинг не поднялся» и цикл перезапусков, и
    про права там нет ни слова. Данные приложения уцелели только потому, что
    `data` и `storage` установщик чинит отдельной строкой, — а каталоги
    мониторинга в тот список не попали.

    Сторож механический: берём из compose ВСЕ каталоги, которые монтируются из
    `$OPENCRM_HOME/monitoring/`, и требуем, чтобы каждый был назван в
    `own_monitoring_dirs`. Пятая служба со своим хранилищем, добавленная
    завтра, обязана попасть в тот же список — иначе она повторит эту историю
    ровно так же молча.
    """
    compose = _read(COMPOSE)
    script = _script()

    # Из compose приезжают и монтирования конфигов из чекаута (`./monitoring/…`),
    # у них нет своего состояния. Оставляем только те, что идут из дома
    # состояния. Ищем построчно, а не одним выражением: путь записан как
    # `${OPENCRM_HOME:-${HOME}/opencrm}`, и вложенная скобка ломает любую
    # попытку описать его регулярным выражением коротко.
    iz_doma = set()
    for _stroka in compose.splitlines():
        if "OPENCRM_HOME" not in _stroka or "/monitoring/" not in _stroka:
            continue
        _sovpalo = re.search(r"/monitoring/([a-z-]+):", _stroka)
        if _sovpalo:
            iz_doma.add(_sovpalo.group(1))
    assert iz_doma, "в compose не нашлось ни одного каталога состояния мониторинга"

    telo = re.search(r"own_monitoring_dirs\(\)\s*\{(.+?)\n\}", script, re.S)
    assert telo, "функция own_monitoring_dirs пропала — чинить владельца стало некому"
    nazvany = set(re.findall(r"[a-z-]+", telo.group(1)))

    zabyty = sorted(iz_doma - nazvany)
    assert not zabyty, (
        "каталоги состояния есть в compose, но им не выставляют владельца: "
        + ", ".join(zabyty)
        + ". Служба упадёт на первой записи в своё же хранилище."
    )

    assert "chown" in telo.group(1), (
        "own_monitoring_dirs только создаёт каталоги — а падало именно на владельце"
    )


def test_vladelets_vystavlyaetsya_pered_kazhdym_podyomom():
    """Починка обязана срабатывать и на уже сломанной установке.

    Один раз при установке — мало: мониторинг включают позже, и человек с
    поднятым стеком должен чинить его той же командой, которой включал, а не
    походом в консоль с `chown`. Поэтому владелец выставляется перед каждым
    подъёмом, а не единожды.
    """
    script = _script()
    telo = re.search(r"monitoring_apply\(\)\s*\{(.+?)\n\}", script, re.S)
    assert telo, "monitoring_apply пропала"
    assert "own_monitoring_dirs" in telo.group(1), (
        "перед подъёмом стека владельца никто не чинит — сломанная установка "
        "останется сломанной после `./opencrm.sh monitoring on`"
    )
