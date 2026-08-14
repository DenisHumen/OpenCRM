"""Публичная поверхность: всё, до чего дотягивается посторонний из интернета.

Отдельный файл, а не строки в `test_security.py`, потому что вопрос здесь один
и он не про уязвимость, а про ГРАНИЦУ: что вообще отвечает с публичного домена и
на каких условиях. Уязвимость находят и чинят по одной, а граница расползается
незаметно — новым маршрутом без входа, новым `add_header`, который отменил
унаследованные, новым блоком nginx мимо ограничителя. Каждая такая правка
выглядит безобидно в своём коммите и видна только списком целиком.

Отсюда устройство файла: **перечни, а не проверки поштучно.** Список маршрутов
без входа выписан руками и сверяется с настоящим приложением; список блоков
nginx со своими заголовками — тоже. Новая строка, появившаяся мимо списка,
обязана уронить проверку и заставить человека сказать вслух, что она делает
снаружи.

Панель мониторинга (`/monitoring/`) открыта с публичного домена по решению
владельца. Здесь она не запирается — здесь проверяется, что дверь, смотрящая в
интернет, крепкая: ограничитель подбора на месте, заголовки не потерялись,
секретов наружу не уходит.

**Настоящий nginx этими проверками не поднимается.** Конфиг читается как текст —
тем же приёмом и по той же причине, что в `test_monitoring.py` и
`test_deploy_config.py`: словарь зависимостей продукта не должен расти ради
тестов. Поведение живого nginx проверялось руками на стенде (настоящий nginx,
настоящий сертификат, настоящая `grafana/grafana:11.5.2`), и снятые там числа
выписаны в комментариях к `docker/nginx/templates/hardening.inc`.
"""

import re
from pathlib import Path

import pytest

#: Приставка API. Своей строкой, а не импортом из `conftest`: половина проверок
#: здесь читает конфиги nginx и не нуждается ни в базе, ни в приложении, а
#: импорт `conftest` на уровне модуля требует поднятой MySQL ещё до сбора
#: тестов. Разница не теоретическая: без базы эти проверки гоняются на любой
#: машине за секунду — в том числе когда проверяют сами себя подменой конфига.
API = "/api/v1"

ROOT = Path(__file__).resolve().parent.parent
NGINX = ROOT / "docker" / "nginx" / "templates"
LOCATIONS = NGINX / "locations.inc"
HEADERS = NGINX / "headers.inc"
HARDENING = NGINX / "hardening.inc"
HTTP_TEMPLATE = NGINX / "http.conf.template"
HTTPS_TEMPLATE = NGINX / "https.conf.template"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- разбор конфига nginx ------------------------------------------------------
#
# Полноценный разборщик здесь не нужен и был бы хуже: проверяем мы не синтаксис
# (его проверяет сам nginx командой `nginx -t` — и на старте контейнера, и перед
# каждым перечитыванием, см. docker/nginx/reload.sh), а РАСКЛАДКУ по блокам.
# Хватает счёта скобок.


def _bloki(config: str) -> dict[str, str]:
    """{заголовок блока: его содержимое} для всех `location`/`server` первого уровня."""
    najdeno: dict[str, str] = {}
    for match in re.finditer(r"^(location[^\n{]*|server)\s*\{", config, re.M):
        zagolovok = match.group(1).strip()
        i = match.end()
        gluboko = 1
        while i < len(config) and gluboko:
            if config[i] == "{":
                gluboko += 1
            elif config[i] == "}":
                gluboko -= 1
            i += 1
        najdeno[zagolovok] = config[match.end():i - 1]
    return najdeno


def _bez_kommentariev(text: str) -> str:
    return "\n".join(s for s in text.splitlines() if not s.lstrip().startswith("#"))


# ==============================================================================
# 1. Что вообще отвечает без входа в систему
# ==============================================================================

#: Маршруты приложения, открытые БЕЗ сессии. Список выписан руками, и это
#: единственный способ поймать новый такой маршрут: он ничем не отличается от
#: обычного, пока кто-нибудь не прочитает его глазами.
#:
#: Рядом с каждым — почему он здесь. Строка без объяснения означает, что маршрут
#: открыли не подумав.
PUBLIC_ROUTES = {
    # витрина клиента: за неё отвечает токен ссылки и PIN
    ("GET", "/b/{token}"),
    ("POST", "/b/{token}/pin"),
    ("GET", "/b/{token}/data"),
    ("GET", "/media/{work_uid}/{filename}"),
    # состояние заказа по QR с квитанции: ограничитель по адресу, 20 за 10 минут
    ("GET", "/d/{number}"),
    # брендинг и аватары: в бою их отдаёт nginx, здесь запасной путь
    ("GET", "/branding/{filename}"),
    ("GET", "/avatars/{filename}"),
    # вход и запрос доступа: им сессия и не нужна
    ("POST", f"{API}/auth/login"),
    ("POST", f"{API}/auth/logout"),
    ("POST", f"{API}/auth/register"),
    # приём заявки с сайта: ключ приёма + ограничитель + ловушка
    ("POST", f"{API}/public/leads"),
    # вебхук АТС: подпись HMAC вместо сессии
    ("POST", f"{API}/telephony/webhook"),
    # змейка со страницы обслуживания: своих данных студии здесь нет
    ("GET", f"{API}/arcade/leaderboard"),
    ("POST", f"{API}/arcade/scores"),
    # метрики: сессии у Prometheus нет и быть не может, снаружи закрыты nginx
    ("GET", f"{API}/metrics"),
    # проверка здоровья: на ней держится откат обновления
    ("GET", "/healthz"),
    # сборка SPA и её отдача
    ("GET", "/{full_path:path}"),
}


def _marshruty(app):
    """(метод, путь, имена зависимостей) для всех маршрутов приложения.

    FastAPI начиная с 0.139 не разворачивает подключённые роутеры в плоский
    `app.routes`, а кладёт обёртку `_IncludedRouter`. Ходим по обеим формам:
    иначе проверка молча увидела бы восемь маршрутов вместо двухсот тридцати и
    зеленела бы, ничего не проверяя.
    """
    from starlette.routing import Mount

    sobrano = []

    def zavisimosti(route):
        imena: list[str] = []

        def obojti(d, gluboko=0):
            if d is None or gluboko > 8:
                return
            call = getattr(d, "call", None)
            if call is not None:
                perm = getattr(call, "opencrm_permission", None)
                imena.append(
                    f"perm:{perm[0]}.{perm[1]}" if perm else getattr(call, "__name__", str(call))
                )
            for sub in getattr(d, "dependencies", []):
                obojti(sub, gluboko + 1)

        dep = getattr(route, "dependant", None)
        if dep is not None:
            for sub in dep.dependencies:
                obojti(sub)
        return set(imena)

    def sobrat(routes, pristavka=""):
        for r in routes:
            if type(r).__name__ == "_IncludedRouter":
                ctx = getattr(r, "include_context", None)
                sobrat(r.original_router.routes, pristavka + (getattr(ctx, "prefix", "") or ""))
                continue
            if isinstance(r, Mount):
                sobrano.append(("MOUNT", pristavka + (r.path or ""), set()))
                continue
            put = getattr(r, "path", None)
            if put is None:
                continue
            deps = zavisimosti(r)
            for metod in getattr(r, "methods", None) or ["GET"]:
                if metod in ("HEAD", "OPTIONS"):
                    continue
                sobrano.append((metod, pristavka + put, deps))

    sobrat(app.routes)
    return sobrano


ZASHCHITA = {"require_staff", "get_current_user", "require_root"}


def test_spisok_otkrytykh_marshrutov_ne_ros_sam_soboy():
    """Маршрут без входа в систему появляется только вместе со строкой в списке.

    Проверка ловит ровно один вид ошибки, зато самый частый: новый маршрут
    написали, зависимость на права приписать забыли. Снаружи это выглядит как
    работающий раздел — ошибки нет ни одной, отказа тоже, — и обнаруживается по
    чужому обращению, а не по красному прогону.
    """
    from web.main import app

    otkryty = {
        (metod, put)
        for metod, put, deps in _marshruty(app)
        if metod != "MOUNT"
        and not any(d.startswith("perm:") or d in ZASHCHITA for d in deps)
        # документация API в боевом окружении не поднимается вовсе — проверка ниже
        and not put.startswith(("/api/docs", "/api/openapi", "/docs", "/redoc"))
    }

    lishnie = otkryty - PUBLIC_ROUTES
    propali = PUBLIC_ROUTES - otkryty
    assert not lishnie, (
        "открыты без входа в систему и не названы в PUBLIC_ROUTES: "
        f"{sorted(lishnie)}. Если так и задумано — впишите строку и объясните, "
        "чем этот адрес защищён вместо сессии."
    )
    assert not propali, (
        f"в PUBLIC_ROUTES перечислено то, чего в приложении больше нет: {sorted(propali)}"
    )


def test_dokumentatsiya_api_v_boevom_okruzhenii_ne_podnimaetsya(monkeypatch):
    """`/api/docs`, `/redoc` и схема OpenAPI — карта всего API для постороннего.

    Отдельная проверка, потому что выключены они не запретом, а тем, что
    `openapi_url=None`: FastAPI при этом не заводит НИ ОДНОГО из четырёх
    маршрутов. Связь неочевидная, и вернуть её обратно можно одной строкой,
    добавленной ради удобства отладки.
    """
    from config.settings import get_settings
    from web.main import create_app

    monkeypatch.setattr(get_settings(), "env", "production")
    boevoe = create_app()

    puti = {getattr(r, "path", "") for r in boevoe.routes}
    for adres in ("/api/docs", "/api/openapi.json", "/docs/oauth2-redirect", "/redoc"):
        assert adres not in puti, f"{adres} отвечает в production"


def test_katalogi_so_statikoy_ne_pokazyvayut_spisok_faylov(base_client):
    """Каталог, отдающий список своих файлов, — это карта сборки наружу."""
    for adres in ("/static/", "/assets/"):
        otvet = base_client.get(adres)
        assert otvet.status_code in (404, 405), f"{adres}: {otvet.status_code}"
        assert b"<a href=" not in otvet.content[:2000], f"{adres} отдал список файлов"


# ==============================================================================
# 2. Заголовки: правило наследования add_header в nginx
# ==============================================================================
#
# Правило это ломает конфиги тише всех прочих: `add_header` НЕ НАСЛЕДУЕТСЯ, если
# на текущем уровне есть хоть один свой. Не «добавляется к унаследованным», а
# отменяет их целиком.
#
# Снято с живого стенда ДО правки: `/branding/logo.svg` и `/avatars/*.webp`
# уходили без `Referrer-Policy` и без HSTS, хотя обе строки написаны уровнем
# выше и выглядят действующими. Прочитать это в конфиге нельзя — только
# запросом. Поэтому проверка механическая.

TREBUYUT_NABORA = "include /opencrm/templates/headers.inc;"


def test_u_kazhdogo_bloka_so_svoim_zagolovkom_stoit_obshchiy_nabor():
    """Свой `add_header` в блоке = потеря унаследованных. Значит include обязателен."""
    for fayl in (LOCATIONS, HTTP_TEMPLATE, HTTPS_TEMPLATE):
        config = _bez_kommentariev(_read(fayl))
        for zagolovok, telo in _bloki(config).items():
            if "add_header" not in telo:
                continue
            # у вложенных блоков своих add_header нет, а include внутри telo
            # виден целиком — счёт скобок вернул содержимое блока полностью
            assert TREBUYUT_NABORA in telo, (
                f"{fayl.name}, блок `{zagolovok}` ставит свой add_header и потому "
                "НЕ наследует заголовки уровня server. Добавьте "
                f"`{TREBUYUT_NABORA}` — разбор в headers.inc"
            )


def test_v_bloke_paneli_monitoringa_net_svoikh_zagolovkov():
    """Панель наследует заголовки с уровня server — и обязана продолжать.

    Один `add_header`, дописанный сюда «заодно», отменит унаследованные: панель,
    открытая в интернет, останется без HSTS и без `Referrer-Policy`, а в конфиге
    обе строки по-прежнему будут написаны. Проверено запросом на живом стенде:
    сейчас `/monitoring/` отдаёт и то, и другое.
    """
    bloki = _bloki(_bez_kommentariev(_read(LOCATIONS)))
    for imya in ("location /monitoring/", "location = /monitoring/login"):
        assert imya in bloki, f"блок `{imya}` пропал из конфига"
        assert "add_header" not in bloki[imya], (
            f"в `{imya}` появился свой add_header — унаследованные заголовки "
            "(HSTS, Referrer-Policy, nosniff) при этом ПРОПАДАЮТ ЦЕЛИКОМ"
        )


def test_nabor_zagolovkov_zhivet_v_odnom_meste():
    """Три заголовка описаны один раз, а не по копии на шаблон."""
    nabor = _read(HEADERS)
    for stroka in (
        "add_header X-Content-Type-Options nosniff always;",
        "add_header Referrer-Policy strict-origin-when-cross-origin always;",
        "add_header Strict-Transport-Security $opencrm_hsts always;",
    ):
        assert stroka in nabor, f"из общего набора пропала строка: {stroka}"

    for fayl in (HTTP_TEMPLATE, HTTPS_TEMPLATE, LOCATIONS):
        config = _bez_kommentariev(_read(fayl))
        for zagolovok in ("X-Content-Type-Options", "Referrer-Policy",
                          "Strict-Transport-Security"):
            assert f"add_header {zagolovok}" not in config, (
                f"{fayl.name} описывает `{zagolovok}` мимо headers.inc — две копии "
                "разойдутся при первой же правке одной из них"
            )


def test_hsts_privyazan_k_skheme_a_ne_k_shablonu():
    """HSTS по обычному HTTP браузер обязан игнорировать (RFC 6797).

    Отдавать его там — шум; хуже другое: пока значение было вписано строкой в
    один только https-шаблон, общего набора заголовков не могло существовать
    вовсе. Поэтому значение приезжает переменной, пустой по http, а `add_header`
    с пустым значением nginx не выводит. Проверено на стенде: по http строки в
    ответе нет.
    """
    hardening = _read(HARDENING)
    map_blok = re.search(r"map \$scheme \$opencrm_hsts \{(.*?)\}", hardening, re.S)
    assert map_blok, "пропал map $scheme -> $opencrm_hsts"
    telo = map_blok.group(1)
    assert re.search(r"default\s+\"\"", telo), "по умолчанию HSTS обязан быть пустым"
    assert re.search(r"https\s+\"max-age=\d+\"", telo), "по https HSTS обязан появляться"


def test_kazhdyy_prokhod_vnutr_rasskazyvaet_adres_klienta():
    """`X-Forwarded-For` описан один раз и стоит у каждого, кто проксирует.

    Строка эта не про удобство: от неё зависит `client_ip` в приложении, а от
    него — защита от подбора PIN, пароля и номеров бланков. Проходов внутрь
    шесть (сайт, медиа, два входа в CRM, панель и её вход), и потеряй заголовок
    один из них — ограничитель на этом маршруте начнёт считать всех посетителей
    за одного, не сказав ни слова.
    """
    zagolovki_est = "include /opencrm/templates/proxy-headers.inc;"
    nabor = _read(NGINX / "proxy-headers.inc")
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in nabor, (
        "из общего набора пропал X-Forwarded-For"
    )

    for zagolovok, telo in _bloki(_bez_kommentariev(_read(LOCATIONS))).items():
        if "proxy_pass" not in telo:
            continue
        assert zagolovki_est in telo, (
            f"блок `{zagolovok}` проксирует запрос, не рассказав, кто его прислал: "
            f"добавьте `{zagolovki_est}`"
        )
        assert "proxy_set_header X-Forwarded-For" not in telo, (
            f"блок `{zagolovok}` описывает X-Forwarded-For мимо общего набора — "
            "две копии разойдутся при первой же правке одной из них"
        )


def test_kartinki_nelzya_vstavit_v_chuzhuyu_stranitsu():
    """`default-src 'none'` НЕ закрывает рамку: у frame-ancestors отката нет.

    `sandbox` тоже не закрывает — он глушит скрипт ВНУТРИ файла, а не запрещает
    файлу оказаться в чужой рамке.
    """
    bloki = _bloki(_bez_kommentariev(_read(LOCATIONS)))
    for imya in ("location /branding/", "location /avatars/"):
        politika = re.search(r'add_header Content-Security-Policy "([^"]+)"', bloki[imya])
        assert politika, f"{imya}: пропала политика безопасности"
        assert "frame-ancestors 'none'" in politika.group(1), (
            f"{imya}: политика не запрещает вставку в чужую страницу"
        )


# ==============================================================================
# 3. Ограничители потока на входах
# ==============================================================================
#
# У Grafana защиты от подбора нет вовсе. Замерено на живом стенде с настоящей
# `grafana/grafana:11.5.2`: сорок попыток входа за 0.2 секунды, сорок попыток
# HTTP Basic за 0.1 секунды — все 401 и ни одного препятствия. После правки те
# же сорок попыток дают семь ответов 401 и тридцать три 429, а владелец входит
# верным паролем как раньше.

#: Где стоят ограничители и какими отсеками. Список руками — он и есть проверка.
OGRANICHITELI = {
    "location = /monitoring/login": {"opencrm_panel_login", "opencrm_basic_auth"},
    "location /monitoring/": {"opencrm_basic_auth"},
    "location = /api/v1/auth/login": {"opencrm_crm_login"},
    "location = /api/v1/auth/register": {"opencrm_crm_register"},
}


def test_vkhody_stoyat_pod_ogranichitelem():
    bloki = _bloki(_bez_kommentariev(_read(LOCATIONS)))
    for imya, otseki in OGRANICHITELI.items():
        assert imya in bloki, f"блок `{imya}` пропал из конфига"
        nashli = set(re.findall(r"limit_req zone=(\w+)", bloki[imya]))
        assert nashli == otseki, f"{imya}: отсеки {nashli}, а ожидались {otseki}"


def test_ogranichitel_ne_zadevaet_ostalnoe():
    """Ограничитель стоит на входах и только на них.

    Иначе он однажды окажется на витрине или на медиа — и клиент студии,
    открывший подборку из тридцати работ, получит 429 вместо картинок.
    """
    bloki = _bloki(_bez_kommentariev(_read(LOCATIONS)))
    s_ogranichitelem = {imya for imya, telo in bloki.items() if "limit_req" in telo}
    assert s_ogranichitelem == set(OGRANICHITELI), (
        f"ограничитель появился там, где его не ждали: "
        f"{sorted(s_ogranichitelem - set(OGRANICHITELI))}"
    )


def test_otseki_obyavleny_i_ne_pustye():
    hardening = _bez_kommentariev(_read(HARDENING))
    obyavleny = dict(re.findall(r"zone=(\w+):(\d+m)", hardening))
    for imya in {o for nabor in OGRANICHITELI.values() for o in nabor}:
        assert imya in obyavleny, f"отсек `{imya}` используется, но не объявлен"

    # И оба шаблона обязаны подключить объявления: без include на уровне http
    # nginx отвергнет конфиг целиком — то есть сайт не поднимется.
    for fayl in (HTTP_TEMPLATE, HTTPS_TEMPLATE):
        assert "include /opencrm/templates/hardening.inc;" in _read(fayl), (
            f"{fayl.name} не подключает объявления отсеков"
        )


def test_otkaz_ogranichitelya_eto_429_a_ne_503():
    """503 означает «сайт лежит» — и его ловит `error_page` соседних блоков.

    Оставь мы умолчание, перебор пароля выглядел бы как страница «идут
    технические работы», а владелец пошёл бы чинить несуществующую аварию.
    """
    assert "limit_req_status 429;" in _read(HARDENING)


def test_klyuch_ogranichitelya_ne_prikhodit_ot_klienta():
    """`$http_x_forwarded_for` в качестве ключа обходится одной строкой.

    Тот же довод, что у `client_ip` в приложении (`docs/07-security.md`): всё,
    что клиент пишет сам, ключом ограничителя быть не может — ротация заголовка
    даёт новый отсек на каждый запрос и снимает защиту целиком.
    """
    hardening = _bez_kommentariev(_read(HARDENING))
    for stroka in re.findall(r"limit_req_zone\s+(\S+)", hardening):
        assert stroka in ("$binary_remote_addr", "$opencrm_basic_probe"), (
            f"ключом ограничителя стало `{stroka}`"
        )


def test_otsek_basic_schitaet_tolko_predyavivshikh_parol():
    """Считаются только запросы с `Authorization: Basic`, остальные — мимо.

    На этом держится обещание «владелец ничего не заметит»: браузер работает с
    панелью по cookie `grafana_session` и заголовка `Authorization` не шлёт
    вовсе (проверено на стенде). Пустой ключ nginx не учитывает — это и есть
    механизм. Токен Grafana (`Bearer`) сюда тоже не попадает: у него 128 бит, и
    ломать чужие скрипты незачем.
    """
    hardening = _read(HARDENING)
    blok = re.search(r"map \$http_authorization \$opencrm_basic_probe \{(.*?)\}", hardening, re.S)
    assert blok, "пропал map по заголовку Authorization"
    telo = blok.group(1)
    assert re.search(r'default\s+""', telo), (
        "умолчание обязано быть ПУСТЫМ: иначе в отсек попадут все подряд, "
        "включая обычную работу с панелью"
    )
    assert re.search(r'"~\*\^Basic"\s+\$binary_remote_addr', telo), (
        "отсек больше не привязан к схеме Basic"
    )


# ==============================================================================
# 4. Метрики приложения
# ==============================================================================


def test_metriki_zakryty_i_tochnym_sovpadeniem_i_prefiksom():
    """Точного совпадения оказалось мало: `/api/v1/metrics/` ему не равен.

    Проверено запросами по живому nginx: до правки хвост со слэшем уходил в
    приложение. Утечки не было по случайности — Starlette отвечал на него
    перенаправлением обратно на запрещённый адрес, — но `redirect_slashes` это
    настройка фреймворка, а не наше решение.
    """
    config = _bez_kommentariev(_read(LOCATIONS))
    for zagolovok in ("location = /api/v1/metrics", "location ^~ /api/v1/metrics"):
        blok = _bloki(config).get(zagolovok)
        assert blok is not None, f"пропал блок `{zagolovok}`"
        assert "deny all;" in blok, f"`{zagolovok}` больше не закрывает метрики"


# ==============================================================================
# 5. Публичные ссылки, PIN и адрес клиента
# ==============================================================================


def test_uvicorn_ne_perepisyvaet_adres_klienta_sam():
    """`--no-proxy-headers` — половина защиты от подбора PIN, и она в одной строке.

    Без флага uvicorn перепишет `request.client` по `X-Forwarded-For` ещё до
    того, как до заголовка доберётся `client_ip`, — то есть адрес пира окажется
    подделан, и вся аккуратность разбора заголовка в `web/api/deps.py` перестанет
    значить что-либо. Флаг живёт в чужом файле и снимается одним словом, поэтому
    сторож здесь, а не там.

    Комментарии отбрасываются, и это не мелочь: рядом с самим запуском в
    entrypoint.sh лежит абзац, объясняющий флаг, — то есть слово `--no-proxy-headers`
    остаётся в файле и после того, как флаг сняли с команды. Проверка на
    вхождение в текст целиком была бы зелёной при снятой защите. Так и вышло при
    первой же проверке этой проверки подменой.
    """
    if not ENTRYPOINT.exists():
        pytest.skip("вне репозитория: docker/entrypoint.sh не входит в образ")
    komanda = _bez_kommentariev(_read(ENTRYPOINT))
    assert "--no-proxy-headers" in komanda, (
        "uvicorn запускается без --no-proxy-headers: подделанный X-Forwarded-For "
        "снова становится адресом клиента"
    )
    assert "--proxy-headers" not in komanda.replace("--no-proxy-headers", ""), (
        "в команде запуска остался --proxy-headers"
    )


def test_neizvestnyy_token_nelzya_otlichit_ot_zakrytoy_ssylki(manager_client):
    """По ответу нельзя понять, существует ли доска: перебор ничего не сообщает.

    Токен — `secrets.token_urlsafe(16)`, 128 бит: перебрать его нельзя. Но
    отличимый ответ превратил бы перебор из «подобрать ссылку» в «пересчитать,
    сколько у студии досок», а это уже посильная задача.
    """
    from fastapi.testclient import TestClient

    from tests.conftest import png_bytes
    from web.main import app

    client = TestClient(app)
    doska = manager_client.post(f"{API}/boards", json={"title": "Скрытая"}).json()
    manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{doska['id']}", json={"is_published": True})
    ssylka = manager_client.post(f"{API}/boards/{doska['id']}/shares", json={}).json()

    manager_client.patch(f"{API}/shares/{ssylka['id']}", json={"is_active": False})

    otozvana = client.get(f"/b/{ssylka['token']}")
    vydumana = client.get("/b/etogo-tokena-nikogda-ne-bylo")

    assert otozvana.status_code == vydumana.status_code == 404
    assert otozvana.content == vydumana.content, (
        "отозванная ссылка и выдуманный токен отвечают по-разному — "
        "по ответу видно, что доска существует"
    )


def test_token_ssylki_dlinnee_lyubogo_perebora():
    """128 бит энтропии. Проверка не на длину строки, а на источник случайности."""
    import inspect

    from core.security import tokens

    tokeny = {tokens.new_share_token() for _ in range(200)}
    assert len(tokeny) == 200, "токены повторяются"
    assert all(len(t) >= 22 for t in tokeny), "токен стал короче 128 бит"
    istochnik = inspect.getsource(tokens.new_share_token)
    assert "secrets." in istochnik, "токен перестал браться из secrets"


def test_cookie_propuska_po_pin_pomechena_kak_polozheno(manager_client, monkeypatch):
    """Пропуск, выданный за верный PIN: HttpOnly, SameSite и Secure за HTTPS.

    Secure проверяется через настройку, а не через ответ: набор гоняется на
    `http://testserver`, и там флаг не ставится намеренно — иначе документированный
    сценарий «сервер в локальной сети» ломался бы вчистую (`docs/07-security.md`).
    Проверяем оба: что за HTTPS флаг появляется и что остальные два стоят всегда.
    """
    from fastapi.testclient import TestClient

    from config.settings import get_settings
    from tests.conftest import png_bytes
    from web.main import app

    client = TestClient(app)
    doska = manager_client.post(f"{API}/boards", json={"title": "С кодом"}).json()
    manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{doska['id']}", json={"is_published": True})
    ssylka = manager_client.post(
        f"{API}/boards/{doska['id']}/shares", json={"pin": "7391"}
    ).json()

    otvet = client.post(
        f"/b/{ssylka['token']}/pin", data={"pin": "7391"}, follow_redirects=False
    )
    assert otvet.status_code == 303, otvet.text
    postavlena = otvet.headers["set-cookie"]
    assert "HttpOnly" in postavlena, "пропуск читается из JavaScript"
    assert "samesite=lax" in postavlena.lower(), "пропуск уезжает с чужого сайта"
    assert "Max-Age" in postavlena, "пропуск переживает закрытие браузера без срока"

    settings = get_settings()
    monkeypatch.setattr(type(settings), "cookies_secure", property(lambda self: True))
    za_https = client.post(
        f"/b/{ssylka['token']}/pin", data={"pin": "7391"}, follow_redirects=False
    )
    assert "Secure" in za_https.headers["set-cookie"], "за HTTPS пропуск идёт без Secure"


def test_cookie_sessii_i_csrf_pomecheny_kak_polozheno(root_client, monkeypatch):
    """Сессия — HttpOnly; cookie CSRF читается фронтендом и потому нет.

    Secure обеим ставится по схеме `base_url`; проверяем через настройку, как и
    у пропуска по PIN.
    """
    from fastapi.testclient import TestClient

    from config.settings import get_settings
    from tests.conftest import ROOT_EMAIL, ROOT_PASSWORD
    from web.main import app

    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(type(settings), "cookies_secure", property(lambda self: True))
    otvet = client.post(
        f"{API}/auth/login", json={"email": ROOT_EMAIL, "password": ROOT_PASSWORD}
    )
    assert otvet.status_code == 200, otvet.text

    postavleny = otvet.headers.get_list("set-cookie")
    sessiya = next(c for c in postavleny if c.startswith("opencrm_session="))
    csrf = next(c for c in postavleny if c.startswith("opencrm_csrf="))

    assert "HttpOnly" in sessiya, "cookie сессии читается из JavaScript"
    assert "Secure" in sessiya and "Secure" in csrf, "за HTTPS cookie идут без Secure"
    for c in (sessiya, csrf):
        assert "samesite=lax" in c.lower(), "cookie уезжает с чужого сайта"
    assert "HttpOnly" not in csrf, (
        "cookie CSRF стала HttpOnly — фронтенд не сможет вернуть её заголовком, "
        "и все изменяющие запросы начнут отвечать 403"
    )


def test_publichnye_puti_perechisleny_vmeste_s_ikh_blokom():
    """Каждый публичный путь обязан знать, каким блоком системы он закрывается.

    Выключенный блок исчезает целиком — меню, API, настройки, отчёты
    (`CLAUDE.md`, `docs/11-modules.md`). Публичные пути про это правило забывают
    чаще всех: они лежат не в `web/api/routes/`, где закрытие блоком стоит
    зависимостью на роутере, а в `web/public/`, где закрывать надо руками. Тогда
    «выключено» означает лишь «не видно в меню», а старая ссылка продолжает
    отдавать работы клиента всему интернету.

    Проверка читает исходник и требует, чтобы у каждого публичного пути,
    принадлежащего необязательному блоку, стояла проверка этого блока. Само
    поведение проверяют `tests/test_modules.py` (бланки) и
    `tests/test_boards.py`; здесь — перечень, чтобы новый публичный путь не
    появился без строки.

    Проверка читает исходник и потому слаба по устройству: она видит, что имя
    блока в файле упомянуто, но не то, что закрыты ВСЕ его пути. Настоящую
    работу делает соседняя проверка ниже — она выключает блок и стучится.
    """
    istochnik = (ROOT / "web" / "public" / "routes.py").read_text(encoding="utf-8")
    trebuetsya = {"documents": "/d/{number}", "boards": "/b/{token}"}
    ne_zakryty = [
        blok for blok in trebuetsya if f'is_enabled(db, "{blok}")' not in istochnik
    ]
    assert not ne_zakryty, (
        "публичные пути отвечают, когда их блок выключен: "
        + ", ".join(f"{trebuetsya[b]} (блок `{b}`)" for b in ne_zakryty)
        + ". Выключенный блок обязан закрывать и то, что открыто без входа."
    )


def test_vyklyuchennye_doski_zakryvayut_vydannye_ssylki(root_client, manager_client):
    """Выключенный блок исчезает ЦЕЛИКОМ — включая уже разосланные ссылки.

    Проверка поведенческая, а не по тексту исходника, и разница тут не
    формальная: путей у витрины четыре, а поиск имени блока в файле проходит,
    если закрыт хотя бы один. Три открытых из четырёх выглядели бы как починка.

    Что было: блок `boards` выключался, а `/b/<токен>`, `/b/<токен>/data` и
    `/media/<uid>/<файл>` продолжали отдавать работы клиента всему интернету.
    То есть «выключил доски» означало лишь «убрал из своего интерфейса», а
    человек, выключивший блок именно ради прекращения показа, узнал бы об этом
    только от того, кому ссылка попала в руки.

    Отказ выглядит как «нет такого», а не «есть, но не покажем»: второе
    подтверждало бы, что подборка существует.
    """
    from fastapi.testclient import TestClient

    from tests.conftest import API
    from tests.test_shares import _published_board, _share
    from web.main import app

    board = _published_board(manager_client)
    share = _share(manager_client, board["id"])
    gost = TestClient(app)

    # Пока блок включён, ссылка работает — иначе проверка ничего не значит.
    assert gost.get(f"/b/{share['token']}").status_code == 200
    raboty = gost.get(f"/b/{share['token']}/data").json()["works"]
    assert raboty, "витрина пуста — проверять закрытие нечего"
    # Адрес требуется, а не берётся «если есть»: витрина отдаёт только готовые
    # работы (`only_ready=True`), значит производные файлы у них уже собраны.
    # Условная проверка молча пропускала бы главный путь — тот, по которому
    # уходит сам файл клиента.
    adres_fayla = (raboty[0].get("media") or {}).get("large")
    assert adres_fayla, f"у готовой работы нет адреса файла: {raboty[0]}"

    root_client.post(f"{API}/modules/boards", json={"enabled": False})
    try:
        assert gost.get(f"/b/{share['token']}").status_code == 404
        assert gost.get(f"/b/{share['token']}/data").status_code == 404
        assert gost.post(f"/b/{share['token']}/pin", data={"pin": "0000"}).status_code == 404
        otvet = gost.get(adres_fayla)
        assert otvet.status_code == 404, f"файл работы всё ещё отдаётся: {adres_fayla}"
    finally:
        root_client.post(f"{API}/modules/boards", json={"enabled": True})

    # И возвращается вместе с блоком: закрывать навсегда — не то же самое.
    assert gost.get(f"/b/{share['token']}").status_code == 200
