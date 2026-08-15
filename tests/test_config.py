"""Проверки конфигурации: небезопасные значения должны останавливать запуск,
а не тихо работать «как есть»."""

from config.settings import DEV_IP_SALT, DEV_ROOT_PASSWORD, DEV_SECRET_KEY, Settings

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
    # Адрес базы — часть «боевого конфига, к которому претензий нет»: без него
    # приложение не работает вовсе, и умолчания у него нет намеренно.
    "db_url": "mysql+pymysql://opencrm:pass@db:3306/opencrm?charset=utf8mb4",
}


def _settings(**overrides) -> Settings:
    # `_env_file=None` отменяет чтение файла, но НЕ окружения: pydantic читает
    # его всегда, а набор задаёт там и адрес базы, и адрес Redis. Поэтому всё,
    # что проверка хочет видеть пустым, обязано приезжать сюда явным доводом —
    # иначе она молча прочитает настоящее значение и пройдёт, ничего не
    # проверив. Ровно на этом один сторож здесь уже покраснел.
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
    """Разработка не спотыкается о требования, которые про безопасность.

    Адрес базы при этом обязателен и здесь: без него приложение не работает ни
    в каком окружении, и проверка его стоит ДО отсечки по окружению — она не
    про безопасность, а про невозможность. Поэтому адрес в наборе есть, а
    пустые ключ и соль — нет: именно их разработке прощают.
    """
    dev = Settings(
        _env_file=None, env="dev", secret_key="", ip_hash_salt="",
        db_url="mysql+pymysql://opencrm:pass@db:3306/opencrm?charset=utf8mb4",
    )
    assert dev.config_errors() == []


def test_v_razrabotke_pustoy_adres_bazy_tozhe_ne_proshchayetsya():
    """Парная к предыдущей: снисхождение к dev не должно расползаться.

    Пустой адрес — это не «работаем как есть», а «не работаем вовсе»:
    `create_engine("")` падает на импорте. Сказать об этом строкой лучше, чем
    выдать след стека из недр SQLAlchemy.

    `db_url=""` задаётся ЯВНО, и это не лишнее слово: `_env_file=None` отменяет
    чтение файла, но не окружения, а в окружении набора адрес базы стоит всегда
    (`tests/conftest.py`). Без явного значения проверка молча читала бы боевой
    адрес и проходила бы, ничего не проверив.
    """
    dev = Settings(_env_file=None, env="dev", secret_key="", ip_hash_salt="", db_url="")
    assert any("OPENCRM_DB_URL" in message for message in dev.config_errors())


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


# --- адрес базы --------------------------------------------------------------


def test_pustoy_adres_bazy_ostanavlivaet_start():
    """Ворота обновления спрашивают об этом ДО подмены живого контейнера.

    Проверка эта уже была написана однажды и **молча не применилась** —
    скриптовая правка не нашла якорь, а докстрока рядом и документация всё это
    время утверждали, что проверка есть. Снаружи это выглядело хуже, чем её
    отсутствие: `python -m config.selfcheck` отвечал «настройки сошлись» на
    пустом адресе, обновление подменяло контейнер, и приложение умирало уже
    после подмены — на импорте `database/session.py`, где `create_engine("")`.
    То есть 502 и откат вместо дешёвого отказа до начала работ.

    Отсюда и сторож: то, на что опираются ворота деплоя, обязано краснеть, а не
    держаться на памяти о том, что «это вроде добавляли».
    """
    errors = _settings(db_url="").config_errors()
    assert any("OPENCRM_DB_URL" in message for message in errors), errors


def test_chuzhoy_dvizhok_ostanavlivaet_start():
    """База у продукта одна. Файловый адрес — след прошлого, а не выбор.

    Молчать тут нельзя вдвойне: на чужом адресе движок остаётся без
    `READ COMMITTED` и без `SET time_zone='+00:00'` — обоих условий
    правильности, описанных в `database/session.py`. Приложение при этом
    поднимется и будет врать временами и отказами по уникальности.
    """
    errors = _settings(db_url="sqlite:///data/opencrm.db").config_errors()
    assert any("MySQL" in message for message in errors), errors


def test_boevoy_adres_bazy_prinimaetsya():
    """Парная проверка: иначе «отказывать всегда» тоже прошло бы обе прошлые."""
    assert _settings().config_errors() == []


def test_production_otvergaet_dev_parol_root():
    """Пароль из исходников в production — отказ, как у ключа и соли.

    Соседи проверялись на равенство своему умолчанию, а этот — только на
    пустоту, и разница была не косметической. Обычная установка не страдает:
    `./opencrm.sh` генерирует двадцать знаков. Страдает установка РУКАМИ —
    `docker compose up` из репозитория, стенд, форк, — и там root заводился с
    паролем, который написан в файле настроек.

    `must_change_password=True` смягчает, но не закрывает: сменить пароль
    предлагается вошедшему ПЕРВЫМ, а первым может оказаться не владелец.
    """
    errors = _settings(root_password=DEV_ROOT_PASSWORD).config_errors()
    assert any("ROOT_PASSWORD" in message for message in errors)


def test_production_otvergaet_parol_root_dlinnee_predela():
    """Слишком длинный пароль root — отказ с объяснением, а не падение старта.

    Случай особый: root заводится ПРИ СТАРТЕ, минуя проверку регистрации.
    Значит пароль длиннее предела bcrypt уронил бы не форму, а подъём
    приложения — трассировкой из чужой библиотеки посреди запуска.

    Порог считается в БАЙТАХ, поэтому опыт кириллицей: 42 знака и 84 байта —
    заведомо длинно, а по знакам ещё коротко.
    """
    errors = _settings(root_password="Пароль" * 7).config_errors()
    assert any("ROOT_PASSWORD" in message for message in errors)
    assert any("байт" in message for message in errors), (
        "отказ обязан назвать причину в байтах — иначе он читается как «пароль слаб»"
    )


def test_dlinnyy_no_dopustimyy_parol_root_prokhodit():
    """Ровно предел — это ещё «можно», и запретить его было бы новой поломкой."""
    rovno = "Пароль" * 6  # 36 знаков, 72 байта
    assert len(rovno.encode()) == 72
    assert _settings(root_password=rovno).config_errors() == []
