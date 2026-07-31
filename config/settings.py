from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

DEV_SECRET_KEY = "dev-secret-key-change-in-production"
DEV_IP_SALT = "dev-ip-salt"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENCRM_",
        env_file=str(BASE_DIR / "config" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"
    secret_key: str = DEV_SECRET_KEY
    db_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'opencrm.db').as_posix()}"
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

    # Сколько ДОВЕРЕННЫХ обратных прокси стоит перед приложением. От этого зависит,
    # какому адресу в X-Forwarded-For можно верить при определении IP клиента
    # (rate-limit подбора PIN, хэш IP в журнале просмотров).
    #   0 — прокси нет (прямой запуск/dev): XFF игнорируется, берётся реальный peer.
    #       Безопасно по умолчанию: заголовок клиента подделать нельзя.
    #   1 — один nginx (боевая схема из docs/08): реальный адрес клиента nginx
    #       дописывает в XFF последним, поэтому берётся последний элемент, а не
    #       первый (первый полностью контролирует клиент и может его подделать).
    # За каждым лишним прокси в цепочке — увеличить на 1.
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
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cookies_secure(self) -> bool:
        """Ставить ли cookie флаг Secure — по схеме `base_url`, а не по имени окружения.

        Браузер молча выбрасывает Secure-cookie, пришедшую по обычному HTTP.
        Пока флаг зависел от `env == production`, документированный сценарий
        «локальная сеть без домена» (см. docs/08) ломался вчистую: вход возвращал
        200, cookie не приживалась, и человека сразу выкидывало обратно на форму.

        Схема в `base_url` — это и есть заявление владельца о том, как сайт
        отдаётся наружу, поэтому решение принимается по ней. За домен с
        сертификатом ничего не меняется: там `https://` и Secure на месте.
        """
        return self.base_url.startswith("https://")

    def config_errors(self) -> list[str]:
        """Небезопасные значения, с которыми нельзя стартовать в production.

        Пустой secret_key подписывает cookie PIN-доступа пустым ключом —
        подделать её смог бы кто угодно, поэтому падаем, а не «работаем как есть».
        """
        if not self.is_production:
            return []
        errors = []
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
