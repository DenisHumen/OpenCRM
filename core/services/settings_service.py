from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.utils import normalize_external_url, now_utc
from database.models import SiteSetting
from database.models.settings import SETTING_DEFAULTS

ALLOWED_LOGO_EXTS = {"png", "jpg", "jpeg", "webp", "svg"}


def get_all(db: Session) -> dict[str, str]:
    values = dict(SETTING_DEFAULTS)
    for row in db.scalars(select(SiteSetting)):
        if row.key in values:
            values[row.key] = row.value
    return values


def update(db: Session, changes: dict[str, str]) -> dict[str, str]:
    unknown = set(changes) - set(SETTING_DEFAULTS)
    if unknown:
        raise errors.ValidationError(f"Unknown settings: {sorted(unknown)}", code="unknown_setting")
    if "showcase_locale" in changes and changes["showcase_locale"] not in ("en", "ru"):
        raise errors.ValidationError("showcase_locale must be en or ru", code="bad_locale")
    if "studio_site_url" in changes:
        try:
            changes = {**changes, "studio_site_url": normalize_external_url(changes["studio_site_url"])}
        except ValueError as exc:
            raise errors.ValidationError(str(exc), code="bad_site_url") from exc
    existing = {row.key: row for row in db.scalars(select(SiteSetting))}
    for key, value in changes.items():
        value = (value or "").strip()
        if key in existing:
            existing[key].value = value
            existing[key].updated_at = now_utc()
        else:
            db.add(SiteSetting(key=key, value=value))
    db.flush()
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
    existing = {row.key for row in db.scalars(select(SiteSetting))}
    for key, value in SETTING_DEFAULTS.items():
        if key not in existing:
            db.add(SiteSetting(key=key, value=value))
    db.flush()
