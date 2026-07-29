"""Автоподбор логотипа сайта по его адресу.

Сервер сам открывает указанную страницу и ищет на ней иконку: apple-touch-icon,
<link rel="icon">, og:image, в последнюю очередь /favicon.ico. Если ничего не нашлось
или картинка не читается — менеджер загружает логотип руками.

Запрос идёт **по адресу, который ввёл пользователь**, поэтому это классический SSRF:
без проверок можно было бы заставить сервер сходить на http://169.254.169.254/ (метаданные
облака) или на внутренний адрес в локальной сети и вытащить ответ. Отсюда правила ниже:
резолвим имя сами и отклоняем непубличные адреса, ограничиваем время, размер и редиректы.
"""

import ipaddress
import socket
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

from core import exceptions as errors
from core.services import media_service
from core.utils import normalize_external_url

TIMEOUT_SECONDS = 6.0
MAX_PAGE_BYTES = 1_500_000
MAX_IMAGE_BYTES = 2_000_000
MAX_REDIRECTS = 3
USER_AGENT = "OpenCRM-logo-fetcher/1.0"

_ICON_RELS = {"apple-touch-icon", "apple-touch-icon-precomposed", "icon", "shortcut icon"}


class _HeadParser(HTMLParser):
    """Собирает кандидатов в логотип из <link> и <meta> в порядке приоритета."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.icons: list[tuple[int, str]] = []  # (приоритет, href)
        self.og_image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link":
            rels = {r.strip().lower() for r in data.get("rel", "").split()}
            href = data.get("href", "").strip()
            if not href:
                return
            rel_value = " ".join(sorted(rels))
            if rels & {"apple-touch-icon", "apple-touch-icon-precomposed"}:
                # apple-touch-icon почти всегда крупный и квадратный — лучший кандидат
                self.icons.append((0, href))
            elif "icon" in rels or rel_value == "shortcut icon":
                self.icons.append((1 if _size_of(data.get("sizes", "")) >= 64 else 2, href))
        elif tag == "meta":
            key = (data.get("property") or data.get("name") or "").lower()
            if key == "og:image" and not self.og_image:
                self.og_image = data.get("content", "").strip() or None


def _size_of(sizes: str) -> int:
    """«32x32 64x64» → 64. Нужен, чтобы предпочесть крупную иконку мелкой."""
    best = 0
    for chunk in sizes.lower().split():
        if "x" in chunk:
            try:
                best = max(best, int(chunk.split("x")[0]))
            except ValueError:
                continue
    return best


def _assert_public_host(url: str) -> None:
    """Отклоняет адреса, ведущие внутрь периметра (SSRF)."""
    host = urlsplit(url).hostname
    if not host:
        raise errors.ValidationError("Bad website address", code="logo_fetch_failed")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise errors.ValidationError("Website is unreachable", code="logo_fetch_failed") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise errors.ValidationError(
                "Website address is not public", code="logo_fetch_failed"
            )


def _get(client: httpx.Client, url: str, limit: int) -> bytes:
    """Скачивает не больше limit байт: сервер может отдавать бесконечный поток."""
    _assert_public_host(url)
    with client.stream("GET", url) as response:
        response.raise_for_status()
        # редирект мог увести на внутренний адрес — проверяем итоговый URL
        _assert_public_host(str(response.url))
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > limit:
                raise errors.ValidationError("File is too large", code="logo_fetch_failed")
        return body


def fetch_logo(site_url: str) -> tuple[bytes, str]:
    """Возвращает (содержимое, имя файла) логотипа сайта.

    Бросает ValidationError с кодом ``logo_fetch_failed`` — на него UI предлагает
    загрузить логотип вручную.
    """
    try:
        url = normalize_external_url(site_url)
    except ValueError as exc:
        raise errors.ValidationError(str(exc), code="logo_fetch_failed") from exc
    if not url:
        raise errors.ValidationError("Website address is empty", code="logo_fetch_failed")

    client = httpx.Client(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,image/*"},
    )
    try:
        try:
            page = _get(client, url, MAX_PAGE_BYTES)
        except errors.ValidationError:
            raise
        except Exception as exc:
            raise errors.ValidationError(
                "Could not open the website", code="logo_fetch_failed"
            ) from exc

        parser = _HeadParser()
        try:
            parser.feed(page.decode("utf-8", errors="replace"))
        except Exception:
            pass  # кривая разметка — остаётся запасной /favicon.ico

        candidates = [href for _rank, href in sorted(parser.icons, key=lambda i: i[0])]
        if parser.og_image:
            candidates.append(parser.og_image)
        root = urlsplit(url)
        candidates.append(f"{root.scheme}://{root.netloc}/favicon.ico")

        for href in candidates:
            absolute = urljoin(url, href)
            if not absolute.lower().startswith(("http://", "https://")):
                continue
            try:
                content = _get(client, absolute, MAX_IMAGE_BYTES)
                return _as_image(content)
            except Exception:
                continue  # следующий кандидат
    finally:
        client.close()

    raise errors.ValidationError("No logo found on the website", code="logo_fetch_failed")


def _as_image(content: bytes) -> tuple[bytes, str]:
    """Проверяет, что это картинка, и приводит .ico к png (у многих сайтов только favicon.ico)."""
    if not content:
        raise ValueError("empty")
    # detect_media на незнакомой сигнатуре бросает исключение — для .ico это норма,
    # его читает PIL ниже, поэтому отказ здесь не окончательный
    try:
        kind, ext, _mime = media_service.detect_media(content[:512], "logo")
    except Exception:
        kind, ext = "", ""
    if kind in ("image", "svg"):
        # брендинг принимает png/jpg/webp/svg — gif переводим, чтобы не отказывать зря
        if ext == "gif":
            with Image.open(BytesIO(content)) as im:
                buffer = BytesIO()
                im.convert("RGBA").save(buffer, "PNG")
                return buffer.getvalue(), "site-logo.png"
        return content, f"site-logo.{ext}"
    # ICO не проходит detect_media, но PIL его читает — берём самый крупный кадр
    with Image.open(BytesIO(content)) as im:
        if (im.format or "").lower() != "ico":
            raise ValueError("not an image")
        im = im.convert("RGBA")
        buffer = BytesIO()
        im.save(buffer, "PNG")
        return buffer.getvalue(), "site-logo.png"
