"""Автоподбор логотипа сайта: выбор кандидата, защита от SSRF, ручной запасной путь."""

import io

import pytest

from core import exceptions as errors
from core.services import site_logo_service
from core.utils import normalize_external_url
from tests.conftest import API, png_bytes


# --- нормализация адреса ---

@pytest.mark.parametrize(
    "typed, expected",
    [
        ("studio.site", "https://studio.site"),
        ("www.studio.site/about", "https://www.studio.site/about"),
        ("https://studio.site", "https://studio.site"),
        ("http://studio.site", "http://studio.site"),
    ],
)
def test_bare_domain_gets_https(typed, expected):
    """Адрес без схемы — обычный способ набора; иначе сохранение молча падало бы."""
    assert normalize_external_url(typed) == expected


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,x", "//evil.example"])
def test_other_schemes_still_rejected(bad):
    with pytest.raises(ValueError):
        normalize_external_url(bad)


# --- выбор кандидата в разметке ---

def _parse(html: str):
    parser = site_logo_service._HeadParser()
    parser.feed(html)
    return [href for _rank, href in sorted(parser.icons, key=lambda i: i[0])], parser.og_image


def test_apple_touch_icon_wins_over_favicon():
    icons, _og = _parse(
        '<link rel="icon" sizes="16x16" href="/small.png">'
        '<link rel="apple-touch-icon" href="/big.png">'
    )
    assert icons[0] == "/big.png"


def test_large_icon_preferred_over_small():
    icons, _og = _parse(
        '<link rel="icon" sizes="16x16" href="/small.png">'
        '<link rel="icon" sizes="180x180" href="/large.png">'
    )
    assert icons[0] == "/large.png"


def test_og_image_used_as_fallback():
    icons, og = _parse('<meta property="og:image" content="https://cdn.example/og.png">')
    assert icons == []
    assert og == "https://cdn.example/og.png"


# --- SSRF ---

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",  # метаданные облака
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
    ],
)
def test_internal_addresses_are_refused(url):
    """Адрес вводит пользователь, запрос делает сервер — внутрь периметра нельзя."""
    with pytest.raises(errors.ValidationError) as exc:
        site_logo_service._assert_public_host(url)
    assert exc.value.code == "logo_fetch_failed"


def test_fetch_from_internal_address_fails_cleanly():
    with pytest.raises(errors.ValidationError) as exc:
        site_logo_service.fetch_logo("http://127.0.0.1:8080")
    assert exc.value.code == "logo_fetch_failed"


def test_fetch_without_address_fails_cleanly():
    with pytest.raises(errors.ValidationError) as exc:
        site_logo_service.fetch_logo("")
    assert exc.value.code == "logo_fetch_failed"


# --- приведение картинки ---

def test_gif_is_converted_to_png():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 100, 50)).save(buffer, "GIF")
    content, filename = site_logo_service._as_image(buffer.getvalue())
    assert filename == "site-logo.png"
    assert content.startswith(b"\x89PNG")


def test_ico_is_converted_to_png():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), (200, 100, 50, 255)).save(buffer, "ICO")
    content, filename = site_logo_service._as_image(buffer.getvalue())
    assert filename == "site-logo.png"
    assert content.startswith(b"\x89PNG")


def test_png_passes_through():
    content, filename = site_logo_service._as_image(png_bytes())
    assert filename == "site-logo.png"
    assert content.startswith(b"\x89PNG")


def test_non_image_rejected():
    with pytest.raises(Exception):
        site_logo_service._as_image(b"<html>not an image</html>")


# --- эндпоинт ---

def test_fetch_endpoint_requires_address(root_client):
    root_client.patch(f"{API}/settings", json={"values": {"studio_site_url": ""}})
    response = root_client.post(f"{API}/settings/site-logo/fetch")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "logo_fetch_failed"


def test_fetch_endpoint_is_root_only(manager_client):
    assert manager_client.post(f"{API}/settings/site-logo/fetch").status_code == 403
