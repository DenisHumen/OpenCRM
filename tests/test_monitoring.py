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
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

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
    "db-exporter",
    "redis-exporter",
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
    (образ `--target tests` рядом с базой). Поэтому проверки установщика в образе
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
    #:
    #: `vid` — вид события безопасности. Он в списке потому, что берётся из
    #: ЗАКРЫТОГО набора `core.bezopasnost.VIDY` (семь значений), а не из того,
    #: что прислал посетитель: снаружи повлиять на это имя нельзя никак. Именно
    #: это и отличает допустимую метку от недопустимой — не смысл, а то, кто
    #: задаёт её значения. Проверка ниже держит границу набора отдельно.
    allowed = {"version", "status", "vid"}
    for name, labels, _value in _samples(body):
        extra = set(labels) - allowed
        assert not extra, f"{name}: метки вне закрытого списка: {sorted(extra)}"
        # Само ЗНАЧЕНИЕ `vid` тоже обязано быть из набора, а не любым словом.
        # Разрешив метку, легко потом начать класть в неё что придётся, и
        # список выше останется на месте, перестав что-либо стеречь.
        if "vid" in labels:
            from core.bezopasnost import VIDY

            assert labels["vid"] in VIDY, (
                f"{name}: vid={labels['vid']!r} не из закрытого набора "
                f"core.bezopasnost.VIDY — метка перестала быть перечислением"
            )


def test_metrics_zakryvaetsya_vmeste_s_blokom(metrics_module, monkeypatch):
    """Выключенный блок исчезает целиком, включая прямые адреса."""
    response = _client(metrics_module, enabled=False, monkeypatch=monkeypatch).get(
        "/api/v1/metrics"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "module_disabled"


def test_ochered_razzhatiya_vidna_v_metrikakh(metrics_module, monkeypatch):
    """Упёршаяся очередь была видна только по жалобе «картинки не грузятся».

    Четыре ряда: занято, предел, общий ли предел и держался ли он в памяти
    процесса в последнюю минуту. Ряд без HELP/TYPE уже ловит общая проверка.
    """
    body = _client(metrics_module, enabled=True, monkeypatch=monkeypatch).get(
        "/api/v1/metrics"
    ).text
    names = {name for name, _labels, _value in _samples(body)}
    for name in (
        "opencrm_decode_queue_busy",
        "opencrm_decode_queue_limit",
        "opencrm_decode_queue_shared",
        "opencrm_decode_queue_recently_local",
    ):
        assert name in names, f"очередь разжатия не отдаёт {name}"
    predel = next(int(v) for n, _l, v in _samples(body) if n == "opencrm_decode_queue_limit")
    from core.services import media_service

    assert predel == media_service.ODNOVREMENNO


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


# --- версия панели и её настройки --------------------------------------------
#
# Панель — единственная служба набора, которую видно из интернета, и
# единственная, куда входят паролем. Поэтому у неё свои сторожа: на версию, на
# срок жизни сессии и на то, что она не разговаривает с чужими серверами.

#: Ветки Grafana, которые ЖИВЫ — то есть получают исправления. Список снят с
#: реестра 14.08.2026: 12.4.8, 13.0.6 и 13.1.3 опубликованы одним днём
#: (07.08.2026), 12.3.10 — тремя днями раньше. 11.6 не попала ни в эту волну,
#: ни в июльскую: её последняя заплатка 11.6.16 от 23.06.2026. Ветки 12.0, 12.1
#: и 12.2 не чинятся с 2025 года.
#:
#: Список руками и должен устареть — в этом его смысл. Тег вне списка означает
#: «сходите в реестр и посмотрите, что чинится сейчас», а не «поправьте тест».
VETKI_S_ISPRAVLENIYAMI = {"12.3", "12.4", "13.0", "13.1"}


def _grafana_blok(compose: str) -> str:
    """Описание службы grafana целиком."""
    assert "\n  grafana:" in compose, "службы grafana больше нет в compose"
    return compose.split("\n  grafana:", 1)[1].split("\n  loki:", 1)[0]


def _srok_v_sekundy(znachenie: str) -> int:
    """`7d` → 604800. Grafana понимает s/m/h/d, других единиц у неё нет.

    Считаем в секундах, а не в часах: `30m` при пересчёте в часы превратилось бы
    в ноль, и сравнение двух сроков между собой начало бы врать.
    """
    sovpalo = re.fullmatch(r"(\d+)([smhd])", znachenie)
    assert sovpalo, f"«{znachenie}»: не срок в понятной Grafana записи"
    mnozhitel = {"s": 1, "m": 60, "h": 3600, "d": 86400}[sovpalo.group(2)]
    return int(sovpalo.group(1)) * mnozhitel


def test_versiya_paneli_zakreplena_i_vetka_chinitsya():
    """Беда была не в номере версии, а в том, что ветка перестала чиниться.

    `11.5.2` простояла здесь полтора года. За это время в ветке 11.5 накопилось
    шесть открытых CVE, включая CVE-2025-3260 (8.3) и CVE-2025-4123 (7.6, XSS
    плюс чтение изнутри сети), и чинить её перестали вовсе. Снаружи это никак
    не видно: панель работает, `/api/health` отвечает 200, обновления приезжают.
    Единственный признак — в реестре, и смотреть туда некому.

    Отсюда сторож не на номер, а на ВЕТКУ. Он краснеет и от возврата к 11.5.2, и
    от переезда на любую другую ветку, за которой больше никто не следит.
    """
    compose = _read(COMPOSE)
    sovpalo = re.search(r"^    image: grafana/grafana:(\S+)$", compose, re.M)
    assert sovpalo, "образ панели больше не закреплён явным тегом"
    teg = sovpalo.group(1)

    # `latest` — это «версия меняется сама, при первом же `pull`». Для службы,
    # у которой схема своей базы едет только вперёд, это способ однажды
    # обновиться на мажор через два и узнать об этом по лежащей панели.
    chasti = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", teg)
    assert chasti, (
        f"«{teg}»: версия панели обязана быть точной, до номера заплатки — "
        "плавающий тег меняет версию сам, а откат схемы у Grafana не работает"
    )
    vetka = f"{chasti.group(1)}.{chasti.group(2)}"
    assert vetka in VETKI_S_ISPRAVLENIYAMI, (
        f"ветка {vetka} не значится живой. Либо это откат на снятую с поддержки "
        f"версию, либо список устарел — посмотрите в реестре, на какие ветки "
        f"выходят исправления сейчас, и обновите VETKI_S_ISPRAVLENIYAMI вместе "
        f"с доводом в docker-compose.yml. Живыми считались: "
        f"{sorted(VETKI_S_ISPRAVLENIYAMI)}"
    )


def test_sessiya_paneli_ne_zhivyot_mesyats():
    """Обе высокие CVE этой панели срабатывают только у авторизованной жертвы.

    Умолчание Grafana — cookie на 30 суток (замерено: `Max-Age=2592000`), то
    есть владелец авторизован практически всегда, и «жертва должна быть
    авторизована» перестаёт быть препятствием. Предел жизни сессии — это и есть
    то, чем такое окно закрывают, и стоить он не стоит ничего.
    """
    blok = _grafana_blok(_read(COMPOSE))
    predely = dict(
        re.findall(r"^      GF_AUTH_LOGIN_MAXIMUM_(\w+)_DURATION: (\S+)$", blok, re.M)
    )
    assert set(predely) == {"LIFETIME", "INACTIVE_LIFETIME"}, (
        f"у сессии панели нет предела жизни: {sorted(predely)}. Без него cookie "
        "живёт месяц, и авторизованная жертва для XSS есть всегда"
    )
    vsego = _srok_v_sekundy(predely["LIFETIME"])
    bezdeystvie = _srok_v_sekundy(predely["INACTIVE_LIFETIME"])
    assert vsego <= 7 * 86400, (
        f"сессия живёт {vsego // 86400} суток — это снова почти месяц"
    )
    assert bezdeystvie <= vsego, (
        "предел бездействия больше общего предела — вторая величина не действует"
    )

    # Защита от подбора у Grafana своя и включена сама. А вот блокировку ПО
    # АДРЕСУ включать нельзя: снаружи панель видна только через nginx, и все
    # запросы приходят с одного адреса контейнера — блокировка закрыла бы вход
    # владельцу после пяти чужих неудачных попыток.
    assert "GF_SECURITY_DISABLE_IP_ADDRESS_LOGIN_PROTECTION" not in blok, (
        "включена блокировка входа по адресу: за nginx все запросы идут с одного "
        "адреса, и первый же подбиратель запрёт владельца снаружи собственной панели"
    )


def test_panel_ne_khodit_na_chuzhie_servery():
    """Обещание «никаких обращений наружу» держится не тремя строками, а пятью.

    Три первых (телеметрия, проверка обновлений, новости) были и раньше, и всё
    это время неправдой: Grafana с 11.5 при первом старте САМА тянет плагины с
    grafana.com, молча и мимо всех `check_for_updates: false`. Замерено на
    стенде: 11.5.2 приносит два плагина и 23 МБ, 12.4.8 — четыре и 47,5 МБ,
    и всё это ложится в каталог состояния.

    Пятая строка — про снимок дашборда: «опубликовать» отправляет содержимое
    панели на snapshots.raintank.io, то есть карту системы третьей стороне.
    """
    blok = _grafana_blok(_read(COMPOSE))
    for peremennaya, chem_grozit in (
        ("GF_ANALYTICS_REPORTING_ENABLED", "телеметрия"),
        ("GF_ANALYTICS_CHECK_FOR_UPDATES", "проверка обновлений"),
        ("GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES", "проверка обновлений плагинов"),
        ("GF_NEWS_NEWS_FEED_ENABLED", "новости на стартовом экране"),
        ("GF_SNAPSHOTS_EXTERNAL_ENABLED", "публикация снимка дашборда наружу"),
    ):
        assert f'{peremennaya}: "false"' in blok, f"наружу открыто: {chem_grozit}"
    assert 'GF_PLUGINS_PREINSTALL_DISABLED: "true"' in blok, (
        "панель снова тянет плагины с grafana.com при старте — это и обращение "
        "наружу, и десятки мегабайт в каталоге состояния, и неудачный старт там, "
        "где исходящие закрыты"
    )


def test_zashchita_soderzhimogo_i_cookie_paneli_na_meste():
    """Обе высокие CVE ветки 11.5 — это XSS, и обе лечатся одним и тем же.

    CSP у Grafana своя, но по умолчанию выключена: панель отдавалась вообще без
    заголовка (проверено на стенде). Cookie сессии без признака `Secure` браузер
    отправит и по http — хватит одного перехода по ссылке без буквы «с».
    """
    blok = _grafana_blok(_read(COMPOSE))
    assert 'GF_SECURITY_CONTENT_SECURITY_POLICY: "true"' in blok, (
        "панель снова отдаётся без Content-Security-Policy"
    )
    assert "GF_SECURITY_COOKIE_SECURE: ${OPENCRM_GRAFANA_COOKIE_SECURE:-true}" in blok, (
        "cookie сессии панели больше не помечается Secure по умолчанию"
    )
    assert "GF_SECURITY_COOKIE_SAMESITE: lax" in blok


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


def test_panel_propuskaet_pereklyuchenie_protokola():
    """Без этого Grafana бьётся в 400 раз в секунду — и топит тревогу о 4xx.

    Grafana держит живое соединение на `/monitoring/api/live/ws`. Если nginx не
    передаёт `Upgrade` и `Connection`, запрос доезжает обычным, Grafana отвечает
    400, а её фронтенд повторяет попытку примерно раз в секунду — вечно, в
    каждой открытой вкладке панели.

    Замерено на боевом сервере 26.08.2026: эти четырёхсотки составили **41–43 %
    ОТ ВСЕХ ответов сайта**, и правило `HighClientErrorRate` (порог 40 %)
    звонило и отбивалось по кругу сутками. Хуже самого шума то, что он делает с
    наблюдением: настоящий всплеск ошибок в таком фоне не разглядеть, а тревогу,
    которая звонит каждый день без причины, перестают читать.

    Проверяется весь набор из четырёх частей: без `map` две другие строки не
    соберутся вовсе (переменной нет), без `proxy_http_version 1.1` переключение
    протокола невозможно по HTTP/1.0, а без самих заголовков Grafana не поймёт,
    чего от неё хотят.
    """
    config = _read(LOCATIONS)
    hardening = _read(ROOT / "docker" / "nginx" / "templates" / "hardening.inc")

    assert re.search(r"map\s+\$http_upgrade\s+\$opencrm_connection_upgrade", hardening), (
        "исчез map для переключения протокола — Grafana Live снова будет отвечать 400"
    )

    block = re.search(r"location /monitoring/ \{(.*?)\n\}", config, re.S)
    assert block, "нет прохода к панели через nginx"
    telo = block.group(1)
    for stroka in (
        "proxy_http_version 1.1;",
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $opencrm_connection_upgrade;",
    ):
        assert stroka in telo, (
            f"в блоке панели нет строки {stroka!r} — живое соединение Grafana "
            f"будет отвечать 400 раз в секунду"
        )


def test_pereklyuchenie_protokola_ne_uehalo_v_obshchiy_fayl():
    """Пятерым из шести мест оно не нужно, и молчаливая строка там вредна.

    `proxy-headers.inc` подключён в шесть мест: корень сайта, медиа витрины, два
    входа в CRM, панель и её страница входа. WebSocket нужен ровно панели —
    живой поток самой CRM сделан на SSE именно затем, чтобы обойтись без правки
    nginx (`docs/12-realtime.md`, раздел 3).

    Строка, которая ничего не делает в пяти местах из шести, однажды будет
    прочитана как «здесь тоже WebSocket» — и следующий разбор пойдёт не туда.
    """
    obshchiy = _read(ROOT / "docker" / "nginx" / "templates" / "proxy-headers.inc")
    assert "Upgrade" not in obshchiy, (
        "переключение протокола уехало в общий файл заголовков: оно нужно одной "
        "панели, а подключён он в шесть мест"
    )


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
    """COMPOSE_PROFILES — общий список, и писать в него можно только по имени.

    Профилей в нём сейчас два, оба про мониторинг: `monitoring` и
    `monitoring-logs`. Запись целиком (`env_set COMPOSE_PROFILES monitoring`)
    молча вынесла бы из стека логи — а раньше, пока база была под профилем,
    так же молча выносила бы саму базу, и сайт после ближайшего `up`
    поднимался бы на пустом файле рядом. Цена ошибки упала, правило осталось.
    """
    script = _script()
    assert "compose_profile monitoring on" in script
    assert "compose_profile monitoring off" in script
    assert "compose_profile monitoring-logs" in script
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


def test_prometheus_perechityvaet_pravila_bezuslovno():
    """У Prometheus сигнал уходит КАЖДЫЙ раз, и это не забывчивость.

    Соседний Alertmanager сравнивает готовый конфиг со старым и молчит, пока
    тот не изменился (см. проверку ниже — там за этим стоит лишнее сообщение в
    чате). Повторить приём здесь нельзя: из шаблона тут рождается только
    `prometheus.yml`, а файлы правил монтируются как есть и в сравнение не
    попадают вовсе. Сравнивающий Prometheus не заметил бы НИ ОДНОЙ правки
    правил, и новое правило после обновления не начало бы действовать никогда —
    отказ, который виден только по несработавшей тревоге.
    """
    entrypoint = _read(MONITORING / "prometheus" / "entrypoint.sh")
    telo = entrypoint.split("while :; do", 1)[1].split("done", 1)[0]
    assert "kill -HUP 1" in telo
    for uslovie in ("if ", "cmp", "diff"):
        assert uslovie not in telo, (
            "перезагрузка Prometheus стала условной — правки правил перестанут "
            "применяться, потому что правила не проходят через шаблон"
        )
    # И причина записана рядом, иначе следующий читатель «починит» это обратно.
    assert "правила" in entrypoint and "alertmanager" in entrypoint


def _kusok(text: str, nachalo: str, konets: str) -> str:
    return text.split(nachalo, 1)[1].split(konets, 1)[0]


def test_alertmanager_shlyot_signal_tolko_pri_nastoyashchem_rasxozhdenii(tmp_path):
    """Второй источник лишних сообщений, и он тише первого.

    Перечитывание шло каждые пять минут БЕЗУСЛОВНО. Перезагрузка конфига в
    Alertmanager останавливает диспетчер и поднимает новый: всё, что было в
    полёте, обрывается вместе со старым контекстом, а отметка «это уже
    отправляли» кладётся в журнал уведомлений ТОЛЬКО после успешной отправки.
    Оборванная на полпути отправка — это доставленное сообщение без отметки, и
    новый диспетчер шлёт то же самое второй раз. Период перезагрузки (5 минут)
    совпадает с `group_interval`, поэтому совпадение систематическое, а не
    случайное — так и приходили два одинаковых «всё в порядке» подряд.

    Сторож не текстовый: тело цикла и функция `render` берутся из самого файла
    и выполняются настоящей оболочкой. Уберут сравнение — сигналов станет
    четыре вместо одного, и проверка покраснеет.
    """
    if not shutil.which("sh"):
        pytest.skip("нет /bin/sh — проверять оболочкой нечем")

    entrypoint = _read(MONITORING / "alertmanager" / "entrypoint.sh")
    render = re.search(r"\nrender\(\) \{\n(.*?)\n\}\n", entrypoint, re.S)
    assert render, "функция render пропала — проверка смотрит не туда"
    tsikl = re.search(r"while :; do\n(.*?)\n    done", entrypoint, re.S)
    assert tsikl, "цикл перечитывания пропал"

    shablon = tmp_path / "shablon.yml"
    shablon.write_text(
        "bot_token: __TELEGRAM_TOKEN__\nchat_id: __TELEGRAM_CHAT__\napi_url: __TELEGRAM_API__\n",
        encoding="utf-8",
    )
    signaly = tmp_path / "signaly"

    # Пути в сценарий подставляются через `as_posix`, а не как есть. На Linux
    # это ничего не меняет, а на Windows — единственный способ вообще получить
    # верный сценарий: `tmp_path` там выглядит `C:\\Users\\...`, а обратная
    # косая в оболочке экранирует следующий знак. Разделители съедались, и
    # `render` писал файл с именем `C:UsersdenishumenAppData...` — прямо в
    # корень репозитория, а проверка краснела на исправном коде.
    kuda = tmp_path.as_posix()
    scenariy = f"""
TOKEN=t
CHAT=c
API=a
TEMPLATE={shablon.as_posix()}
TARGET={kuda}/gotovyy.yml
NOVYY={kuda}/gotovyy.yml.new
sleep() {{ :; }}
kill() {{ echo HUP >> {signaly.as_posix()}; }}
render() {{
{render.group(1)}
}}
render "$TARGET"
for _ in 1 2; do
{tsikl.group(1)}
done
echo "# правка шаблона" >> "$TEMPLATE"
for _ in 1 2; do
{tsikl.group(1)}
done
"""
    zapusk = subprocess.run(
        ["sh", "-c", scenariy], capture_output=True, text=True, timeout=60
    )
    assert zapusk.returncode == 0, zapusk.stderr

    poslano = signaly.read_text(encoding="utf-8").split() if signaly.exists() else []
    assert poslano == ["HUP"], (
        "сигналов должно быть ровно два состояния: молчание, пока конфиг тот же, "
        f"и один сигнал на настоящую правку. Получено: {poslano!r}"
    )
    # А правка при этом действительно доехала до готового конфига.
    assert "# правка шаблона" in (tmp_path / "gotovyy.yml").read_text(encoding="utf-8")
    # Черновик за собой убран: мусор в /tmp контейнера копился бы вечно.
    assert not (tmp_path / "gotovyy.yml.new").exists()


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
        "DecodeQueueSaturated": "очередь разжатия картинок занята целиком",
        "DecodeQueueLocalLimit": "предел разжатия держится в памяти процесса",
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


# --- «тревога» — «всё в порядке» — «тревога»: гистерезис ----------------------


#: Тревоги, которым гистерезис НЕ нужен, и почему именно. Список закрытый и
#: проверяется в обе стороны: у названных здесь `keep_firing_for` быть не
#: должно, у всех остальных — обязан. Смысл двусторонности в том, что новое
#: правило нельзя завести молча: либо оно умеет качаться вокруг порога и
#: получает гистерезис, либо его вносят сюда, объяснив, почему не умеет.
BEZ_GISTEREZISA = {
    "CertificateExpiringSoon": "счётчик дней идёт только вниз; вверх — один раз, продлением",
    "BackupTooOld": "возраст копии растёт монотонно, гаснет от появления новой",
    "DigestMissing": "то же у сводки: срок с последней растёт монотонно, гаснет от новой",
    "BackupBroken": "меняется раз в сутки, в момент снятия копии",
    "DeployRolledBack": "гаснет по своему часовому окну — гистерезис спорил бы с замыслом",
}


def _pravila_blokami() -> dict[str, str]:
    """{имя тревоги: её кусок текста}."""
    rules = _read(RULES)
    bloki = {}
    for kusok in re.split(r"\n      - alert: ", rules)[1:]:
        bloki[kusok.splitlines()[0].strip()] = kusok
    return bloki


def _vyrazhenie(block: str) -> str:
    """Условие правила (`expr:`) одной строкой — включая свёрнутые (`>-`).

    Свёрнутый блок YAML склеивает строки ПРОБЕЛОМ, поэтому и здесь склейка
    пробелом: разбор обязан видеть ровно то выражение, которое увидит Prometheus.
    """
    stroki = block.splitlines()
    for nomer, stroka in enumerate(stroki):
        sovpalo = re.match(r"^        expr:\s*(.*)$", stroka)
        if not sovpalo:
            continue
        hvost = sovpalo.group(1).strip()
        if hvost and hvost not in (">-", ">", "|", "|-"):
            return hvost
        kuski = []
        for dalshe in stroki[nomer + 1 :]:
            if dalshe.strip() and not dalshe.startswith("          "):
                break
            kuski.append(dalshe.strip())
        return " ".join(k for k in kuski if k)
    raise AssertionError(f"у правила {stroki[0].strip()} нет условия")


def _annotatsii(block: str) -> dict[str, str]:
    """Аннотации правила, склеенные из свёрнутых (`>-`) кусков в одну строку."""
    if "annotations:" not in block:
        return {}
    naydeno: dict[str, str] = {}
    tekushchiy = ""
    for stroka in block.split("annotations:", 1)[1].splitlines():
        klyuch = re.match(r"^          (\w+):(.*)$", stroka)
        if klyuch:
            tekushchiy = klyuch.group(1)
            naydeno[tekushchiy] = klyuch.group(2).strip()
        elif tekushchiy and stroka.strip():
            naydeno[tekushchiy] += " " + stroka.strip()
    return naydeno


def test_u_kachayushchihsya_trevog_est_gisterezis():
    """Мигание порога приучает не читать быстрее, чем ложная тревога.

    Живой случай: доля 4xx весь день ходила вокруг сорока процентов, и в чат
    приходило «тревога» — «всё в порядке» — «тревога» — «всё в порядке», при
    том что поломки не было ни разу. `for:` держит только ВХОД в тревогу, а
    выход из неё мгновенный; `keep_firing_for` и есть недостающая половина.
    """
    bloki = _pravila_blokami()
    assert len(bloki) >= 25, "правил стало подозрительно мало — разбор смотрит не туда"
    for imya, block in bloki.items():
        est = bool(re.search(r"^\s+keep_firing_for: ", block, re.M))
        if imya in BEZ_GISTEREZISA:
            assert not est, (
                f"{imya} назван в списке исключений ({BEZ_GISTEREZISA[imya]}), "
                "но гистерезис у него всё-таки стоит — список врёт"
            )
        else:
            assert est, (
                f"{imya}: нет keep_firing_for. Показатель, ходящий вокруг порога, "
                "будет слать «тревога» — «всё в порядке» по кругу. Если он так не "
                "умеет — впишите его в BEZ_GISTEREZISA и объясните, почему"
            )


# --- число в сообщении -------------------------------------------------------


#: Тревоги, у которых `$value` осмыслен и обязан стоять в первой строке.
#: Остальные меряют «ноль или один» (лежит / нездоров / не совпало), и число в
#: них не сказало бы ничего.
S_CHISLOM = (
    "SiteSlow",
    "HighErrorRate",
    "HighClientErrorRate",
    "CertificateExpiringSoon",
    "DiskAlmostFull",
    "HostMemoryLow",
    "HostSwapping",
    "ContainerRestartLoop",
    "BackupTooOld",
    # `RateLimiterUnavailable` здесь БЫЛ и ушёл вместе со сменой выражения.
    #
    # Он считал прирост счётчика, живущего в памяти процесса, и число в
    # заголовке было осмысленным: «столько-то отказов за пять минут». Но при
    # нескольких рабочих процессах такой счётчик врёт — Prometheus скребёт один
    # адрес и попадает на случайный процесс, ряд скачет, и `increase` показывает
    # прирост там, где его нет. Тревога переехала на СОСТОЯНИЕ
    # (`opencrm_ratelimiter_recently_unavailable`, ноль или единица), а у
    # состояния числа не бывает: «1 отказов» — не сведение, а шум.
    #
    # Рядом ровно тот же случай и то же решение: `RedisDown` в этом списке
    # никогда и не стоял, потому что «Redis не отвечает» — тоже состояние.
    "DatabaseConnectionsHigh",
    "DatabaseLockWaits",
    "DatabaseSlowQueries",
    "DatabaseBufferPoolMisses",
    "RedisEvictingKeys",
    "RedisMemoryNearLimit",
)


def test_chislo_stoit_v_pervoy_stroke_a_ne_v_podrobnostyah():
    """«Доля 4xx — 63%» и «доля 4xx выше 40%» стоят одинаково, а решения по ним
    разные. Первая строка сообщения собирается из `summary` — значит и число
    живёт там, а не в описании, куда надо разворачивать."""
    bloki = _pravila_blokami()
    for imya in S_CHISLOM:
        assert imya in bloki, f"правило {imya} исчезло — список смотрит не туда"
        summary = _annotatsii(bloki[imya]).get("summary", "")
        assert "$value" in summary, (
            f"{imya}: в заголовке нет самого числа — человек узнаёт только то, "
            "что порог перейдён, и идёт смотреть ради одной цифры"
        )


def test_doli_pechatayutsya_protsentami_a_ne_dolyami():
    """Отказ, который врал в сто раз и молчал об этом.

    Выражения вроде `avail / size` дают ДОЛЮ: 0.08, а не 8. Привычное
    `printf "%.1f%%"` печатало из неё «0.1%», а `printf "%.0f%%"` из доли
    пятисоток 0.07 — честный «0%». То есть сообщение про кончающийся диск
    пугало вдесятеро сильнее правды, а сообщение про сыплющиеся ошибки —
    успокаивало. Проверено `promtool test rules` на выдуманных рядах:
    8% свободного места давали ровно «Свободно 0.1%».

    `humanizePercentage` умножает на сто сам.
    """
    rules = _read(RULES)
    plohie = re.findall(r"\{\{[^}]*\$value[^}]*printf[^}]*%%[^}]*\}\}", rules)
    assert not plohie, (
        "доля печатается через printf с процентом — она уедет в текст в сто раз "
        f"меньше настоящей: {plohie}"
    )
    bloki = _pravila_blokami()
    for imya in (
        "HighErrorRate",
        "HighClientErrorRate",
        "DiskAlmostFull",
        "HostMemoryLow",
        "DatabaseConnectionsHigh",
        "DatabaseBufferPoolMisses",
        "RedisMemoryNearLimit",
    ):
        summary = _annotatsii(bloki[imya]).get("summary", "")
        assert "humanizePercentage" in summary, f"{imya}: доля печатается не процентами"


def test_dolyu_vidno_po_samomu_vyrazheniyu_a_ne_po_spisku_ruchkoy():
    """Тот же запрет, но без списка имён — и потому он не устареет.

    Список выше стережёт правила, которые УЖЕ написаны; беда приходит со
    следующим. Признак доли механический: в выражении есть деление, а порог, с
    которым сравнивают, лежит строго между нулём и единицей — то есть это не
    секунды, не дни и не штуки, а именно доля. Такому правилу `humanizePercentage`
    обязателен, потому что `printf "%.1f%%"` напечатает из 0.85 — «0.9%».

    Ровно этот отказ и чинили: «Свободно 0.1%» вместо «8%». Проверено
    `promtool test rules` на выдуманных рядах и здесь, и тогда.
    """
    for imya, block in _pravila_blokami().items():
        expr = _vyrazhenie(block)
        if "/" not in expr:
            continue
        porogi = [float(p) for p in re.findall(r"[<>]=?\s*([0-9]*\.?[0-9]+)", expr)]
        if not any(0 < p < 1 for p in porogi):
            continue
        summary = _annotatsii(block).get("summary", "")
        assert "humanizePercentage" in summary, (
            f"{imya}: выражение даёт долю (порог {porogi}), а в заголовке её печатают "
            f"иначе — число уедет в сообщение в сто раз меньше настоящего"
        )


# --- база и Redis: правило обязано быть способно сработать --------------------
#
# Здесь стережётся отказ, у которого нет ни одного внешнего признака: правило
# написано, `promtool check rules` его принял, в списке тревог оно есть — а ряда,
# по которому оно считает, в хранилище нет и не будет. Такая тревога молчит
# всегда и одинаково: и когда всё хорошо, и когда база задыхается.


#: Поводы про базу и Redis. Все — `warning`, и это разделение труда, а не
#: осторожность: про ЛЕЖАЩУЮ базу скажет `SiteDown` (без неё приложение не
#: отвечает вовсе), а здесь про запас, который кончается при живом сайте.
POVODY_KHRANILISHCH = {
    "DatabaseConnectionsHigh": "соединений к базе больше 80% от потолка",
    "DatabaseLockWaits": "запросы стоят в очереди за блокировками строк",
    "DatabaseSlowQueries": "медленные запросы идут потоком",
    "DatabaseBufferPoolMisses": "рабочий набор базы не помещается в память",
    "DatabaseMetricsUnavailable": "наблюдатель не подключается к базе",
    "RedisEvictingKeys": "Redis выбрасывает ключи, то есть теряет счётчики попыток",
    "RedisMemoryNearLimit": "Redis занял больше 90% своего потолка памяти",
}

#: Приставка имени метрики → задание сбора, у которого стоит отбор `keep`.
OTBOR = (("mysql_", "mysql"), ("redis_", "redis"))

#: Счётчики: растут от старта сервера и не убывают. Сравнивать их с порогом
#: напрямую нельзя — один медленный запрос в прошлом месяце навсегда сделал бы
#: `slow_queries > 0` истиной, а тревогу — вечной.
SCHYOTCHIKI = (
    "mysql_global_status_slow_queries",
    "mysql_global_status_innodb_row_lock_waits",
    "mysql_global_status_innodb_buffer_pool_reads",
    "mysql_global_status_innodb_buffer_pool_read_requests",
    "redis_evicted_keys_total",
)


def _keep_spisok(job: str) -> str:
    """Отбор `keep` задания сбора: какие ряды экспортёра доезжают до хранилища."""
    config = _read(PROMETHEUS)
    assert f"job_name: {job}\n" in config, f"в шаблоне Prometheus нет задания {job!r}"
    kusok = config.split(f"job_name: {job}\n", 1)[1].split("- job_name:", 1)[0]
    sovpalo = re.search(r"^([ ]*)regex: >-\n", kusok, re.M)
    assert sovpalo, f"у задания {job} нет списка keep — экспортёр зальёт в хранилище всё"
    # Тело свёрнутого блока — строки с отступом БОЛЬШИМ, чем у самого ключа, до
    # первой пустой строки. Границу считаем отступом, а не «непробельным
    # символом»: `\s` съедает и перевод строки, и такой разбор утаскивает в
    # выражение соседние комментарии файла.
    otstup = len(sovpalo.group(1))
    kuski = []
    for stroka in kusok[sovpalo.end() :].splitlines():
        if not stroka.strip():
            break
        if len(stroka) - len(stroka.lstrip()) <= otstup:
            break
        kuski.append(stroka.strip())
    otbor = " ".join(kuski)
    # Свёрнутый блок YAML склеивает строки ПРОБЕЛОМ. Перенос внутри регулярного
    # выражения оставил бы в нём пробел, и оно перестало бы совпадать с чем бы то
    # ни было — то есть до хранилища не доехал бы НИ ОДИН ряд этого экспортёра.
    assert " " not in otbor, (
        f"список keep задания {job} разбит переносом: свёрнутый блок `>-` склеит "
        f"строки пробелом, и отбор перестанет совпадать с любым именем"
    )
    return otbor


def test_metrika_kazhdogo_pravila_dodet_cherez_otbor():
    """Худший вид тревоги — та, что не может сработать никогда.

    У заданий `mysql` и `redis` стоит жёсткий `metric_relabel_configs` с `keep`:
    из 992 рядов mysqld-exporter и 300 рядов redis_exporter (замерено запросом к
    живым контейнерам) до хранилища доезжает около двух десятков — иначе одна
    служба удвоила бы объём и уткнулась в потолок 512 МБ раньше, чем истекут
    пятнадцать дней хранения.

    Отсюда правило: имя, названное в тревоге, обязано пройти этот отбор. Не
    прошло — ряда в хранилище нет, условие не выполняется никогда, и тревога
    молчит ОДИНАКОВО при исправной и при задыхающейся базе. Ни `promtool check
    rules`, ни глаз человека этого не видят: правило выглядит написанным.

    Соседний `test_monitoring_dashboards.py` держит ту же связку со стороны
    панелей; здесь — со стороны тревог, а это половина важнее: на панель ходят,
    заподозрив неладное, а тревога и есть то, чем неладное узнаётся.
    """
    otbory = {job: _keep_spisok(job) for _pristavka, job in OTBOR}
    naydeno = 0

    for imya, block in _pravila_blokami().items():
        expr = _vyrazhenie(block)
        for metrika in sorted(set(re.findall(r"\b(?:mysql|redis)_[a-z0-9_]+", expr))):
            job = next((j for pristavka, j in OTBOR if metrika.startswith(pristavka)), None)
            assert job, f"{imya}: метрику {metrika} никто не собирает"
            naydeno += 1
            assert re.fullmatch(otbory[job], metrika), (
                f"{imya}: метрика {metrika} не проходит отбор `keep` задания {job!r} — "
                f"ряда в хранилище не будет, и правило не сработает НИКОГДА. Допишите "
                f"её в metric_relabel_configs в prometheus.yml.template"
            )

    assert naydeno >= 10, (
        "правила перестали ссылаться на метрики базы и Redis — проверка стережёт пустоту"
    )


def test_povody_pro_bazu_i_redis_est_i_ne_zvenyat_nochyu():
    """Второй половине обещания — важность.

    Alertmanager разводит важность по приёмникам: `critical` звенит, `warning`
    приходит молча. Предупреждение о кончающемся запасе, разбудившее ночью,
    научит выключать звук всему чату — а выключенный звук чата это выключенный
    звук и у настоящей аварии тоже.
    """
    bloki = _pravila_blokami()
    for imya, povod in POVODY_KHRANILISHCH.items():
        assert imya in bloki, f"пропал повод «{povod}»"
        assert re.search(r"^\s+severity: warning\s*$", bloki[imya], re.M), (
            f"{imya} («{povod}») стал звенеть ночью. Про лежащую базу скажет SiteDown; "
            f"это правило — про запас, который кончается при живом сайте"
        )


def test_schyotchiki_ot_starta_beryutsya_prirostom():
    """Счётчик, сравнённый с порогом напрямую, даёт вечную тревогу.

    `mysql_global_status_slow_queries` и `redis_evicted_keys_total` живут от
    старта сервера и не обнуляются. Условие `> 0` по такому счётчику,
    выполнившись однажды, останется истинным до перезапуска — то есть месяцами,
    и различить по нему «идёт прямо сейчас» от «случалось когда-то» нельзя.
    Тот же урок уже стоил перевода `RateLimiterUnavailable` на `increase`.
    """
    for imya, block in _pravila_blokami().items():
        expr = _vyrazhenie(block)
        for schyotchik in SCHYOTCHIKI:
            for sovpalo in re.finditer(re.escape(schyotchik), expr):
                do = expr[: sovpalo.start()]
                assert re.search(r"(?:rate|increase)\(\s*$", do), (
                    f"{imya}: счётчик {schyotchik} сравнивается напрямую, а он растёт от "
                    f"старта сервера. Тревога, выполнившись однажды, будет гореть вечно "
                    f"и перестанет что-либо означать. Нужен rate() или increase()"
                )


def test_dolya_ot_potolka_ne_delitsya_na_nol():
    """Правило, горящее с первой секунды, — это выключенное правило.

    Потолок памяти Redis настраивается (`OPENCRM_REDIS_MAXMEMORY`), и значение
    `0` означает «без потолка». На такой установке `used / max` даёт `+Inf`,
    `+Inf > 0.9` — истина, и тревога загорается сразу и навсегда, при полностью
    здоровом Redis. Оговорка в условии стоит именно от этого; проверено
    `promtool test rules` рядом с нулевым потолком: тревоги нет.
    """
    expr = _vyrazhenie(_pravila_blokami()["RedisMemoryNearLimit"])
    # Ноль и ничего больше: без «не цифра дальше» выражение `> 0` находилось бы
    # внутри самого порога `> 0.9`, и снятая оговорка прошла бы незамеченной.
    # Поймано снятием починки: сторож остался зелёным на сломанном правиле.
    assert re.search(r"redis_memory_max_bytes\s*>\s*0(?![.\d])", expr), (
        "исчезла оговорка про нулевой потолок: на установке без maxmemory деление "
        "даст +Inf, и тревога загорится навсегда с первой секунды"
    )


# --- оформление сообщения о тревоге ------------------------------------------


def _poluchateli() -> dict[str, str]:
    """{имя приёмника: его кусок текста} из шаблона alertmanager."""
    template = _read(ALERTMANAGER)
    hvost = template.split("\nreceivers:", 1)[1]
    naydeno = {}
    for sovpalo in re.finditer(r"\n  - name: (\S+)\n(.*?)(?=\n  - name: |\Z)", hvost, re.S):
        naydeno[sovpalo.group(1)] = sovpalo.group(2)
    return naydeno


def _telo_soobshcheniya() -> str:
    """Текст шаблона сообщения — блочный скаляр под ключом `message:`."""
    stroki = _read(ALERTMANAGER).splitlines()
    nachalo = next(i for i, s in enumerate(stroki) if s.strip().startswith("message: &"))
    otstup = len(stroki[nachalo]) - len(stroki[nachalo].lstrip())
    telo = []
    for stroka in stroki[nachalo + 1 :]:
        if stroka.strip() and (len(stroka) - len(stroka.lstrip())) <= otstup:
            break
        telo.append(stroka.strip())
    assert telo, "тело сообщения не нашлось — разбор смотрит не туда"
    return "\n".join(telo)


def test_dannye_ekraniruyutsya_rovno_odin_raz():
    """Единственная защита от потери сообщения об аварии — и её легко удвоить.

    Запасного пути у Alertmanager нет: неразобранная разметка — это 400 и
    выброшенное сообщение, то есть об аварии не узнают именно потому, что
    сообщение было подробным. Защита тут одна, и она встроенная: при
    `parse_mode: HTML` Alertmanager рендерит текст через `html/template`, а тот
    экранирует ПОДСТАНОВКИ, не трогая литеральные `<b>` шаблона.

    Проверено живьём на v0.28.0 (та же версия, что в docker-compose.yml),
    заглушкой вместо Telegram: описание `less< more> amp&` доехало как
    `less&lt; more&gt; amp&amp;`, а без `parse_mode` — как есть.

    Отсюда обе половины проверки. `parse_mode: HTML` обязан быть у каждого
    приёмника — без него экранирования не будет вовсе. А ручная цепочка
    `reReplaceAll` — запрещена: она ложится ПОД встроенную, и `<script>`
    доезжает как `&amp;lt;script&amp;gt;`. Ровно так и было.
    """
    poluchateli = _poluchateli()
    assert poluchateli, "приёмники не разобрались"
    for imya, blok in poluchateli.items():
        assert re.search(r"^\s+parse_mode: HTML\s*$", blok, re.M), (
            f"{imya}: без parse_mode: HTML подстановки перестанут экранироваться, "
            "а теги — работать"
        )

    telo = _telo_soobshcheniya()
    assert "reReplaceAll" not in telo, (
        "в шаблоне снова ручное экранирование: поверх него ляжет встроенное, и "
        "текст тревоги приедет мусором вида &amp;lt;script&amp;gt;"
    )


def test_pervaya_stroka_otvechaet_idti_li_smotret():
    """В списке чатов видно только начало сообщения.

    Прежняя первая строка — «🔴 Тревога (1)» — отвечала на «горит ли», но не на
    «что именно» и не на «вставать ли сейчас». Открывать чат приходилось всегда,
    в том числе ночью и в том числе ради предупреждения, которое подождёт до
    утра.
    """
    telo = _telo_soobshcheniya()
    # Объявления переменных ничего не печатают и в счёт первой строки не идут.
    stroki = [s for s in telo.splitlines() if not s.startswith("{{- $")]
    pervaya = stroki[0]

    for znachok in ("🔴", "🟢"):
        assert znachok in pervaya, f"в первой строке нет значка состояния {znachok}"
    for znachok in ("🆘", "⚠️"):
        assert znachok in pervaya, (
            f"в первой строке нет значка важности {znachok} — «вставать сейчас» и "
            "«посмотреть утром» выглядят одинаково"
        )
    assert "Annotations.summary" in pervaya, (
        "первая строка не называет, ЧТО случилось: в списке чатов сообщение "
        "неотличимо от любого другого"
    )
    # И время жизни тревоги: «не отвечает две минуты» и «не отвечает два часа» —
    # разные новости.
    assert "since" in telo and "humanizeDuration" in telo, (
        "не сказано, сколько тревога висит"
    )


def test_vazhnost_reshaet_zvenet_li_telefon():
    """Предупреждение, звенящее ночью, учит выключать звук всему чату.

    А выключенный звук чата — это выключенный звук и у аварии тоже. Поэтому
    важность разведена по приёмникам: критическое звенит, предупреждение
    приходит значком.
    """
    template = _read(ALERTMANAGER)
    marshruty = template.split("routes:", 1)[1].split("\nreceivers:", 1)[0]
    kriticheskiy = marshruty.split('severity="critical"', 1)[1].split("- matchers", 1)[0]
    preduprezhdenie = marshruty.split('severity="warning"', 1)[1]

    gromkiy = re.search(r"receiver: (\S+)", kriticheskiy).group(1)
    tikhiy = re.search(r"receiver: (\S+)", preduprezhdenie).group(1)
    assert gromkiy != tikhiy, "важность больше не решает, звенеть ли телефону"

    poluchateli = _poluchateli()
    assert "disable_notifications: false" in poluchateli[gromkiy], (
        "критическое перестало звенеть — а разбудить оно обязано"
    )
    assert "disable_notifications: true" in poluchateli[tikhiy], (
        "предупреждение снова звенит"
    )
    # Текст у обоих один и тот же — якорем, а не копией: разъехавшиеся копии
    # означали бы, что половина тревог оформлена по-старому.
    assert "message: *" in poluchateli[tikhiy], (
        "шаблон сообщения размножен копией — копии разъедутся молча"
    )


def test_sledstvie_ne_shlyot_vtorogo_soobshcheniya():
    """Лежащий сайт зажигает полдюжины правил, а чинят одну поломку.

    Шесть сообщений об одном событии читаются как одно, и в следующий раз
    читается только первое. Инхибиция гасит следствия, у которых нет ни своего
    знания, ни своего действия.
    """
    template = _read(ALERTMANAGER)
    assert "inhibit_rules:" in template, "следствия снова шлют свои сообщения"
    blok = template.split("inhibit_rules:", 1)[1].split("\nroute:", 1)[0]
    assert 'alertname="SiteDown"' in blok, "падение сайта ничего не гасит"

    # Каждое имя, названное в инхибиции, обязано существовать правилом.
    # Переименованная тревога иначе тихо перестала бы гаситься.
    rules = _read(RULES)
    nazvany = set()
    for gruppa in re.findall(r'alertname=~?"([^"]+)"', blok):
        nazvany.update(gruppa.split("|"))
    assert nazvany, "инхибиция не ссылается ни на одно правило"
    for imya in sorted(nazvany):
        assert f"- alert: {imya}" in rules, (
            f"инхибиция ссылается на несуществующее правило {imya} — строка мертва"
        )


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


# --- размер базы: спрашивается у сервера, и спрашивается всегда --------------
#
# Метрика `opencrm_database_size_bytes` когда-то ПРОСТО ИСЧЕЗАЛА на всём, кроме
# SQLite: сборщик выходил на первой строке, а спросить сервер было нечем — ответ
# лежит внутри него, а запросы живут только в `database/`. Переезд на MySQL
# состоялся, и такое молчание означало бы, что за ростом базы не следит никто
# ровно на том движке, где она и растёт.


def test_razmer_bazy_sprashivaetsya_u_servera():
    """Число приходит от самого сервера, через репозиторий.

    Развилки по движку здесь больше нет, и это не упрощение записи. База в
    проекте одна, и вторая половина развилки на прогоне не исполнялась вовсе:
    ветка «не MySQL» проверяла молчание, до которого прогон никогда не доходил,
    то есть половина проверки была мертва и красной стать не могла.
    """
    from database.repositories import engine_info
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        razmer = engine_info.database_size_bytes(db)
    finally:
        db.close()
    assert razmer and razmer > 0, "сервер не сказал размер своей базы"


def test_razmer_sprashivaetsya_zaprosom_a_ne_molchaniem():
    """Сторожим связку, а не число: числу и так есть кому покраснеть выше.

    Ровно этой связки и не хватало: размер умели считать только у файловой базы,
    а на сервере сборщик выходил на первой строке — другого пути у числа не было
    вовсе, и метрика молча пропадала.
    """
    # Путь от корня репозитория, как у всех соседей в этом файле. Относительный
    # зависел бы от рабочего каталога прогона — то есть от того, что делал
    # предыдущий тест, а не от того, что проверяет этот.
    istochnik = (ROOT / "web" / "api" / "routes" / "metrics.py").read_text(encoding="utf-8")
    sborshchik = istochnik[istochnik.index("def _collect_database("):]
    sborshchik = sborshchik[: sborshchik.index("\ndef ")]
    assert "engine_info.database_size_bytes" in sborshchik, (
        "размер базы снова неоткуда взять — метрика молча исчезнет с панели"
    )
    # И запрос при этом остаётся в database/: маршрут его не собирает сам.
    # (Слово `information_schema` в тексте подсказки к метрике — это проза, а не
    # запрос, поэтому ищем именно конструкции сборки запроса.)
    for zapreshcheno in ("select(", "db.execute", "text("):
        assert zapreshcheno not in sborshchik, (
            f"запрос переехал в маршрут ({zapreshcheno}) — это нарушение границы базы"
        )


# --- метрики, устойчивые к нескольким рабочим процессам ----------------------


def test_trevoga_ne_derzhitsya_za_schyotchik_v_pamyati_processa():
    """`RateLimiterUnavailable` обязана смотреть на СОСТОЯНИЕ, а не на счётчик.

    **Разбор.** `opencrm_ratelimiter_unavailable_total` живёт в памяти ПРОЦЕССА,
    а Prometheus скребёт один адрес: nginx отдаёт запрос случайному рабочему
    процессу. При нескольких воркерах ряд заскачет между их независимыми
    значениями (5, 0, 3, 0…), и для типа `counter` каждое понижение читается как
    перезапуск — `increase` покажет прирост там, где его нет.

    Цена: тревога уровня `critical` зазвонит навсегда после первой в жизни
    неудачи Redis, и настоящую аварию в этом звоне никто не отличит. Хуже
    молчащей тревоги только звонящая без повода — её выключают, и вместе с ней
    выключают все остальные.

    Состояние такой беды не имеет по построению: Redis у процессов общий, и
    ответ «сбоил ли ограничитель в последнюю минуту» у всех одинаков.
    """
    bloki = _pravila_blokami()
    assert "RateLimiterUnavailable" in bloki, "правило исчезло — проверка смотрит не туда"
    # Берём ТОЛЬКО выражение, а не блок целиком: слово `unavailable_total`
    # стоит в объяснении рядом, и поиск по блоку краснел бы на исправном
    # правиле. Помощник для этого в файле уже есть.
    vyrazhenie = _vyrazhenie(bloki["RateLimiterUnavailable"])
    assert vyrazhenie, "у правила нет выражения — проверка смотрит не туда"

    assert "unavailable_total" not in vyrazhenie, (
        "тревога снова считает прирост счётчика из памяти процесса — при "
        "нескольких воркерах она зазвонит на ровном месте"
    )
    assert "recently_unavailable" in vyrazhenie, (
        "тревога не смотрит на состояние ограничителя"
    )


def test_sostoyanie_ogranichitelya_est_v_metrikah(metrics_module, monkeypatch):
    """Метрика, на которую переехала тревога, обязана существовать.

    Иначе правило ссылается в пустоту: выражение верное, ряда нет, тревога молчит
    всегда — и это худший вид немоты, потому что выглядит как «всё хорошо».

    Блок мониторинга включается фикстурой: маршрут закрыт им, и без неё проверка
    мерила бы отказ, а не метрики.
    """
    otvet = _client(metrics_module, enabled=True, monkeypatch=monkeypatch).get(
        "/api/v1/metrics"
    )
    assert otvet.status_code == 200, otvet.text
    telo = otvet.text

    assert "opencrm_ratelimiter_recently_unavailable" in telo, (
        "нет метрики состояния ограничителя — тревога ссылается в пустоту"
    )
    # И она о состоянии, а не о накоплении: ноль или единица.
    for stroka in telo.splitlines():
        if stroka.startswith("opencrm_ratelimiter_recently_unavailable "):
            znachenie = stroka.rsplit(" ", 1)[1]
            assert znachenie in ("0", "1", "0.0", "1.0"), (
                f"состояние отдано числом {znachenie} — это уже не состояние"
            )
            break
    else:
        raise AssertionError("строка метрики не найдена вовсе")


# --- то, что нашлось в боевом логе ---------------------------------------------
#
# Три беды, снятые с работающего сервера 27-28.08.2026. Ни одну не видно в
# интерфейсе, все три видны в логе, и первая из них отвечала пятисотками на
# вход и на запросы панелей.


def test_grafana_derzhit_bazu_v_zhurnalnom_rezhime():
    """WAL у SQLite. Одна настройка против полутора сотен ошибок.

    Без журнала чтения ждут записи, и на занятой базе Grafana отвечает
    «database is locked (5) (SQLITE_BUSY)». В боевом логе за полдня из этого
    выросло: 40 отказов аутентификации, срыв обхода готовых панелей, срыв
    чтения настроек входа и **44 ответа 500** на `/api/ds/query` и `/login`,
    каждый по семь с половиной секунд.

    Оттуда же сотня строк в логе Loki «failed mapping AST, context canceled»:
    браузер бросал запрос, не дождавшись ответа. Искать беду в Loki было бы
    напрасно — он тут пострадавший, и это главная причина, по которой настройка
    закреплена проверкой: снявший её будет чинить не то.
    """
    blok = _grafana_blok(_read(COMPOSE))
    assert re.search(r'GF_DATABASE_WAL:\s*"?true"?', blok), (
        "у Grafana снят журнальный режим SQLite — вернутся 500 на /login и на "
        "запросы панелей, а виноватым будет выглядеть Loki"
    )


def test_promtail_pomnit_dokuda_prochital():
    """Отметка о прочитанном обязана пережить перезапуск.

    Прежде она лежала в /tmp с доводом «потеряется — промотает логи заново, это
    дешевле, чем ещё один каталог состояния». Довод оказался неверным: промотка
    заново означает отправку в Loki записей недельной давности, а тот старше
    `reject_old_samples_max_age` не принимает и отвечает 400 на ВЕСЬ пакет —
    вместе со свежими строками, которые в нём ехали. Снято с боевого:
    «entry for stream has timestamp too old: 2026-08-14, oldest acceptable is
    2026-08-18». То есть перезапуск promtail не тратил время, а терял логи.
    """
    config = _read(MONITORING / "promtail" / "promtail.yml")
    razobrano = yaml.safe_load(config)
    put = razobrano["positions"]["filename"]
    assert not put.startswith("/tmp/"), (
        f"позиции promtail снова в /tmp ({put}) — после перезапуска он пошлёт в "
        "Loki недельные записи, тот ответит 400 на весь пакет, и свежие строки "
        "уедут вместе со старыми"
    )
    katalog = put.rsplit("/", 1)[0]
    compose = _read(COMPOSE)
    # До следующей службы, а не до первой строки с двумя пробелами: тело
    # службы всё отбито глубже, и наивная резка давала пустоту.
    blok = re.split(
        r"\\n  \\w[\\w-]*:",
        compose.split(chr(10) + "  promtail:", 1)[1],
        maxsplit=1,
    )[0]
    assert f":{katalog}" in blok, (
        f"{katalog} не примонтирован службе promtail — отметка ляжет внутрь "
        "контейнера и пропадёт вместе с ним"
    )


def test_katalog_promtail_sozdayotsya_ustanovshchikom():
    """Том без каталога — это каталог, созданный докером от root.

    Дальше владелец в него не пишет, и разбираться приходят через неделю, когда
    логов за эту неделю нет.
    """
    skript = (ROOT / "opencrm.sh").read_text(encoding="utf-8")
    stroka = [s for s in skript.splitlines() if "for _msub in" in s]
    assert stroka, "список каталогов мониторинга пропал из установщика"
    assert "promtail" in stroka[0], (
        "каталог promtail не создаётся установщиком: " + stroka[0].strip()
    )


def test_zaminka_odnogo_konteynera_ne_ronyaet_ves_sbor():
    """Частичный ответ лучше пустого.

    `stats` докер считает сам, и на занятой машине (сборка образа, перезапуск
    стека) один ответ приходит секунды. Прежде любая такая заминка выбрасывала
    исключение наверх, и наружу уходило `opencrm_containers_exporter_up 0` — то
    есть метрик не оставалось НИ ПО ОДНОМУ контейнеру. Снято с боевого: три
    сорванных сбора подряд `TimeoutError('timed out')`, и в эти минуты панель
    контейнеров была пуста целиком — ровно тогда, когда на неё и смотрят.

    Проверяется прогоном настоящего сборщика с подставным docker, а не поиском
    `try` в исходнике: важно, что уцелеет в ответе, а не что написано.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("opencrm_exporter", EXPORTER)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)

    spisok = [
        {"Id": "aaa", "Names": ["/opencrm-app-1"], "State": "running",
         "Labels": {"com.docker.compose.service": "app"}},
        {"Id": "bbb", "Names": ["/opencrm-db-1"], "State": "running",
         "Labels": {"com.docker.compose.service": "db"}},
    ]

    def podstavnoy_docker(path: str):
        if path.startswith("/containers/json?"):
            return spisok
        if path.startswith("/containers/aaa/stats"):
            raise TimeoutError("timed out")     # один контейнер подвис
        if path.endswith("/json"):
            return {"State": {"StartedAt": "2026-08-27T10:00:00Z"}, "RestartCount": 1}
        return {"cpu_stats": {"cpu_usage": {"total_usage": 5_000_000_000}}}

    modul.docker = podstavnoy_docker
    otvet = modul.collect()

    assert "opencrm_containers_exporter_up{} 1" in otvet, (
        "заминка по одному контейнеру погасила весь сбор:\n" + otvet
    )
    for imya in ("opencrm-app-1", "opencrm-db-1"):
        assert imya in otvet, f"контейнер {imya} пропал из ответа целиком:\n{otvet}"
    assert "opencrm_containers_exporter_failures_total" in otvet, (
        "срыв не посчитан — по метрикам его будет не видно вовсе"
    )


def test_promtail_vedyot_uchyot_tolko_po_svoim_konteyneram():
    """Отбор целей стоит на стороне докера, а не только в relabel.

    Разница не косметическая и замерена на стенде. `keep` в `relabel_configs`
    отбрасывает цель ПОСЛЕ того, как promtail её завёл, — а заводя, он пишет ей
    курсор в `positions.yaml`. С одним только relabel на машине с 26
    контейнерами курсоров набралось 24 при НУЛЕ подходящих под `keep`.

    Чем это плохо: контейнер убирают (а обновление убирает их каждый раз),
    курсор остаётся, и promtail ходит к исчезнувшему снова и снова —
    «could not inspect container info: No such container». На стенде это давало
    от восьми до двадцати двух строк за полторы минуты и не прекращалось.
    Перезапуск не помогал: цели воскресали из того же `positions.yaml`, а он
    нарочно лежит на томе (`test_promtail_pomnit_dokuda_prochital`).

    С отбором на стороне докера — 1 курсор при 34 контейнерах.

    `keep` при этом обязан остаться: фильтр решает, за кем promtail ходит, а
    `keep` — что попадает в Loki, и молчаливо отказавший фильтр не должен
    открыть Loki чужие логи.
    """
    razobrano = yaml.safe_load(_read(PROMTAIL))
    rabota = razobrano["scrape_configs"][0]
    obnaruzhenie = rabota["docker_sd_configs"][0]

    filtry = obnaruzhenie.get("filters")
    assert filtry, (
        "у docker_sd_configs нет `filters` — promtail заведёт курсор на КАЖДЫЙ "
        "контейнер машины, и каждый убранный будет вечно давать "
        "«could not inspect container info»"
    )
    znacheniya = [z for f in filtry for z in f.get("values", [])]
    assert any("com.docker.compose.project=opencrm" in z for z in znacheniya), (
        f"фильтр есть, но не по своему проекту: {filtry}"
    )
    # Одноразовые — отдельным условием, и довод у него свой, не про шум.
    # У контейнера от `compose run --rm` ТА ЖЕ метка службы, что у живого
    # приложения (проверено на обоих), поэтому его вывод ложился в поток
    # приложения — и панель «Ошибки приложения» показывала вывод разовой
    # проверки настроек как ошибку сайта.
    assert any("com.docker.compose.oneoff=False" in z for z in znacheniya), (
        f"одноразовые контейнеры снова попадают в сбор: {filtry}. Их вывод "
        "уедет в поток приложения под его же меткой службы, а каждая их "
        "смерть — а умирают они на каждом обновлении — даст строку об "
        "исчезнувшем контейнере"
    )

    keep = [
        pravilo
        for pravilo in rabota["relabel_configs"]
        if pravilo.get("action") == "keep"
    ]
    assert keep, (
        "`keep` по проекту убран вместе с добавлением фильтра — отказавший "
        "фильтр открыл бы Loki чужие логи, а заметить это было бы нечем"
    )


def test_grafana_ne_zhaluetsya_na_pustye_katalogi_provizii():
    """Необязательные каталоги провизии существуют и не мешают.

    Замерено на Grafana 12.4.8 тремя состояниями:

      - каталога нет вовсе — ДВЕ ошибки уровня error, «no such file or
        directory» для `provisioning/plugins` и `provisioning/alerting`;
      - в каталоге `.gitkeep` — предупреждение «file has invalid suffix
        '.gitkeep' (.yaml,.yml,.json accepted), skipping»;
      - пустой `.yaml` с `apiVersion: 1` — ни одной строки.

    Grafana ищет эти каталоги безусловно, даже когда сама возможность выключена
    (`GF_UNIFIED_ALERTING_ENABLED: false`). Поэтому убрать их нельзя, а держать
    в них файл с непринимаемым расширением — значит оставить предупреждение
    навсегда.
    """
    for katalog in ("alerting", "plugins"):
        put = MONITORING / "grafana" / "provisioning" / katalog
        assert put.is_dir(), (
            f"каталог {katalog} исчез — Grafana ответит на это двумя ошибками "
            "«no such file or directory» при каждом старте"
        )
        fayly = [f for f in sorted(put.iterdir()) if f.is_file()]
        assert fayly, f"в {katalog} нет ни одного файла — каталог не переживёт git"
        for f in fayly:
            assert f.suffix in (".yaml", ".yml", ".json"), (
                f"{f.name} в {katalog}: Grafana принимает только .yaml/.yml/.json "
                "и на всё прочее пишет предупреждение при каждом старте"
            )


def test_node_exporter_ne_sobiraet_to_chego_nikto_ne_sprashivaet():
    """Собиратель, чьих рядов никто не спрашивает, не включается.

    Правило записано рядом с самим списком в docker-compose.yml: «здесь ровно
    то, по чему написаны правила». `diskstats` его нарушал и вдобавок был
    ЕДИНСТВЕННЫМ, кто требовал `/run/udev/data`, которого в контейнере нет, —
    отсюда ошибка на каждом старте: «Failed to open directory, disabling udev
    device properties».

    Проверка узкая нарочно: она стережёт не весь список, а именно тот
    собиратель, который уже принёс ошибку в боевой журнал. Общее правило
    («каждый включённый собиратель кем-то спрашивается») сегодня не выполняется
    ещё для `netdev`, `uname`, `os` и `loadavg`; они молчат, ошибок не дают, и
    снимать их — отдельная работа с отдельным доводом.
    """
    compose = yaml.safe_load(_read(COMPOSE))
    komanda = compose["services"]["node-exporter"]["command"]
    assert not any("diskstats" in str(c) for c in komanda), (
        "`--collector.diskstats` вернулся: он требует /run/udev/data, которого "
        "в контейнере нет, и пишет ошибку при каждом старте — а ряды node_disk_* "
        "не спрашивают ни правила, ни панель"
    )

    pravila = _read(MONITORING / "prometheus" / "rules" / "opencrm.yml")
    panel = _read(MONITORING / "grafana" / "dashboards" / "opencrm-host.json")
    assert "node_disk" not in pravila + panel, (
        "кто-то начал спрашивать node_disk_* — тогда собиратель нужен, и вместе "
        "с ним нужно монтирование /run/udev, иначе ряды придут без меток"
    )
