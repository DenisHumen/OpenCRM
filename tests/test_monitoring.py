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

import asyncio
import importlib
import re
from pathlib import Path

import httpx
import pytest

from core.services import monitoring_service
from tests.conftest import API as API_PREFIX

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


#: Службы, которым публикация порта БЕЗ привязки к адресу разрешена. Ровно одна:
#: nginx и есть сайт, он обязан слушать на всех интерфейсах.
PORTY_NA_VES_SVET = {"nginx"}

#: Адреса, привязка к которым равна её отсутствию.
VES_SVET = {"0.0.0.0", "::", "*", ""}


def _publikatsii(compose: str) -> dict[str, list[str]]:
    """Что каждая служба публикует на хост: {служба: [строка порта, ...]}.

    Разбор построчный, а не YAML-парсером — по тому же доводу, что и во всём
    файле: словарь зависимостей приложения не должен расти ради тестов.
    """
    naydeno: dict[str, list[str]] = {}
    sluzhba = ""
    v_portakh = False
    for stroka in compose.splitlines():
        imya = re.match(r"^  (\S+):\s*$", stroka)
        if imya:
            sluzhba = imya.group(1)
            v_portakh = False
            continue
        if re.match(r"^    ports:\s*$", stroka):
            v_portakh = True
            continue
        if v_portakh:
            zapis = re.match(r'^      - "([^"]+)"\s*$', stroka)
            if zapis:
                naydeno.setdefault(sluzhba, []).append(zapis.group(1))
                continue
            if stroka.strip() and not stroka.startswith("      "):
                v_portakh = False
    return naydeno


def _podstanovka(zapis: str) -> str:
    """`${VAR:-127.0.0.1}` → `127.0.0.1`, `${VAR}` → пустота.

    То же, что делает compose на установке, где docker/.env пуст, — а это самая
    частая установка и есть. Проверять надо ДЕЙСТВУЮЩЕЕ значение: умолчание
    `0.0.0.0` открывает панель всему свету ровно так же, как отсутствие привязки.
    """
    zapis = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", zapis)
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", zapis)


def test_port_publikuetsya_tolko_s_privyazkoy_k_adresu():
    """Открытая панель мониторинга — это карта всей системы, выданная любому.

    Прежний сторож считал блоки `ports:` и требовал ровно один, у nginx. Довод
    был верен ровно наполовину: он верен для публикации НА ВЕСЬ СВЕТ
    (`9080:3000` слушает на всех интерфейсах — панель без TLS и мимо
    единственной точки входа достаётся любому сканеру), но не для привязки к
    частному адресу за NAT. Заказчику нужен вход по http://10.0.0.130:9080/ из
    локальной сети, и запрещать это нечем.

    Поэтому запрет переехал с самого факта публикации на её ФОРМУ: у каждой
    опубликованной строки обязан быть явный адрес, и он не имеет права быть
    «любым». Ослабив сторожа в одном, обязаны сделать его строже в остальном —
    прежняя проверка `"3000:" not in compose` прошла бы мимо
    `"${OPENCRM_GRAFANA_BIND:-127.0.0.1}:9080:3000"` и стала бы мёртвой.
    """
    compose = _read(COMPOSE)
    publikuyut = _publikatsii(compose)

    assert "grafana" in publikuyut, "порт панели пропал — вход в неё остался один, через nginx"

    for sluzhba, zapisi in publikuyut.items():
        if sluzhba in PORTY_NA_VES_SVET:
            continue
        for zapis in zapisi:
            # Считаем ДЕЙСТВУЮЩЕЕ значение, с раскрытыми умолчаниями: правка
            # `${OPENCRM_GRAFANA_BIND:-0.0.0.0}` обязана ронять проверку, а по
            # тексту с переменной она выглядела бы привязкой.
            chasti = _podstanovka(zapis).split(":")
            assert len(chasti) == 3, (
                f"{sluzhba}: «{zapis}» публикуется без привязки к адресу — "
                "это все интерфейсы сразу, то есть весь интернет"
            )
            adres = chasti[0]
            assert adres not in VES_SVET, (
                f"{sluzhba}: «{zapis}» привязан к {adres or 'пустоте'} — это тот же весь свет"
            )

    # Умолчание — только петля, и названо явно: «привязка обязательна» не должна
    # держаться на том, что никто не тронул одно значение в docker/.env.example.
    assert "${OPENCRM_GRAFANA_BIND:-127.0.0.1}" in compose, (
        "умолчание привязки панели больше не 127.0.0.1"
    )

    # Остальные службы мониторинга не публикуют ничего вовсе — им и через nginx
    # ходить незачем, они разговаривают только внутри сети compose.
    for sluzhba in SERVICES:
        if sluzhba == "grafana":
            continue
        assert sluzhba not in publikuyut, f"наружу опубликован порт службы {sluzhba}"


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


def test_za_nat_proverka_idyot_po_imeni_a_ne_po_seromu_adresu():
    """Сервер за NAT не дозванивается до собственного публичного адреса.

    Живой случай 12 августа: частный адрес 10.0.0.130, имя ведёт на роутер,
    порты проброшены внутрь — и `curl https://<домен>/healthz` с самого сервера
    отказывает за 55 мс. Правило SiteDown в такой установке не может пройти
    НИКОГДА: тревога приходит при полностью живом сайте, и её перестают читать
    вместе с настоящими.

    Лечение обязано быть именно таким: имя остаётся в цели, а ведёт на локальный
    адрес записью в /etc/hosts контейнера проверки. Подстановка серого адреса в
    сам URL так не умеет — сертификат выписан на имя, и проверка его либо
    покраснеет, либо (что хуже) будет отключена.
    """
    compose = _read(COMPOSE)
    blok = compose.split("\n  blackbox:", 1)[1].split("\n  grafana:")[0]
    assert "extra_hosts:" in blok, (
        "проверке нечем узнать, что имя сайта живёт по локальному адресу — "
        "за NAT она будет красной всегда"
    )
    zapis = re.search(r'^\s+- "([^"]+)"\s*$', blok.split("extra_hosts:", 1)[1], re.M)
    assert zapis, "запись extra_hosts не найдена"
    # Делим по границе `}:${`, а не по первому двоеточию: оно стоит внутри самой
    # подстановки `${ИМЯ:-умолчание}`.
    para = re.fullmatch(r"(\$\{[^}]+\}):(\$\{[^}]+\})", zapis.group(1))
    assert para, (
        f"«{zapis.group(1)}»: запись обязана быть парой переменных «имя:адрес», "
        "иначе локальный адрес прописан в репозиторий"
    )
    imya, adres = para.group(1), para.group(2)

    # Обе половины с НЕПУСТЫМ умолчанием. Проверено вживую (docker compose v5.3.1):
    # пустое значение даёт не «мониторинг не поднялся», а отказ РАЗБОРА ФАЙЛА
    # («invalid additional host, missing IP»), причём независимо от профилей. То
    # есть на любой установке без мониторинга — а он выключен по умолчанию —
    # переставал бы подниматься сайт целиком. Это ровно то, что запрещает
    # CLAUDE.md: выключенный блок обязан исчезать, не задевая остальных.
    for chast in (imya, adres):
        umolchanie = re.fullmatch(r"\$\{[A-Z_]+:-([^}]*)\}", chast)
        assert umolchanie, f"«{chast}»: не переменная с умолчанием"
        assert umolchanie.group(1), (
            "пустое умолчание в extra_hosts валит разбор ВСЕГО compose-файла, "
            "включая установки без мониторинга"
        )

    # Смысл приёма — в том, что сертификат проверяется по-настоящему.
    blackbox = _read(MONITORING / "blackbox" / "blackbox.yml")
    assert "insecure_skip_verify: false" in blackbox, (
        "проверка сертификата отключена — просроченный или чужой станет невидим, "
        "а ради этого всё и затевалось"
    )

    # Цель остаётся именем, а не адресом: иначе сертификат не сойдётся.
    prometheus = _read(PROMETHEUS)
    site = prometheus.split("job_name: site", 1)[1].split("job_name:")[0]
    assert "__SITE_URL__" in site and not re.search(r"\d+\.\d+\.\d+\.\d+", site), (
        "в цель проверки подставлен адрес вместо имени — сертификат к нему не подойдёт"
    )


def test_ustanovshchik_stuchitsya_po_adresu_a_ne_verit_na_slovo():
    """Корень ложной тревоги: адрес спросили и поверили.

    Достижимость проверялась ровно одним способом — сравнением A-записи с
    внешним IP (`issue_certificate`), а при NAT оно как раз ПРОХОДИТ: A-запись
    ведёт на роутер, роутер и есть наш публичный адрес. Hairpin оставался
    невидимым, и `doctor` рядом проверял НЕПУСТОТУ СТРОКИ, стоя в пятнадцати
    строках от образцовой пробы панели запросом.

    То же правило, по которому в проекте живёт doctor: проверкой, а не
    обещанием.
    """
    script = _script()
    assert "probe_monitor_url()" in script, "проверка достижимости адреса не заведена"
    telo = re.search(r"\nprobe_monitor_url\(\) \{(.+?)\n\}", script, re.S)
    assert telo, "функция probe_monitor_url пропала"
    telo = telo.group(1)
    assert "curl" in telo, "адрес проверяется без единого запроса — это снова слово, а не дело"
    assert "OPENCRM_MONITOR_HOST" in telo and "OPENCRM_MONITOR_IP" in telo, (
        "нечем предложить локальный адрес: пара для extra_hosts не пишется"
    )
    assert "lan_ip" in telo, "локальный адрес не предлагается — человеку придётся угадывать"

    # Зовётся оттуда, где стек уже поднят и адрес мог смениться.
    nastroyka = re.search(r"\nconfigure_monitoring\(\) \{(.+?)\n\}", script, re.S)
    assert nastroyka and "probe_monitor_url" in nastroyka.group(1), (
        "установщик снова верит адресу на слово"
    )


def test_pro_nevidimyy_router_skazano_na_ekrane_a_ne_v_kontse_glavy():
    """Оговорка, без которой лечение опаснее болезни.

    Проверка по локальному адресу видит nginx, TLS и приложение — но не видит
    роутер. Отвалится проброс портов, и сайт ляжет для всего мира, пока
    мониторинг остаётся зелёным. Человек обязан узнать это там, где включает, а
    не в конце главы документации, до которой дойдёт не он и не сегодня.
    """
    script = _script()
    assert "monitor_local_warning()" in script, "оговорки про роутер нет в установщике"
    telo = re.search(r"\nmonitor_local_warning\(\) \{(.+?)\n\}", script, re.S).group(1)
    assert "роутер" in telo and "router" in telo, "оговорка не в обеих редакциях"
    assert "UptimeRobot" in telo, "не назван способ закрыть эту дыру"

    # И на экране мониторинга — той же мыслью, обеими редакциями.
    for value in _perevody("monSiteBlind"):
        assert "UptimeRobot" in value, "экран не называет, чем закрывается слепое пятно"
    assert "monSiteBlind" in _read(SCREEN), "оговорка объявлена, но экран её не показывает"


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


# --- включение блока обязано что-то ЗАЖИГАТЬ ---------------------------------
#
# Правило проекта: выключенный блок исчезает целиком, включённый — появляется.
# У мониторинга единственное видимое проявление — панель на /monitoring/, и
# ведёт туда nginx. Который про этот путь узнаёт из конфига, прочитанного при
# своём запуске, — то есть, возможно, полгода назад.


def _vetka_monitoringa(script: str, name: str) -> str:
    """Одна ветка `case` внутри `cmd_monitoring` — целиком, до своего `;;`."""
    telo = re.search(r"\ncmd_monitoring\(\) \{(.+?)\n\}", script, re.S)
    assert telo, "cmd_monitoring пропала"
    vetka = re.search(rf"\n        {name}\)\n(.*?)\n            ;;", telo.group(1), re.S)
    assert vetka, f"в cmd_monitoring нет ветки {name}"
    return vetka.group(1)


def test_vklyuchenie_monitoringa_daet_nginx_uznat_o_paneli():
    """Живой случай 12 августа 2026: все восемь контейнеров подняты и здоровы, а
    /monitoring/ отвечает так, будто мониторинг выключен.

    Причина не в мониторинге. `compose up -d` не пересоздаёт nginx — у него не
    меняются ни образ, ни описание службы, — а сам он за файлами не следит.
    Значит включение обязано попросить его перечитать конфиг; иначе панель
    заработает «когда-нибудь», а человек об этом не узнает никак.
    """
    script = _script()
    telo = re.search(r"monitoring_apply\(\)\s*\{(.+?)\n\}", script, re.S)
    assert telo, "monitoring_apply пропала"
    assert "reload_nginx" in telo.group(1), (
        "включение мониторинга не даёт nginx узнать о пути /monitoring/ — "
        "панель останется недостижимой при полностью здоровой Grafana"
    )
    # Все ветки команды проходят через monitoring_apply — значит и включение, и
    # выключение, и переключение логов.
    for vetka in ("on", "off", "logs"):
        assert "monitoring_apply" in _vetka_monitoringa(script, vetka), (
            f"ветка {vetka} поднимает стек мимо monitoring_apply и остаётся без reload_nginx"
        )


def test_vyklyuchenie_ostavlyaet_vnyatnoe_vyklyucheno_a_ne_502():
    """Выключение — та же беда с другой стороны.

    Пока nginx помнит конфиг с работающим `proxy_pass` в Grafana, снятой уже
    поимённо, /monitoring/ отдаёт 502 вместо честного «Monitoring is switched
    off». Внятный ответ описан в locations.inc и применяется тем же
    перечитыванием.
    """
    script = _script()
    vetka = _vetka_monitoringa(script, "off")
    assert "monitoring_remove" in vetka, "контейнеры не снимаются"
    assert "monitoring_apply" in vetka, "выключение идёт мимо monitoring_apply"
    # Цепочка целиком: off → monitoring_apply → reload_nginx. Без последнего
    # звена nginx помнит рабочий proxy_pass в снятую Grafana и отдаёт 502.
    primenenie = re.search(r"monitoring_apply\(\)\s*\{(.+?)\n\}", script, re.S)
    assert primenenie and "reload_nginx" in primenenie.group(1), (
        "после снятия контейнеров nginx не перечитывает конфиг — /monitoring/ будет отдавать 502 "
        "вместо честного «Monitoring is switched off»"
    )
    # Сам внятный ответ обязан существовать в конфиге.
    config = _read(LOCATIONS)
    assert "@opencrm_monitoring_off" in config
    assert "Monitoring is switched off" in config


def test_posle_vklyucheniya_chelovek_uznayot_adres_paneli_no_ne_parol():
    """Панель у Grafana своего порта не имеет намеренно — угадать адрес нельзя.

    Значит включение обязано его назвать: адрес сайта плюс /monitoring/ и логин
    admin. Пароль — не печатать: он лежит в docker/.env с правами 600, а вывод
    команды уходит в историю оболочки и в чужие логи.
    """
    script = _script()
    assert "monitoring_panel_hint" in _vetka_monitoringa(script, "on"), (
        "включение заканчивается словом «включён» и не говорит, куда идти смотреть"
    )

    telo = re.search(r"monitoring_panel_hint\(\)\s*\{(.+?)\n\}", script, re.S)
    assert telo, "подсказки с адресом панели нет"
    hint = telo.group(1)
    assert "/monitoring/" in hint, "в подсказке нет адреса панели"
    assert "admin" in hint, "в подсказке нет логина"
    assert "OPENCRM_GRAFANA_PASSWORD" in hint, "не сказано, где взять пароль"
    # Значение пароля не разворачивается: в подсказке только имя переменной и
    # команда смены. `env_get … OPENCRM_GRAFANA_PASSWORD` здесь означал бы
    # печать самого пароля.
    assert not re.search(r"env_get[^\n]*OPENCRM_GRAFANA_PASSWORD", hint), (
        "подсказка достаёт и печатает сам пароль"
    )


def test_doctor_proveryaet_panel_zaprosom_a_ne_obeshchaniem():
    """Строка «панель» обязана отвечать на вопрос «открывается?».

    Прежняя проверка смотрела на непустой OPENCRM_GRAFANA_PASSWORD и потому
    рапортовала «закрыта паролем» ровно тогда, когда панель была недостижима:
    контейнеры здоровы, пароль на месте, nginx работает с конфигом, в котором
    пути /monitoring/ ещё нет. Именно это и продержалось пять суток.
    """
    script = _script()
    telo = re.search(r"cmd_doctor\(\)\s*\{(.+?)\n\}", script, re.S)
    assert telo, "cmd_doctor пропала"
    body = telo.group(1)
    assert re.search(r"curl[^\n]*/monitoring/", body), (
        "диагностика не ходит на /monitoring/ — про доступность панели она только обещает"
    )
    # Ответ разбирается по существу: панель, честное «выключено» и всё
    # остальное — это три разных диагноза, а не один.
    assert "Monitoring is switched off" in body, (
        "диагностика не отличает честное «выключено» от протухшего конфига nginx"
    )


# --- то же обещание со стороны интерфейса ------------------------------------
#
# Предыдущий раздел чинит путь к панели на сервере. Здесь — вторая половина той
# же жалобы: «включаю Мониторинг в модулях, а на сайте ничего не меняется».
# Так и было: `monitoring` оставался единственным блоком системы, включение
# которого не зажигало ни пункта меню, ни экрана, ни ссылки, — а единственное,
# что он закрывал (`/api/v1/metrics`), снаружи не видно вовсе (nginx: deny all).
#
# Проверки читают `.tsx` как текст — тем же приёмом и с тем же разменом, что в
# `tests/test_screens.py`: собранного фронтенда в прогоне нет, а правило простое
# и проверяется чтением.

CRM = ROOT / "web" / "frontend" / "crm" / "src"
SIDEBAR = CRM / "components" / "Sidebar.tsx"
APP = CRM / "App.tsx"
SCREEN = CRM / "screens" / "Monitoring.tsx"
I18N = CRM / "lib" / "i18n.ts"

#: Адрес экрана. Именно `/server`, и это не вкусовщина — см. проверку ниже.
SCREEN_PATH = "/server"


def _perevody(key: str) -> list[str]:
    """Значения ключа в обеих редакциях словаря — английской и русской."""
    found = re.findall(rf'^  {key}:\s*\n?\s*"((?:[^"\\]|\\.)*)"', _read(I18N), re.M)
    assert len(found) == 2, (
        f"ключ {key} обязан быть в обеих редакциях i18n.ts, а найдено {len(found)}"
    )
    return found


def test_vklyuchennyy_blok_zazhigaet_punkt_menyu():
    """Пункт меню закрыт блоком И правом и ведёт на свой экран.

    Отбор в сайдбаре один на все пункты (`allowed`), поэтому достаточно, чтобы у
    пункта стояли оба поля: выключенный блок и нехватка права убирают его тем же
    правилом, каким убирают склад и почту.

    Право своё, а не `settings.manage`: карта сервера менеджеру не нужна, а
    дежурному по серверу нужна — и выдать её ролью, не отдавая заодно логотип
    сайта и переключатели блоков, можно только отдельным правом.
    """
    sidebar = _read(SIDEBAR)
    punkt = re.search(r"\{[^{}]*module:\s*\"monitoring\"[^{}]*\}", sidebar)
    assert punkt, "включение блока «Мониторинг» не зажигает пункта меню"
    body = punkt.group(0)
    assert 'perm: "monitoring.view"' in body, (
        "пункт «Мониторинг» открыт всем, кто видит «Админ», — карта сервера не для менеджера"
    )
    assert f'to: "{SCREEN_PATH}"' in body, f"пункт ведёт не на {SCREEN_PATH}"
    assert SCREEN.exists(), "экрана, на который ведёт пункт меню, нет"


def test_ekran_zakryt_i_blokom_i_pravom_v_tom_zhe_poryadke():
    """Порядок обёрток тот же, что порядок проверок на сервере: блок, потом право."""
    marshrut = re.search(
        r'<Route element=\{<ModuleRoute module="monitoring" />\}>(.*?)</Route>',
        _read(APP),
        re.S,
    )
    assert marshrut, "маршрут экрана не закрыт блоком monitoring"
    inside = marshrut.group(1)
    assert '<PermRoute perm="monitoring.view" />' in inside, (
        "маршрут закрыт блоком, но не правом: закладка откроет карту сервера кому угодно"
    )
    assert f'path="{SCREEN_PATH}"' in inside


def test_ekran_ne_stoit_na_adrese_kotoryy_zabiraet_nginx():
    """`/monitoring` принадлежит nginx, и экрану там не место.

    `location = /monitoring { return 301 /monitoring/; }` уводит в Grafana.
    Клиентская навигация внутри React сработала бы, а F5, закладка и ссылка из
    письма — нет: экран открывался бы через раз, и обнаружилось бы это только на
    боевом сервере.
    """
    config = _read(LOCATIONS)
    assert "location = /monitoring {" in config and "return 301 /monitoring/;" in config, (
        "перехвата больше нет — проверка смотрит не туда"
    )
    for path in (APP, SIDEBAR):
        text = _read(path)
        assert 'to: "/monitoring"' not in text, f"{path.name}: пункт уводит в Grafana, а не на экран"
        assert 'path="/monitoring"' not in text, (
            f"{path.name}: маршрут SPA встал под перехват nginx"
        )


def test_chelovek_znaet_chto_otkroet_i_chto_u_nego_sprosyat():
    """Панель — чужая программа со своим входом.

    Нажавший «Открыть панель» впервые упирается в форму пароля, которого у него
    нет на руках; сказать об этом обязан экран, а не догадка. Сам пароль при этом
    не печатается нигде: он уезжает только в контейнер Grafana, приложению его не
    передают.
    """
    screen = _read(SCREEN)
    assert 'target="_blank"' in screen and 'rel="noreferrer"' in screen, (
        "ссылка на панель открывается поверх CRM — из чужой программы обратно не вернуться"
    )
    for value in _perevody("monSignIn"):
        assert "admin" in value, "не сказано, под каким логином пустят"
        assert "OPENCRM_GRAFANA_PASSWORD" in value, "не сказано, где взять пароль"
    for value in _perevody("monOpensWhat"):
        assert "/monitoring/" in value, "не сказано, что именно откроется"


# --- ручка состояния ---------------------------------------------------------


@pytest.fixture()
def blok_monitoringa(root_client):
    """Переключатель блока, возвращающий состояние обратно.

    Состояние блоков глобальное и переживает файл, а база у проверок общая:
    оставленный включённым мониторинг заставил бы посторонние проверки ходить по
    сети к несуществующим именам.
    """
    from core.services import monitoring_service

    listed = root_client.get(f"{API_PREFIX}/modules").json()["items"]
    bylo = next(item["enabled"] for item in listed if item["key"] == MODULE_KEY)

    def pereklyuchit(enabled: bool) -> None:
        # Отчёт кэшируется на несколько секунд: без сброса вторая проверка
        # отвечала бы за первую.
        monitoring_service.invalidate()
        response = root_client.post(f"{API_PREFIX}/modules/{MODULE_KEY}", json={"enabled": enabled})
        assert response.status_code == 200, response.text

    yield pereklyuchit
    pereklyuchit(bylo)


def test_sostoyanie_zakryto_snachala_blokom_a_potom_pravom(
    root_client, manager_client, blok_monitoringa
):
    """Выключенный блок отвечает «блок выключен», а не «нет права».

    Порядок задан `require_perm` и переставлять его нельзя: иначе владелец пойдёт
    искать несуществующую ошибку в матрице доступов вместо того, чтобы включить
    переключатель.
    """
    put = f"{API_PREFIX}/system/monitoring"

    blok_monitoringa(False)
    zakryto = root_client.get(put)
    assert zakryto.status_code == 403
    assert zakryto.json()["error"]["code"] == "module_disabled"

    blok_monitoringa(True)
    otkaz = manager_client.get(put)
    assert otkaz.status_code == 403, otkaz.text
    assert otkaz.json()["error"]["code"] == "permission_denied"
    assert "monitoring.view" in otkaz.json()["error"]["message"]

    otvet = root_client.get(put)
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    assert set(telo) == {
        "checked_at",
        "panel",
        "grafana",
        "targets",
        "site",
        "alerts",
        "channel",
        "logs",
    }
    assert telo["panel"]["path"] == "/monitoring/"
    # Секретов в ответе нет и быть не может: приложению их не передают.
    assert "password" not in otvet.text.lower()


def test_nedostupnyy_stek_eto_stroka_a_ne_pyatisotka(root_client, blok_monitoringa):
    """В прогоне имён `nginx`, `grafana` и `prometheus` не существует вовсе.

    Ровно то же бывает и на живой машине: мониторинг выключен, контейнеров нет.
    Отчёт о наблюдении, отвечающий 500 из-за этого, — наблюдение, выключившее
    само себя; вдобавок перебор всех GET-адресов в `test_modules.py` требует,
    чтобы ни один адрес не отвечал пятисоткой.
    """
    blok_monitoringa(True)
    otvet = root_client.get(f"{API_PREFIX}/system/monitoring")
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    assert telo["panel"]["state"] in ("open", "off", "stale", "no_answer")
    # Форма ответа одна при любом исходе: ключ не исчезает, а приходит с честным
    # «не отвечает».
    assert isinstance(telo["grafana"]["reachable"], bool)
    assert isinstance(telo["targets"]["down"], list)
    assert telo["logs"]["on"] in (True, False, None)


# --- три беды, которые снаружи выглядят одинаково ----------------------------
#
# Самая ценная часть отчёта — разбор ответа на `/monitoring/`. Ради него всё и
# затевалось: «стек не поднят» и «nginx работает по старому конфигу» человек не
# различает ничем, и второе продержалось на боевом пять суток. Разбор проверяем
# подставным транспортом httpx — без единого контейнера и без сети.


def _razbor(handler) -> str:
    async def run() -> str:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            return await monitoring_service._panel_answer(client, "http")

    return asyncio.run(run())


def test_put_k_paneli_razlichaet_bedy_kotorye_snaruzhi_odinakovy():
    spa = (
        '<!doctype html><html><head><title>OpenCRM</title></head>'
        '<body><div id="root"></div></body></html>'
    )

    # Grafana без сессии уводит на свою форму входа — путь работает.
    grafana = _razbor(lambda _r: httpx.Response(302, headers={"location": "/monitoring/login"}))
    assert grafana == "open"

    # Конфиг свежий, контейнеров нет: locations.inc отвечает внятным «выключено».
    vyklyucheno = _razbor(
        lambda _r: httpx.Response(503, text="Monitoring is switched off (./opencrm.sh monitoring)")
    )
    assert vyklyucheno == "off"

    # Живой случай 12 августа: блока /monitoring/ в конфиге нет, запрос ушёл в
    # `location /`, приложение отдало SPA — снаружи неотличимо от «выключено».
    assert _razbor(lambda _r: httpx.Response(200, text=spa)) == "stale"
    assert _razbor(lambda _r: httpx.Response(200, text="<html><body>Grafana</body></html>")) == "open"

    def net_svyazi(_request):
        raise httpx.ConnectError("no such host")

    assert _razbor(net_svyazi) == "no_answer"


def test_proba_prohodit_redirekt_nginxa_s_http_na_https():
    """При включённом TLS порт 80 отвечает 301 — и это не ответ Grafana.

    Схему нарочно не берём из `base_url`: он говорит, каким сайт объявлен наружу,
    а нужен тот конфиг, с которым nginx работает на самом деле.
    """

    def handler(request):
        if request.url.scheme == "http":
            return httpx.Response(
                301, headers={"location": f"https://{request.url.host}/monitoring/"}
            )
        return httpx.Response(302, headers={"location": "/monitoring/login"})

    report = monitoring_service._blank()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            await monitoring_service._probe_panel(client, report)

    asyncio.run(run())
    assert report["panel"]["state"] == "open", (
        "проба остановилась на редиректе nginx и объявила рабочую панель недостижимой"
    )


# --- размер базы: спрашивается по-разному, но спрашивается всегда ------------
#
# На MySQL метрика `opencrm_database_size_bytes` ПРОСТО ИСЧЕЗАЛА. Молчание было
# выбрано осознанно — ноль на графике неотличим от «база исчезла», — но спросить
# сервер было нечем: ответ лежит внутри него, а запросы живут только в
# `database/`. После переезда на MySQL это молчание стало бы вечным: за ростом
# базы перестал бы следить кто бы то ни было ровно на том движке, где она растёт.


def test_razmer_bazy_na_sqlite_beryotsya_iz_fayla(root_client, blok_monitoringa):
    """У файловой базы ответ лежит в файловой системе — там и берём."""
    blok_monitoringa(True)
    otvet = root_client.get(f"{API_PREFIX}/metrics")
    assert otvet.status_code == 200
    assert "opencrm_database_size_bytes" in otvet.text, (
        "размер файловой базы пропал из метрик"
    )


def test_razmer_bazy_na_ne_mysql_ne_pridumyvaetsya():
    """`None`, а не ноль: ноль на графике выглядит как «база исчезла».

    Набор гоняется на SQLite, поэтому здесь проверяется сторона отказа —
    репозиторий обязан молчать про чужой движок, а не отдавать выдумку.
    """
    from database.repositories import engine_info
    from database.session import SessionLocal, engine

    db = SessionLocal()
    try:
        razmer = engine_info.database_size_bytes(db)
    finally:
        db.close()
    if engine.dialect.name == "mysql":
        assert razmer and razmer > 0, "сервер не сказал размер своей базы"
    else:
        # Чужой движок — молчание, а не выдумка: ноль на графике неотличим от
        # «база исчезла», а отсутствие точки видно как отсутствие.
        assert razmer is None


def test_na_mysql_razmer_sprashivaetsya_zaprosom_a_ne_molchaniem():
    """Живого MySQL в наборе нет, поэтому стережём связку, а не число.

    Ровно эта связка и отсутствовала: сборщик метрик выходил на первой строке,
    если движок не SQLite, и другого пути у размера не было вовсе.
    """
    istochnik = Path("web/api/routes/metrics.py").read_text(encoding="utf-8")
    sborshchik = istochnik[istochnik.index("def _collect_database("):]
    sborshchik = sborshchik[: sborshchik.index("\ndef ")]
    assert "engine_info.database_size_bytes" in sborshchik, (
        "на не-SQLite размер снова неоткуда взять — метрика исчезнет после переезда"
    )
    # И запрос при этом остаётся в database/: маршрут его не собирает сам.
    # (Слово `information_schema` в тексте подсказки к метрике — это проза, а не
    # запрос, поэтому ищем именно конструкции сборки запроса.)
    for zapreshcheno in ("select(", "db.execute", "text("):
        assert zapreshcheno not in sborshchik, (
            f"запрос переехал в маршрут ({zapreshcheno}) — это нарушение границы базы"
        )
