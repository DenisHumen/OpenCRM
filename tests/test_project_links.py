"""Ссылка на проект у работы и настройка «сайт студии».

Обе используют один валидатор: на витрину ссылка уходит в href, поэтому схема
проверяется явно — иначе javascript: в атрибуте дал бы XSS.
"""

import pytest
from fastapi.testclient import TestClient

from core.utils import MAX_URL_LENGTH, normalize_external_url
from tests.conftest import API, png_bytes
from web.main import app


def _board_with_work(client, title="Кейсы"):
    board = client.post(f"{API}/boards", json={"title": title}).json()
    upload = client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("work.png", png_bytes(), "image/png")},
    )
    assert upload.status_code == 202, upload.text
    work_id = upload.json()["id"]
    return board, work_id


# --- валидатор ---

@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_url_is_allowed(value):
    assert normalize_external_url(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "https://client.example/case",
        "http://client.example",
        "  https://client.example/case?a=1#x  ",
    ],
)
def test_http_urls_pass(value):
    assert normalize_external_url(value) == value.strip()


def test_bare_domain_is_completed_not_rejected():
    """Менеджер вправе набрать «client.example/case» — дописываем схему сами."""
    assert normalize_external_url("client.example/case") == "https://client.example/case"


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "//evil.example/case",        # protocol-relative уводит на чужой домен
        "ftp://client.example",
        "https://client.example/\nx",  # перевод строки ломает атрибут
    ],
)
def test_dangerous_urls_rejected(value):
    with pytest.raises(ValueError):
        normalize_external_url(value)


def test_too_long_url_rejected():
    with pytest.raises(ValueError):
        normalize_external_url("https://client.example/" + "a" * MAX_URL_LENGTH)


# --- работа ---

def test_work_project_url_saved_and_cleared(manager_client):
    _board, work_id = _board_with_work(manager_client)
    board_id = manager_client.get(f"{API}/boards").json()["items"][0]["id"]

    saved = manager_client.patch(
        f"{API}/boards/{board_id}/works/{work_id}",
        json={"project_url": "https://client.example/kara"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["project_url"] == "https://client.example/kara"

    cleared = manager_client.patch(
        f"{API}/boards/{board_id}/works/{work_id}", json={"project_url": ""}
    )
    assert cleared.status_code == 200
    assert cleared.json()["project_url"] == ""


def test_work_rejects_javascript_url(manager_client):
    board, work_id = _board_with_work(manager_client, title="Кейсы XSS")
    bad = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work_id}",
        json={"project_url": "javascript:alert(1)"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "bad_project_url"


def test_work_without_url_defaults_to_empty(manager_client):
    board, work_id = _board_with_work(manager_client, title="Кейсы пустые")
    work = manager_client.get(f"{API}/boards/{board['id']}/works/{work_id}").json()
    assert work["project_url"] == ""


# --- настройка сайта студии ---

def test_studio_site_url_setting(root_client):
    saved = root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_url": "https://studio.example"}}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["studio_site_url"] == "https://studio.example"

    bad = root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_url": "javascript:alert(1)"}}
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "bad_site_url"

    # значение из неудачной попытки не сохранилось
    assert root_client.get(f"{API}/settings").json()["studio_site_url"] == "https://studio.example"


def test_site_logo_upload_serve_and_clear(root_client):
    root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_url": "https://studio.example"}}
    )
    uploaded = root_client.post(
        f"{API}/settings/site-logo",
        files={"file": ("site-logo.png", png_bytes(size=(200, 60)), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    path = uploaded.json()["studio_site_logo"]
    assert path.startswith("/branding/site-logo.png?v=")  # метка версии против кэша

    # публичная отдача: без этого лого в кнопке было бы битой картинкой
    served = TestClient(app).get(path)
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/")

    # лого попадает в кнопку на витрине
    assert root_client.get(f"{API}/settings").json()["studio_site_logo"] == path

    removed = root_client.delete(f"{API}/settings/site-logo")
    assert removed.status_code == 200
    assert root_client.get(f"{API}/settings").json()["studio_site_logo"] == ""
    assert TestClient(app).get(path).status_code == 404


def test_site_logo_does_not_clash_with_brand_logo(root_client):
    """Оба лежат в /branding — удаление одного не должно стирать другое."""
    root_client.post(
        f"{API}/settings/logo", files={"file": ("logo.png", png_bytes(), "image/png")}
    )
    root_client.post(
        f"{API}/settings/site-logo", files={"file": ("site-logo.png", png_bytes(), "image/png")}
    )
    root_client.delete(f"{API}/settings/site-logo")

    values = root_client.get(f"{API}/settings").json()
    assert values["brand_logo_path"].startswith("/branding/logo.png")
    assert values["studio_site_logo"] == ""
    assert TestClient(app).get("/branding/logo.png").status_code == 200


# --- витрина ---

def test_showcase_renders_case_button_only_with_url(manager_client):
    board, work_id = _board_with_work(manager_client, title="Витрина кейсов")
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    without = TestClient(app).get(f"/b/{share['token']}")
    assert "client.example" not in without.text

    manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work_id}",
        json={"project_url": "https://client.example/kara", "title": "Kara Collection"},
    )
    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert 'href="https://client.example/kara"' in page.text
    assert 'rel="noopener noreferrer"' in page.text
    assert "Kara Collection" in page.text


def test_showcase_return_button_with_and_without_logo(manager_client, root_client):
    board, _work_id = _board_with_work(manager_client, title="Витрина шапки")
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    root_client.patch(f"{API}/settings", json={"values": {"studio_site_url": ""}})
    # надпись есть только в самой кнопке (класс .btn-site встречается ещё и в CSS)
    assert "Return to the site" not in TestClient(app).get(f"/b/{share['token']}").text

    root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_url": "https://studio.example"}}
    )
    root_client.delete(f"{API}/settings/site-logo")
    plain = TestClient(app).get(f"/b/{share['token']}").text
    assert "Return to the site" in plain
    # без лого остаётся только надпись, плашки нет
    assert '<span class="site-logo">' not in plain
    assert "site-logo.png" not in plain
    # надпись всё равно в своём span: она лежит над волной заливки (z-index)
    assert '<span class="site-text">' in plain

    root_client.post(
        f"{API}/settings/site-logo",
        files={"file": ("site-logo.png", png_bytes(), "image/png")},
    )
    withlogo = TestClient(app).get(f"/b/{share['token']}").text
    assert '<span class="site-logo">' in withlogo
    assert '<img src="/branding/site-logo.png?v=' in withlogo
