"""Аватары пользователей: приём картинки, обрезка в квадрат, хранение webp.

Тип проверяем по сигнатуре (не по расширению), SVG не принимаем — только растр,
чтобы аватар нельзя было использовать как носитель скрипта.
"""

import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.services import media_service, storage_service
from database.models import User

AVATAR_SIZE = 256
MAX_AVATAR_BYTES = 5 * 1024 * 1024


def _avatar_file(filename: str) -> Path:
    return get_settings().avatars_dir / filename


def _delete_current(user: User) -> None:
    if not user.avatar_path:
        return
    name = user.avatar_path.rsplit("/", 1)[-1]
    if name and "/" not in name and "\\" not in name:
        _avatar_file(name).unlink(missing_ok=True)


def save_avatar(db: Session, user: User, content: bytes) -> str:
    if not content:
        raise errors.ValidationError("File is empty", code="file_empty")
    if len(content) > MAX_AVATAR_BYTES:
        raise errors.ValidationError("Image is too large (max 5 MB)", code="file_too_large")
    kind, _ext, mime = media_service.detect_media(content[:512], "avatar")
    if kind != "image" or mime == "image/svg+xml":
        raise errors.ValidationError(
            "Avatar must be a JPG, PNG, WebP or GIF image", code="bad_avatar_type"
        )
    if not storage_service.has_room_for(len(content)):
        raise errors.ValidationError("Not enough free disk space on the server", code="disk_full")

    directory = get_settings().avatars_dir
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.webp"
    try:
        with Image.open(BytesIO(content)) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("P", "LA") else "RGB")
            square = _center_square(im).resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
            square.save(directory / filename, "WEBP", quality=85)
    except Exception:
        (directory / filename).unlink(missing_ok=True)
        raise errors.ValidationError("Could not read image", code="bad_image")

    _delete_current(user)
    user.avatar_path = f"/avatars/{filename}"
    storage_service.invalidate_size_cache()
    return user.avatar_path


def clear_avatar(db: Session, user: User) -> None:
    _delete_current(user)
    user.avatar_path = ""
    storage_service.invalidate_size_cache()


def _center_square(im: Image.Image) -> Image.Image:
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return im.crop((left, top, left + side, top + side))
