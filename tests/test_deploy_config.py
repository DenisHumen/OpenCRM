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
MAINTENANCE = ROOT / "docker" / "nginx" / "maintenance" / "maintenance.html"
DOCKERIGNORE = ROOT / ".dockerignore"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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
