from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

DEV_SECRET_KEY = "dev-secret-key-change-in-production"
DEV_IP_SALT = "dev-ip-salt"
#: Пароль root по умолчанию — такое же dev-значение, как два выше.
#:
#: Проверялся только на пустоту, а не на равенство умолчанию, как соседи: при
#: установке РУКАМИ (`docker compose up`, стенд, форк) root заводился с паролем из
#: исходников, а `must_change_password` ловит лишь того, кто вошёл ПЕРВЫМ.
DEV_ROOT_PASSWORD = "root-changeme"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENCRM_",
        env_file=str(BASE_DIR / "config" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"
    #: Запущено ли из образа (`ENV OPENCRM_DEPLOYED=1` в docker/Dockerfile).
    #:
    #: Снаружи не задаётся: флаг зашит в образ, и потерять его нельзя, не собрав
    #: другой образ.
    deployed: bool = False
    secret_key: str = DEV_SECRET_KEY
    #: Адрес базы. Умолчания нет и быть не может: база одна — MySQL, а адрес у неё
    #: с паролем, который пишет `./opencrm.sh` в config/.env. Придуманное умолчание
    #: увело бы приложение не туда, и на пустой базе это выглядит как потеря данных.
    db_url: str = ""
    #: Адрес Redis: общий счётчик попыток входа, PIN и потока заявок.
    #:
    #: Обязателен всюду, кроме одного сочетания — процесс один и окружение не
    #: боевое: там счётчик в памяти процесса И ЕСТЬ общий счётчик. Проверяет
    #: `config_errors()`, устройство — в `core/redis_client.py`.
    redis_url: str = ""
    #: Сколько рабочих процессов поднимает uvicorn (docker/entrypoint.sh).
    #:
    #: Читается и приложением: без общего счётчика значение больше единицы молча
    #: множит порог защиты от подбора на число процессов. Сказать об этом надо на
    #: старте, а не когда пароль подберут при работающем ограничителе.
    workers: int = 1
    #: Общий секрет у приёма тревог от наблюдателя.
    #:
    #: Пусто — приём закрыт только границей сети, как было. Это НЕ послабление
    #: ради удобства: обновление, потребовавшее секрета, оставило бы уже
    #: работающие установки без тревог до вмешательства человека — то есть
    #: молча выключило бы уведомления ровно у тех, кто их настроил.
    alerts_secret: str = ""
    storage_dir: Path = BASE_DIR / "storage"
    base_url: str = "http://localhost:8000"

    root_email: str = "root@opencrm.local"
    root_password: str = "root-changeme"

    max_upload_mb: int = 200
    # контроль свободного места на разделе со storage
    disk_warning_percent: float = 80.0
    disk_critical_percent: float = 90.0
    disk_min_free_mb: int = 1024  # ниже этого запаса загрузка файлов блокируется
    session_ttl_days: int = 14
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    pin_max_attempts: int = 5
    pin_lockout_minutes: int = 15

    # соль для хэширования IP в журнале просмотров (не для паролей)
    ip_hash_salt: str = DEV_IP_SALT

    # Сколько ДОВЕРЕННЫХ прокси перед приложением — тем и решается, какому адресу
    # в X-Forwarded-For верить (подбор PIN, хэш IP): 0 — никакому, берётся peer;
    # 1 — nginx, и берётся ПОСЛЕДНИЙ элемент, потому что первый пишет клиент.
    trusted_proxy_hops: int = 0

    @property
    def media_dir(self) -> Path:
        return self.storage_dir / "media"

    @property
    def client_files_dir(self) -> Path:
        return self.storage_dir / "client_files"

    @property
    def branding_dir(self) -> Path:
        return self.storage_dir / "branding"

    @property
    def avatars_dir(self) -> Path:
        return self.storage_dir / "avatars"

    @property
    def product_photos_dir(self) -> Path:
        """Снимки товаров склада. Отдельно от медиа досок и от файлов клиентов.

        Отдельным каталогом, а не подпапкой в `media`: у медиа досок своя обработка,
        свой набор производных и раздача через nginx напрямую, а снимок товара —
        только через приложение; в общей папке первая правка nginx открыла бы не то.
        """
        return self.storage_dir / "product_photos"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cookies_secure(self) -> bool:
        """Ставить ли cookie флаг Secure — по схеме `base_url`, а не по имени окружения.

        Браузер молча выбрасывает Secure-cookie, пришедшую по HTTP: пока флаг
        зависел от `env`, вход в локальной сети без домена (docs/08) давал 200,
        а человека сразу выкидывало обратно на форму.
        """
        return self.base_url.startswith("https://")

    def config_errors(self) -> list[str]:
        """Небезопасные значения, с которыми нельзя стартовать в production.

        Пустой secret_key подписывает cookie PIN-доступа пустым ключом —
        подделать её смог бы кто угодно, поэтому падаем, а не «работаем как есть».
        """
        # Импорт здесь, а не наверху, и это про слои, а не про скорость: `config`
        # лежит под `core`, и постоянная зависимость вверх развернула бы связи во
        # всём проекте ради одной проверки. Цикла нет, но и повода тянуть его нет.
        from core.security import passwords

        errors = []

        # Адрес базы — первым и не под отсечкой по окружению: это не безопасность,
        # а «не работает вовсе». И здесь, не только в `deploy/config.py`: изнутри
        # образа спрашивают ворота обновления — пропустят, и сайт сменится на 502.
        if not self.db_url.strip():
            errors.append(
                "OPENCRM_DB_URL пуст. Адрес базы пишет `./opencrm.sh install` в "
                "config/.env — вместе с паролем, который тот же установщик "
                "кладёт в docker/.env."
            )
        elif not self.db_url.startswith("mysql"):
            errors.append(
                f"OPENCRM_DB_URL={self.db_url!r} — база у продукта одна, MySQL. "
                "Вид значения: "
                "mysql+pymysql://opencrm:ПАРОЛЬ@db:3306/opencrm?charset=utf8mb4"
            )

        # Условий у «можно несколько процессов» два, и второе НЕ выводится из
        # первого («база держит писателей» — его даёт отсечка по MySQL выше):
        # счётчик в памяти молча множит порог на процессы, видно лишь по подбору.
        if self.workers > 1 and not self.redis_url.strip():
            errors.append(
                f"OPENCRM_WORKERS={self.workers} без OPENCRM_REDIS_URL: счётчик "
                "попыток входа лежал бы в памяти каждого процесса отдельно, и "
                f"порог защиты от подбора пароля и PIN стал бы в {self.workers} раз "
                "выше заявленного — без единой ошибки. Задайте OPENCRM_REDIS_URL "
                "или оставьте OPENCRM_WORKERS=1."
            )

        # Общий счётчик обязателен, даже пока процесс один: `OPENCRM_WORKERS`
        # правят снаружи, в docker/.env, и ровно тогда, когда выросла нагрузка, —
        # то есть в момент, когда про ограничитель точно не вспомнят.
        if self.is_production and not self.redis_url.strip():
            errors.append(
                "OPENCRM_REDIS_URL пуст: общего счётчика попыток нет, и защита от "
                "подбора пароля и PIN держится на памяти процесса. В развёрнутой "
                "системе это неприемлемо — служба redis поднимается вместе со "
                "стеком, адрес пишет ./opencrm.sh."
            )

        # **Конфиг не доехал.** `config/.env` подключается необязательным (иначе
        # первый запуск до установки не поднялся бы): пропади он — compose поднимет
        # контейнер молча, на умолчаниях, то есть с dev-ключом подписи cookie.
        if self.deployed and not self.is_production:
            errors.append(
                f"OPENCRM_ENV={self.env} внутри контейнера — почти наверняка не "
                "подхватился config/.env. Приложение взяло бы значения по "
                "умолчанию, включая dev-ключ подписи cookie. Проверьте, что файл "
                "на месте и подключён: docker compose config | grep -A3 env_file"
            )

        if not self.is_production:
            return errors
        if not self.secret_key.strip() or self.secret_key == DEV_SECRET_KEY:
            errors.append(
                "OPENCRM_SECRET_KEY пуст или равен dev-значению — подписи cookie "
                "(PIN-доступ к доскам) можно подделать."
            )
        if not self.ip_hash_salt.strip() or self.ip_hash_salt == DEV_IP_SALT:
            errors.append(
                "OPENCRM_IP_HASH_SALT пуст или равен dev-значению — IP посетителей "
                "витрин восстанавливаются из хэшей перебором."
            )
        if not self.root_password.strip():
            errors.append("OPENCRM_ROOT_PASSWORD пуст — root-аккаунт не будет создан.")
        elif self.root_password == DEV_ROOT_PASSWORD:
            errors.append(
                "OPENCRM_ROOT_PASSWORD равен dev-значению — root заводится с "
                "паролем из исходников. Сгенерировать: ./opencrm.sh или "
                "python -c \"import secrets; print(secrets.token_urlsafe(20))\""
            )
        elif not passwords.is_valid_password(self.root_password):
            # Не «слабый пароль» вообще, а единственный случай, когда приложение
            # не поднимется: root заводится ПРИ СТАРТЕ, минуя проверку регистрации,
            # и слишком длинный пароль уронил бы хэширование трассировкой bcrypt.
            errors.append(
                f"OPENCRM_ROOT_PASSWORD не проходит требования: от "
                f"{passwords.MIN_PASSWORD_LENGTH} знаков и не длиннее "
                f"{passwords.MAX_PASSWORD_BYTES} байт (кириллица весит два байта "
                f"на букву). Сейчас: {len(self.root_password)} знаков, "
                f"{len(self.root_password.encode())} байт."
            )
        return errors

    def config_warnings(self) -> list[str]:
        """Подозрительные, но не блокирующие значения."""
        warnings = []
        if "example.com" in self.base_url or "example." in self.base_url:
            warnings.append(
                f"OPENCRM_BASE_URL={self.base_url} — публичные ссылки на доски будут "
                "вести на этот домен. Укажите реальный адрес сайта."
            )
        if self.is_production and self.base_url.startswith("http://"):
            warnings.append(
                "OPENCRM_BASE_URL использует http:// при OPENCRM_ENV=production. "
                "Вход работать будет (cookie выдаются без флага Secure — см. "
                "cookies_secure), но пароли и cookie сессий идут по сети открытым "
                "текстом. Годится для локальной сети, для публичного сайта — нет."
            )
        if not self.is_production and self.secret_key == DEV_SECRET_KEY:
            warnings.append("OPENCRM_SECRET_KEY — dev-значение. Для боевого запуска задайте свой.")
        return warnings


def generate_secret_hint() -> str:
    return 'python -c "import secrets; print(secrets.token_urlsafe(48))"'


@lru_cache
def get_settings() -> Settings:
    return Settings()
