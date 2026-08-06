from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import uniqueness
from core.services import media_service
from core.utils import normalize_external_url, now_utc
from database.models import SiteSetting
from database.models.settings import SETTING_DEFAULTS
from database.repositories import settings as settings_repo

ALLOWED_LOGO_EXTS = {"png", "jpg", "jpeg", "webp", "svg"}

#: Настройки, значение которых становится ссылкой на публичной странице.
#:
#: Список закрытый и проверяется целиком: добавить сюда поле легко забыть, а
#: цена забывчивости — работающий `javascript:` в href у клиентов студии.
URL_SETTINGS = (
    "studio_site_url",
    "social_telegram",
    "social_instagram",
    "social_website",
)


def get_all(db: Session) -> dict[str, str]:
    values = dict(SETTING_DEFAULTS)
    for row in settings_repo.all_rows(db):
        if row.key in values:
            values[row.key] = row.value
    return values


def update(db: Session, changes: dict[str, str]) -> dict[str, str]:
    unknown = set(changes) - set(SETTING_DEFAULTS)
    if unknown:
        raise errors.ValidationError(f"Unknown settings: {sorted(unknown)}", code="unknown_setting")
    if "showcase_locale" in changes and changes["showcase_locale"] not in ("en", "ru"):
        raise errors.ValidationError("showcase_locale must be en or ru", code="bad_locale")
    # Подпись уходит в шапку публичной витрины и растягивает кнопку по себе:
    # без потолка одна длинная строка разносит вёрстку у всех клиентов сразу.
    # 40 символов — вдвое длиннее значения по умолчанию, с запасом на «Вернуться
    # на сайт студии» и подобное.
    if len((changes.get("studio_site_label") or "").strip()) > 40:
        raise errors.ValidationError(
            "Button text is too long (max 40 characters)", code="site_label_too_long"
        )
    # Все ссылки, уходящие в href публичных страниц, проходят одну проверку.
    #
    # Раньше её проходил только адрес сайта студии, а соцсети и «сайт» из
    # контактов — нет. `javascript:alert(1)` в поле «Телеграм» доезжал до
    # атрибута href витрины и страницы закрытой ссылки: экранирование Jinja2
    # схему не трогает, а CSP витрины разрешает inline-скрипты. Получался
    # хранимый XSS по клиентам студии, на том же домене, что и CRM, — от любой
    # учётки с правом на настройки.
    for key in URL_SETTINGS:
        if key not in changes:
            continue
        try:
            changes = {**changes, key: normalize_external_url(changes[key])}
        except ValueError as exc:
            raise errors.ValidationError(str(exc), code="bad_site_url") from exc
    settings_repo.write_many(db, {key: (value or "").strip() for key, value in changes.items()})
    return get_all(db)


def _save_branding_image(
    db: Session, original_name: str, content: bytes, base_name: str, setting_key: str
) -> str:
    ext = Path(original_name).suffix.lstrip(".").lower()
    if ext not in ALLOWED_LOGO_EXTS:
        raise errors.ValidationError("Image must be PNG, JPG, WebP or SVG", code="bad_logo_type")
    if len(content) > 5 * 1024 * 1024:
        raise errors.ValidationError("Image is too large (max 5 MB)", code="file_too_large")
    directory = get_settings().branding_dir
    directory.mkdir(parents=True, exist_ok=True)
    # убираем старые версии с другим расширением, чтобы не отдать устаревший файл
    for old in directory.glob(f"{base_name}.*"):
        old.unlink(missing_ok=True)
    if ext == "svg":
        # Логотип чистится тем же способом, что и работы на досках. Раньше не
        # чистился вовсе: `<script>` в загруженном логотипе доезжал до
        # `/branding/logo.svg` дословно, а этот адрес открывают напрямую.
        content = media_service.sanitize_svg(content)
    filename = f"{base_name}.{ext}"
    (directory / filename).write_bytes(content)
    # имя файла постоянное, а отдаётся он с длинным кэшем — без метки версии
    # заменённый логотип ещё час показывался бы старым и клиенту, и менеджеру
    path = f"/branding/{filename}?v={int(now_utc().timestamp())}"
    update(db, {setting_key: path})
    return path


def _clear_branding_image(db: Session, base_name: str, setting_key: str) -> None:
    """Удаляет файл(ы) картинки с диска и сбрасывает настройку в дефолт (пустую строку)."""
    directory = get_settings().branding_dir
    if directory.exists():
        for old in directory.glob(f"{base_name}.*"):
            old.unlink(missing_ok=True)
    update(db, {setting_key: SETTING_DEFAULTS[setting_key]})


def save_logo(db: Session, original_name: str, content: bytes) -> str:
    return _save_branding_image(db, original_name, content, "logo", "brand_logo_path")


def clear_logo(db: Session) -> None:
    _clear_branding_image(db, "logo", "brand_logo_path")


def save_site_logo(db: Session, original_name: str, content: bytes) -> str:
    """Лого сайта студии для кнопки «Return to the site» на витрине."""
    return _save_branding_image(db, original_name, content, "site-logo", "studio_site_logo")


def clear_site_logo(db: Session) -> None:
    _clear_branding_image(db, "site-logo", "studio_site_logo")


def save_og_default(db: Session, original_name: str, content: bytes) -> str:
    """Дефолтная OG-картинка: превью ссылки для PIN-досок (рекомендация 1200×630)."""
    return _save_branding_image(db, original_name, content, "og-default", "og_default_image")


def clear_og_default(db: Session) -> None:
    _clear_branding_image(db, "og-default", "og_default_image")


def seed_defaults(db: Session) -> None:
    """Умолчания настроек при первом запуске.

    Терпимо к тому, что соседний процесс сеет то же самое: `uvicorn --workers 2`
    поднимает оба разом, и оба видят «ключа нет». Без этого проигравший падал
    прямо на старте, и приложение не поднималось.
    """
    existing = settings_repo.existing_keys(db)
    for key, value in SETTING_DEFAULTS.items():
        if key in existing:
            continue
        uniqueness.insert_or_ignore(
            db,
            SiteSetting(key=key, value=value),
            taken=lambda row: settings_repo.key_exists(db, row.key),
        )
