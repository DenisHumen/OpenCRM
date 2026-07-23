"""Приём и обработка медиафайлов работ: валидация по сигнатуре,
WebP-превью, blurhash, постеры видео (через ffmpeg, если он установлен)."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

import blurhash as blurhash_lib

from config.settings import get_settings
from core import exceptions as errors

# размеры производных изображений (по длинной стороне)
SIZE_LARGE = 1920
SIZE_CARD = 800
SIZE_THUMB = 320

# имена файлов, которые разрешено отдавать наружу
PUBLIC_FILENAMES = {
    "large.webp", "card.webp", "thumb.webp", "poster.webp",
    "image.svg", "video.mp4", "video.webm",
}


def detect_media(header: bytes, filename: str) -> tuple[str, str, str]:
    """Определяет тип по magic bytes → (kind, ext, mime). Расширению не доверяем."""
    if header.startswith(b"\xff\xd8\xff"):
        return "image", "jpg", "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png", "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image", "gif", "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image", "webp", "image/webp"
    if len(header) > 8 and header[4:8] == b"ftyp":
        return "video", "mp4", "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "webm", "video/webm"
    stripped = header.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if stripped.startswith((b"<?xml", b"<svg")):
        return "svg", "svg", "image/svg+xml"
    raise errors.ValidationError(
        "Unsupported file type. Allowed: JPG, PNG, GIF, WebP, SVG, MP4, WebM",
        code="unsupported_media_type",
    )


def work_dir(work_uid: str) -> Path:
    return get_settings().media_dir / work_uid


def save_original(work_uid: str, kind: str, ext: str, content: bytes) -> Path:
    """Кладёт исходник на диск. SVG предварительно санитайзится."""
    directory = work_dir(work_uid)
    directory.mkdir(parents=True, exist_ok=True)
    if kind == "svg":
        content = sanitize_svg(content)
        path = directory / "image.svg"
    elif kind == "video":
        path = directory / f"video.{ext}"
    else:
        path = directory / f"original.{ext}"
    path.write_bytes(content)
    return path


def delete_work_files(work_uid: str) -> None:
    directory = work_dir(work_uid)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


_SVG_SCRIPT_RE = re.compile(rb"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_SVG_EVENT_RE = re.compile(rb"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
_SVG_JSHREF_RE = re.compile(rb"(href|xlink:href)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


def sanitize_svg(content: bytes) -> bytes:
    content = _SVG_SCRIPT_RE.sub(b"", content)
    content = _SVG_EVENT_RE.sub(b"", content)
    content = _SVG_JSHREF_RE.sub(b"", content)
    return content


# --- обработка (фоновая задача после загрузки) ---

def process_image(work_uid: str, original: Path) -> dict:
    with Image.open(original) as im:
        im.load()
        width, height = im.size
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("P", "LA") else "RGB")
        directory = original.parent
        for name, size in (("large", SIZE_LARGE), ("card", SIZE_CARD), ("thumb", SIZE_THUMB)):
            variant = im.copy()
            variant.thumbnail((size, size), Image.LANCZOS)
            variant.save(directory / f"{name}.webp", "WEBP", quality=85)
        bh = compute_blurhash(im)
    return {"width": width, "height": height, "blurhash": bh}


def compute_blurhash(im: Image.Image) -> str | None:
    try:
        small = im.convert("RGB").copy()
        small.thumbnail((32, 32), Image.LANCZOS)
        w, h = small.size
        pixels = [[list(small.getpixel((x, y))) for x in range(w)] for y in range(h)]
        return blurhash_lib.encode(pixels, components_x=4, components_y=3)
    except Exception:
        return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def process_video(work_uid: str, video_path: Path) -> dict:
    """Постер и метаданные видео. Без ffmpeg — видео остаётся без постера."""
    meta: dict = {"width": None, "height": None, "duration_sec": None, "blurhash": None}
    if not ffmpeg_available():
        return meta
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "csv=p=0", str(video_path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        lines = [ln.strip() for ln in probe.stdout.splitlines() if ln.strip()]
        for ln in lines:
            parts = ln.split(",")
            if len(parts) >= 2 and parts[0].isdigit():
                meta["width"], meta["height"] = int(parts[0]), int(parts[1])
            elif parts[0].replace(".", "", 1).isdigit():
                meta["duration_sec"] = round(float(parts[0]), 2)

        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", str(video_path),
                 "-frames:v", "1", str(frame)],
                capture_output=True, timeout=120, check=True,
            )
            if not frame.exists():  # видео короче 1 секунды — берём первый кадр
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", str(frame)],
                    capture_output=True, timeout=120, check=True,
                )
            if frame.exists():
                directory = video_path.parent
                with Image.open(frame) as im:
                    im.load()
                    for name, size in (("poster", SIZE_LARGE), ("card", SIZE_CARD), ("thumb", SIZE_THUMB)):
                        variant = im.copy()
                        variant.thumbnail((size, size), Image.LANCZOS)
                        variant.save(directory / f"{name}.webp", "WEBP", quality=85)
                    meta["blurhash"] = compute_blurhash(im)
    except (subprocess.SubprocessError, OSError):
        pass  # постер — best effort; видео остаётся играбельным
    return meta


def media_url(work_uid: str, filename: str) -> str:
    return f"/media/{work_uid}/{filename}"


def work_media_urls(work) -> dict:
    """URL производных файлов работы для API и витрины."""
    uid = work.work_uid
    if work.kind == "image":
        if work.mime == "image/svg+xml":
            svg = media_url(uid, "image.svg")
            return {"thumb": svg, "card": svg, "large": svg}
        return {
            "thumb": media_url(uid, "thumb.webp"),
            "card": media_url(uid, "card.webp"),
            "large": media_url(uid, "large.webp"),
        }
    # video
    ext = "webm" if work.mime == "video/webm" else "mp4"
    directory = work_dir(uid)
    has_poster = (directory / "poster.webp").exists()
    return {
        "thumb": media_url(uid, "thumb.webp") if has_poster else None,
        "card": media_url(uid, "card.webp") if has_poster else None,
        "poster": media_url(uid, "poster.webp") if has_poster else None,
        "video": media_url(uid, f"video.{ext}"),
    }
