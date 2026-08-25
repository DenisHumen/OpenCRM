"""Обвязка развёртывания: nginx, compose, .dockerignore.

Самые дорогие поломки этого проекта случились не в коде, а в конфигурации, и ни
один тест их не видел. Здесь — сторожа ровно на те места, где уже обжигались:
каждая проверка описывает, что именно ломалось на живом сервере.

Проверки читают файлы как текст, а не разбирают YAML: словарь зависимостей
приложения не должен расти ради тестов, а формат этих файлов меняется редко.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker" / "docker-compose.yml"
LOCATIONS = ROOT / "docker" / "nginx" / "templates" / "locations.inc"
HTTPS_TEMPLATE = ROOT / "docker" / "nginx" / "templates" / "https.conf.template"
MAINTENANCE = ROOT / "docker" / "nginx" / "maintenance" / "maintenance.html"
DOCKERIGNORE = ROOT / ".dockerignore"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


MYSQL_URL = "mysql+pymysql://opencrm:p@db:3306/opencrm?charset=utf8mb4"

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- иконка вкладки ---


def test_icon_files_are_where_the_pages_point():
    """Ссылка на иконку и сам файл живут в разных местах и легко расходятся.

    Вкладка с пустым глобусом — это не ошибка в логах и не упавший тест: её
    просто никто не замечает, пока не посмотрит на браузер.
    """
    static = ROOT / "web" / "public" / "static"
    for name in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png"):
        assert (static / name).is_file(), f"нет файла иконки: {name}"

    templates = ROOT / "web" / "public" / "templates"
    pages = [
        ROOT / "web" / "frontend" / "crm" / "index.html",
        templates / "base.html",
        templates / "maintenance_mode.html",
        # Появляется вместе со своей функцией — проверяем, только если он есть.
        templates / "document_status.html",
    ]
    checked = 0
    for page in pages:
        if not page.is_file():
            continue
        assert 'rel="icon"' in _read(page), f"страница без иконки: {page.name}"
        checked += 1
    assert checked >= 3, "проверять оказалось нечего — список страниц устарел"


def test_png_icon_exists_for_browsers_without_svg_support():
    """SVG-иконку понимают Chrome, Edge и Firefox, а Safari до 16-й — нет.

    Без растрового запасного варианта у части посетителей вкладка осталась бы
    пустой, и заметить это по логам невозможно.
    """
    index = _read(ROOT / "web" / "frontend" / "crm" / "index.html")
    assert 'type="image/png"' in index, "нет растровой иконки для старых браузеров"


def test_showcase_prefers_the_studio_own_logo():
    """Витрину открывает клиент студии, а не мы: в его вкладке должен стоять
    знак студии, если она его загрузила, и только иначе — знак продукта."""
    base = _read(ROOT / "web" / "public" / "templates" / "base.html")
    assert "site.brand_logo_path" in base, "витрина всегда показывает чужой знак"


# --- проверка здоровья ---


def test_healthz_is_not_behind_the_https_redirect():
    """Свои проверки ходят по http://127.0.0.1/healthz — с этой же машины.

    Когда домен переехал на HTTPS, блок на 80 порту стал редиректить всё
    подряд, и проверки начали получать 301 вместо ответа. Сайт объявлялся
    мёртвым, будучи живым; на этом же падал health-check деплоя — обновление
    откатывалось само, откат тоже «не поднимался», приходило «нужен человек».

    Гнать проверки через HTTPS нельзя: сертификат выписан на домен, а не на
    127.0.0.1, и обращение по имени домена с самого сервера упирается в hairpin
    NAT. Проверка здоровья не должна зависеть ни от TLS, ни от DNS.
    """
    text = _read(HTTPS_TEMPLATE)
    port80 = text[: text.index("listen 443")]
    assert "location = /healthz" in port80, "/healthz снова уедет в редирект на HTTPS"
    # Резолвер нужен и в этом блоке: адрес приложения берётся по имени, а блок
    # на 443 со своим resolver'ом сюда не распространяется.
    assert "resolver" in port80, "без резолвера proxy_pass по имени не заработает"
    assert re.search(r"proxy_pass\s+http://\$", port80), (
        "имя приложения записано литералом — nginx запомнит адрес навсегда"
    )


def test_app_healthcheck_has_a_start_period():
    """Без окна запуска отсчёт неудач идёт с первой секунды.

    Сразу после `up -d --build` машина занята сборкой, диск холодный, а
    приложению надо снять копию базы и прогнать миграции. На боевом VPS в
    бюджет interval × retries не уложились: контейнер объявили нездоровым,
    деплой откатился, откат тоже не поднялся.
    """
    healthcheck = _read(COMPOSE)
    healthcheck = healthcheck[healthcheck.index("healthcheck:") :]
    assert "start_period" in healthcheck, "нет окна запуска — медленный старт снова уронит деплой"


def test_nginx_does_not_wait_for_the_app_to_be_healthy():
    """Иначе медленное приложение уносит сайт целиком.

    nginx не обращается к приложению при старте — имя резолвится на каждый
    запрос, — поэтому ждать здоровья ему незачем. А пока он ждал, 80 и 443 не
    слушал никто: посетитель получал отказ соединения вместо страницы
    обслуживания, которая для этого промежутка и сделана.
    """
    text = _read(COMPOSE)
    nginx = text[text.index("  nginx:") : text.index("  certbot:")]
    assert "condition: service_started" in nginx, (
        "nginx снова ждёт healthy — медленный старт приложения уронит весь сайт"
    )


# --- nginx: адрес приложения ---


def test_nginx_resolves_the_app_on_every_request():
    """Обновление пересоздаёт контейнер, и адрес в сети compose меняется.

    Имя, записанное в proxy_pass литералом, nginx резолвит один раз при разборе
    конфига. Проверено на стенде: приложение здорово, а сайт отдаёт 502, потому
    что nginx стучится по адресу, которого больше нет. Само не чинится — nginx
    не падает, и `restart: unless-stopped` его не трогает.

    Лечится парой: встроенный DNS Docker плюс ПЕРЕМЕННАЯ в proxy_pass. Одного
    резолвера мало — без переменной до него дело не доходит, поэтому проверяем
    оба условия вместе.
    """
    config = _read(LOCATIONS)
    assert "resolver 127.0.0.11" in config, "нет встроенного DNS Docker"

    upstream = re.search(r"proxy_pass\s+(\S+);", config)
    assert upstream, "в конфиге вообще нет proxy_pass"
    assert "$" in upstream.group(1), (
        "proxy_pass без переменной: nginx запомнит адрес контейнера навсегда"
    )


# --- nginx: страница на время обновления ---


def test_a_missing_app_shows_the_maintenance_page():
    config = _read(LOCATIONS)
    assert re.search(r"error_page\s+502\s+503\s+504\s+=503", config), (
        "ошибки шлюза должны становиться 503 со страницей обслуживания"
    )
    assert MAINTENANCE.exists(), "страницы обслуживания нет на диске"
    assert "/opencrm/maintenance/maintenance.html" in config


def test_the_maintenance_page_is_mounted_into_nginx():
    assert "./nginx/maintenance:/opencrm/maintenance:ro" in _read(COMPOSE), (
        "без монтирования nginx отдаст свою страницу «502 Bad Gateway»"
    )


def test_the_maintenance_page_asks_for_nothing_from_outside():
    """Страница показывается ровно тогда, когда приложения нет.

    Любая внешняя ссылка — шрифт, картинка, скрипт — ведёт на тот же лежащий
    сайт: в лучшем случае страница едет дольше, в худшем разъезжается. Поэтому
    запрещены не скрипты как таковые (змейка и опрос /healthz без них не
    работают), а всё, что пришлось бы откуда-то загружать: страница обязана
    открыться одним файлом.
    """
    page = _read(MAINTENANCE)
    assert not re.search(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', page), (
        "на странице обслуживания есть внешняя ссылка"
    )
    assert not re.search(r"<script[^>]*\ssrc\s*=", page, re.I), (
        "скрипт подключается файлом — его неоткуда взять, пока сайт лежит"
    )
    assert not re.search(r"<link[^>]*stylesheet", page, re.I), (
        "стили подключаются файлом — их неоткуда взять, пока сайт лежит"
    )


def test_the_maintenance_page_keeps_working_without_javascript():
    """meta-обновление — единственный возврат на сайт с отключённым JS.

    Оно обязано лежать внутри <noscript>. Убрать meta скриптом нельзя: браузер
    планирует перезагрузку при разборе документа, и удаление тега её уже не
    отменяет — страница перезагружалась прямо во время партии в змейку. Внутри
    <noscript> тег просто не применяется, когда скрипты работают.
    """
    page = _read(MAINTENANCE)
    refresh = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]*>', page, re.I)
    assert refresh, "без meta-обновления страница без JS никогда не вернётся на сайт"

    inside = re.search(
        r"<noscript>\s*<meta[^>]+http-equiv=[\"']refresh[\"'][^>]*>\s*</noscript>", page, re.I
    )
    assert inside, "meta-обновление вне <noscript> перезагрузит страницу посреди игры"


def test_the_maintenance_page_never_hides_a_real_error():
    """`proxy_intercept_errors` подменял бы страницей и 404, и 500 приложения."""
    assert "proxy_intercept_errors on" not in _read(LOCATIONS)


def test_the_certificate_check_survives_the_maintenance_window():
    """Продление сертификата ходит по HTTP в тот же nginx.

    Отдай мы на этот адрес страницу обслуживания — продление провалилось бы
    молча, и через 90 дней сайт остался бы без сертификата.
    """
    for template in ("http.conf.template", "https.conf.template"):
        config = _read(ROOT / "docker" / "nginx" / "templates" / template)
        assert "/.well-known/acme-challenge/" in config
        assert "root /var/www/certbot" in config


# --- compose ---


def test_the_production_image_carries_no_tests():
    assert re.search(r"target:\s*app", _read(COMPOSE)), (
        "без явного этапа compose соберёт последний — а это `tests` с pytest внутри"
    )


def test_container_logs_cannot_eat_the_disk():
    """По умолчанию json-лог растёт без предела.

    Сайт живёт месяцами без присмотра, а кончившееся место ломает не логи —
    останавливаются загрузка файлов и сами обновления.
    """
    compose = _read(COMPOSE)
    assert "max-size" in compose
    # Якорь у каждого сервиса, а не один на файл: забыть его у нового сервиса
    # так же легко, как у старого.
    assert compose.count("logging: *logging") >= 3


def test_the_number_of_proxies_in_front_stays_configurable():
    """`environment` перекрывает `env_file`, поэтому значение в config/.env
    молча не работало бы. Настройка должна приходить снаружи, иначе сайт за
    Cloudflare увидит вместо посетителей пару адресов самого Cloudflare."""
    assert "OPENCRM_TRUSTED_PROXY_HOPS: \"${OPENCRM_TRUSTED_PROXY_HOPS:-1}\"" in _read(COMPOSE)


# --- контекст сборки ---


def test_secrets_never_reach_an_image_layer():
    """Слои образа читает любой, у кого есть образ."""
    ignore = _read(DOCKERIGNORE)
    for secret in ("config/.env", "docker/.env"):
        assert secret in ignore


def test_the_deploy_gate_keeps_the_files_it_needs():
    """Строки `tests/` или `deploy/` здесь сломали бы этап `tests` — то есть
    единственную проверку между сломанным коммитом и живым сайтом."""
    lines = [line.strip() for line in _read(DOCKERIGNORE).splitlines()]
    meaningful = [line for line in lines if line and not line.startswith("#")]
    for needed in ("tests/", "deploy/", "tests", "deploy"):
        assert needed not in meaningful


# --- CI ---


def test_ci_runs_the_suite_in_reverse_order_too():
    """Второй прогон — в обратном порядке файлов.

    База у тестов общая на всю сессию, и тест, оставивший после себя настройку
    или полсотни записей в журнале, роняет не себя, а соседа. При одном и том
    же порядке такая связь держится годами и вскрывается в тот день, когда файл
    переименовали. Двое таких нашлось при первом же обратном прогоне: один
    читал английскую надпись витрины, переключённой соседом на русский, второй
    считал записи журнала страницей в полсотни строк.

    Обратный порядок, а не случайный: прогон либо зелёный всегда, либо красный
    всегда. «Мигающих» тестов, которые ловят повторами и чинят по настроению,
    так не появляется.
    """
    if not WORKFLOW.exists():
        pytest.skip("вне репозитория: .github не входит в контекст сборки образа")

    workflow = _read(WORKFLOW)
    assert "sort -r" in workflow, "обратный прогон из CI пропал"
    assert workflow.count("python -m pytest") >= 2, "прогонов тестов меньше двух"


def test_ci_builds_the_frontend_before_running_tests():
    """CI обещает гонять «те же тесты, что автообновление перед деплоем».

    В образе фронтенд собирает отдельный этап node, а web/frontend/crm/dist в
    git не хранится. Без сборки в CI web/main.py не регистрирует маршруты SPA,
    и проверки витрины CRM падают на ровном месте — что и случилось на первом же
    прогоне. Заодно без этого шага ошибка TypeScript проезжает через зелёный CI
    и всплывает уже сборкой на сервере.
    """
    if not WORKFLOW.exists():
        # .github/ намеренно исключён из контекста сборки (.dockerignore), так
        # что внутри образа проверять нечего. На самом GitHub файл на месте.
        pytest.skip("вне репозитория: .github не входит в контекст сборки образа")

    workflow = _read(WORKFLOW)
    assert "npm ci" in workflow, "фронтенд в CI не устанавливается"
    assert "npm run build" in workflow, "фронтенд в CI не собирается"
    assert workflow.index("npm run build") < workflow.index("python -m pytest"), (
        "сборка фронтенда должна идти до тестов, иначе dist ещё нет"
    )


def test_several_workers_without_redis_are_refused():
    """Несколько процессов законны только с общим счётчиком попыток.

    Пока условие было одно — «база не SQLite», — переезд на MySQL разрешал
    восемь процессов разом и вместе с запретом снимал единственное, на чём
    держалась защита от подбора: счётчик жил в памяти процесса. Восемь
    процессов дают восемь независимых счётчиков, порог умножается на восемь, и
    никакой ошибки при этом не возникает.

    Отказ стоит в двух местах, и оба нужны: приложение знает про
    `OPENCRM_WORKERS`, а entrypoint — тот, кто это число передаёт uvicorn.
    """
    from config.settings import Settings

    bez_redis = Settings(workers=2, db_url=MYSQL_URL, redis_url="")
    zhaloby = " ".join(bez_redis.config_errors())
    assert "OPENCRM_WORKERS" in zhaloby, "приложение молча берётся за невозможное"
    assert "OPENCRM_REDIS_URL" in zhaloby, "отказ не подсказывает выход"

    # С общим счётчиком несколько процессов законны — запрет не должен мешать.
    s_redis = Settings(workers=2, db_url=MYSQL_URL, redis_url="redis://:p@redis:6379/0")
    assert not [x for x in s_redis.config_errors() if "OPENCRM_WORKERS" in x], (
        "запрет срабатывает там, где всё в порядке"
    )

def test_propavshiy_config_ne_podnimaet_prod_v_dev_rezhime():
    """`config/.env` подключён с `required: false` — и это осознанно: без него
    первый запуск до установки не поднялся бы вовсе.

    Оборотная сторона: пропади файл (снесли, сломалось монтирование, перепутали
    путь), и compose поднимет контейнер молча, а приложение возьмёт значения по
    умолчанию — dev-режим и всем известный ключ подписи cookie. Снаружи это
    выглядит как работающий сайт, а PIN-доступ к доскам подделывается ключом
    из репозитория.

    Развёртывание в dev-режиме не бывает законным: образ собирают, чтобы им
    пользовались, а не чтобы разрабатывать внутри него.
    """
    from config.settings import Settings

    lost = Settings(deployed=True, env="dev")
    assert any("config/.env" in problem for problem in lost.config_errors()), lost.config_errors()

    # На ноутбуке тот же dev-режим — норма, и мешать ему нельзя.
    laptop = Settings(deployed=False, env="dev")
    assert laptop.config_errors() == []


def test_obraz_pomechen_kak_razvyortyvanie():
    """Флаг зашит в образ, поэтому снаружи его не потерять.

    Сторож: убрать строку из Dockerfile легко — выглядит она как лишняя
    переменная, — а без неё проверка выше перестанет срабатывать молча.
    """
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV OPENCRM_DEPLOYED=1" in dockerfile, "образ перестал помечаться как развёртывание"
    # Этап тестов флаг снимает: набор развёртыванием не является.
    assert "ENV OPENCRM_DEPLOYED=0" in dockerfile


def test_kopiya_pered_migratsiyami_ne_zatiraetsya():
    """Копия, снятая ДО миграции, не имеет права быть затёртой состоянием ПОСЛЕ.

    Прежде файл был один и обновлялся на каждом старте контейнера — включая
    перезапуски, перезагрузку машины и цикл рестартов после падения. Схема
    беды: миграция испортила данные, контейнер перезапустился, копия затёрлась.
    К моменту, когда это замечали, возвращаться было не к чему.
    """
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    # Имя копии зависит от ревизии — значит на разных ревизиях это разные файлы.
    assert "pre-migrate-$CURRENT" in entrypoint, "копия перестала зависеть от ревизии"
    # И существующая не перезаписывается: повторный старт на той же ревизии
    # ничего не трогает.
    assert 'if [ -f "$SNAPSHOT" ]' in entrypoint, "копия снова перезаписывается на каждом старте"
    # Ровно одного файла `.pre-migrate` больше нет — иначе старое поведение
    # вернулось бы вместе со строкой, которую забыли убрать.
    assert ".backup '$DB_FILE.pre-migrate'" not in entrypoint


# --- ворота деплоя ---

COMPOSE_TESTS = ROOT / "docker" / "docker-compose.tests.yml"


def test_vorota_podmenyayut_tochku_vhoda_a_ne_tolko_komandu():
    """Иначе ворота поднимут веб-сервер вместо набора — и молча.

    `compose` подменяет команду, но НЕ точку входа, а точка входа образа —
    `docker/entrypoint.sh`. Он аргументы дальше не передаёт вовсе: ждёт базу,
    накатывает миграции и кончается `exec python -m uvicorn`. То есть `command:`
    в службе `tests` без `entrypoint:` отбрасывается целиком.

    Поймано живьём: контейнер ворот ушёл в проверки боевого окружения из точки
    входа, упал на правах каталога данных и не запустил ни одного теста. Молчание
    тут особенно дорого — «тесты прошли» и «тесты не запускались» выглядят на
    воротах одинаково, а за воротами живой сайт.
    """
    text = _read(COMPOSE_TESTS)
    tests_block = text.split("  tests:", 1)[1]
    assert "entrypoint:" in tests_block, (
        "у службы tests нет entrypoint: — набор не запустится, а ворота этого "
        "не заметят"
    )


def test_vorota_beryot_kod_vozvrata_u_nabora():
    """Без `--exit-code-from tests` красный набор проехал бы зелёным.

    `compose up` отдаёт свой код, а не код службы: контейнер, упавший на
    провалившемся тесте, — это для компоуза обычное завершение. Флаг называет,
    чей код считать ответом.
    """
    text = _read(COMPOSE_TESTS)
    assert "--exit-code-from tests" in text, (
        "в подсказке к запуску нет --exit-code-from tests"
    )
    updater = _read(ROOT / "deploy" / "updater.py")
    assert '"--exit-code-from", "tests"' in updater, (
        "обновление зовёт ворота без --exit-code-from tests"
    )


def test_baza_vorot_ne_boevaya():
    """Набор портит схему нарочно — делать это в боевой базе нельзя.

    Своё имя проекта даёт свою сеть и свои имена контейнеров: перепутать со
    стеком сайта нельзя даже опечаткой. Данные в tmpfs — после прогона не
    остаётся ничего.
    """
    text = _read(COMPOSE_TESTS)
    assert "name: opencrm-tests" in text, "у ворот нет своего имени проекта"
    assert "tmpfs:" in text, "база ворот пишет на диск, а должна в память"


# --- число рабочих процессов доезжает до контейнера --------------------------


def test_chislo_vorkerov_doezzhaet_do_konteynera():
    """`OPENCRM_WORKERS` обязан быть в окружении службы `app`.

    **НАЙДЕНО РАЗБОРОМ, и это была немота.** Переменная описана в
    `docker/.env.example`, но `docker/.env` — файл подстановки переменных
    COMPOSE: его значения идут в `${...}` внутри YAML и в контейнер сами по себе
    не попадают. Службе `app` переменная не передавалась ни через `env_file`
    (там `config/.env`), ни в `environment`.

    Значит выставивший её не получал ничего: `entrypoint.sh` читал умолчание,
    приложение видело один процесс, и ни ошибки, ни строчки в логе. Хуже самой
    немоты то, что она обесценивает замеры: подняв число процессов и не увидев
    разницы, легко заключить «многопроцессность не помогает», хотя процесс всё
    это время был один.

    Тот же класс беды, что уже разобран у `OPENCRM_TRUSTED_PROXY_HOPS`: там
    `environment` перекрывает `env_file`, и забытая строка молча ничего не
    делает.
    """
    stroki = (ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8").splitlines()

    # Вырезаем блок службы `app` построчно, а не разбором YAML: разборщик — это
    # ещё одна зависимость, которой у набора нет, а нам нужно ровно одно —
    # понять, где кончается служба. Кончается она на следующей строке с тем же
    # отступом в два пробела: так устроен файл, и так его читает сам compose.
    vnutri = False
    blok_strok: list[str] = []
    for stroka in stroki:
        if stroka.startswith("  app:"):
            vnutri = True
            continue
        if vnutri:
            golaya = stroka.strip()
            if golaya and not stroka.startswith("    ") and not stroka.startswith("#"):
                break
            blok_strok.append(stroka)
    blok = "\n".join(blok_strok)
    assert blok.strip(), "не нашлось службы `app` — проверка смотрит не туда"
    assert "env_file:" in blok, "вырезали не тот блок: у службы `app` есть env_file"

    assert "OPENCRM_WORKERS:" in blok, (
        "число рабочих процессов не передаётся контейнеру — выставить его можно, "
        "а подействует оно никогда, и молча"
    )
    # Со значением по умолчанию: без него `docker compose` ругается на
    # незаданную переменную у всех, кто её не трогал.
    assert "${OPENCRM_WORKERS:-1}" in blok, (
        "нет значения по умолчанию: у тех, кто переменную не задавал, compose "
        "подставит пустую строку, и `${OPENCRM_WORKERS:-1}` в entrypoint её не "
        "спасёт — пустая строка это не «не задано»"
    )


def test_storozh_vorkerov_bez_redis_na_meste():
    """Несколько процессов без общего счётчика — молча ослабленная защита.

    Проверка стоит в двух местах сразу (`docker/entrypoint.sh` и
    `config/settings.py`), и это не дублирование: контейнер поднимается точкой
    входа, а на ноутбуке приложение запускают напрямую. Убери одну — половина
    случаев останется без предупреждения.
    """
    vhod = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    nastroyki = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")

    assert "OPENCRM_WORKERS" in vhod and "REDIS" in vhod, (
        "точка входа перестала проверять пару «много процессов + Redis»"
    )
    assert "workers" in nastroyki and "redis" in nastroyki.lower(), (
        "приложение перестало проверять пару «много процессов + Redis»"
    )


def test_u_prilozheniya_est_potolok_pamyati():
    """Без потолка нехватку памяти разбирает ядро, и разбирает молча.

    Оно выбирает жертву по наибольшему потреблению и уносит её без единой строки
    в логе — процесс просто исчезает. Соседние службы наблюдения потолки имеют
    давно, и довод там записан тот же: пусть упадёт наблюдатель, а не сайт. У
    самого сайта потолка не было вовсе.

    Число не «на глаз»: замерено в боевом образе — холостой процесс 227 МБ, два
    одновременных разжатия картинок дают пик 550 МБ. При четырёх рабочих
    процессах худший случай 2,2 ГБ, и три гигабайта покрывают его с запасом на
    машине с 7937 МБ.
    """
    stroki = (ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8").splitlines()

    vnutri = False
    blok: list[str] = []
    for stroka in stroki:
        if stroka.startswith("  app:"):
            vnutri = True
            continue
        if vnutri:
            golaya = stroka.strip()
            if golaya and not stroka.startswith("    ") and not stroka.startswith("#"):
                break
            blok.append(stroka)
    telo = "\n".join(blok)
    assert "env_file:" in telo, "вырезали не ту службу"

    predel = [s.strip() for s in blok if s.strip().startswith("mem_limit:")]
    assert predel, (
        "у приложения нет потолка памяти — при нехватке ядро унесёт самый "
        "крупный процесс молча, и это может оказаться база"
    )
    # Потолок обязан вмещать четыре рабочих процесса на пике: 4 × 550 МБ.
    znachenie = predel[0].split(":", 1)[1].strip().lower()
    assert znachenie.endswith("g"), f"потолок задан не в гигабайтах: {znachenie}"
    assert float(znachenie[:-1]) >= 3, (
        f"потолок {znachenie} мал: четыре воркера на пике дают 2,2 ГБ, и "
        "контейнер начнёт умирать на обычной работе"
    )
