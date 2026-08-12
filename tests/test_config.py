"""Проверки конфигурации: небезопасные значения должны останавливать запуск,
а не тихо работать «как есть»."""

from config.settings import DEV_IP_SALT, DEV_SECRET_KEY, Settings

PROD = {
    "env": "production",
    "secret_key": "a-real-random-secret-value",
    "ip_hash_salt": "a-real-random-salt",
    "root_password": "root-password-1",
    "base_url": "https://studio.example.org",
    # Развёрнутая система обязана иметь общий счётчик попыток: без него защита
    # от подбора живёт в памяти процесса, и первый же OPENCRM_WORKERS>1 молча
    # умножает порог на число процессов. Поэтому адрес входит в набор «боевой
    # конфиг, к которому претензий нет».
    "redis_url": "redis://:pass@redis:6379/0",
}


def _settings(**overrides) -> Settings:
    # _env_file=None: тест не должен зависеть от локального config/.env
    return Settings(_env_file=None, **{**PROD, **overrides})


def test_production_rejects_empty_secret_key():
    errors = _settings(secret_key="").config_errors()
    assert any("SECRET_KEY" in message for message in errors)


def test_production_rejects_dev_secret_key():
    errors = _settings(secret_key=DEV_SECRET_KEY).config_errors()
    assert any("SECRET_KEY" in message for message in errors)


def test_production_rejects_empty_ip_salt():
    for value in ("", DEV_IP_SALT):
        errors = _settings(ip_hash_salt=value).config_errors()
        assert any("IP_HASH_SALT" in message for message in errors), value


def test_production_rejects_empty_root_password():
    errors = _settings(root_password="").config_errors()
    assert any("ROOT_PASSWORD" in message for message in errors)


def test_valid_production_config_has_no_errors():
    assert _settings(base_url="https://studio.site").config_errors() == []


def test_dev_config_never_blocks_startup():
    dev = Settings(_env_file=None, env="dev", secret_key="", ip_hash_salt="")
    assert dev.config_errors() == []


def test_placeholder_base_url_warns():
    warnings = _settings(base_url="https://studio.example.com").config_warnings()
    assert any("BASE_URL" in message for message in warnings)


def test_http_base_url_in_production_warns_about_plain_traffic():
    warnings = _settings(base_url="http://192.168.1.10:8000").config_warnings()
    assert any("открытым" in message for message in warnings)


# --- флаг Secure у cookie ---


def test_https_site_marks_cookies_secure():
    assert _settings(base_url="https://studio.site").cookies_secure is True


def test_plain_http_site_does_not_mark_cookies_secure():
    """Иначе браузер молча выбрасывает cookie и вход по локальной сети невозможен.

    Сценарий «сервер в локальной сети без домена» документирован в docs/08 и
    ставится скриптом установки — он обязан работать, а не выглядеть работающим.
    """
    assert _settings(base_url="http://192.168.1.10").cookies_secure is False


def test_the_flag_follows_the_address_not_the_environment_name():
    assert _settings(env="dev", base_url="https://studio.site").cookies_secure is True
    assert _settings(env="production", base_url="http://studio.site").cookies_secure is False
