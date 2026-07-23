from fastapi.testclient import TestClient

from tests.conftest import API, png_bytes
from web.main import app


def test_settings_root_only(root_client, manager_client):
    assert manager_client.get(f"{API}/settings").status_code == 403
    settings = root_client.get(f"{API}/settings").json()
    assert settings["brand_name"] == "Studio"
    assert settings["showcase_locale"] == "en"


def test_update_settings_and_showcase_branding(root_client, manager_client):
    updated = root_client.patch(
        f"{API}/settings",
        json={
            "values": {
                "brand_name": "Пример Студия",
                "contact_email": "hello@example.com",
                "showcase_locale": "ru",
            }
        },
    )
    assert updated.status_code == 200
    assert updated.json()["brand_name"] == "Пример Студия"

    # витрина подхватывает бренд и язык
    board = manager_client.post(f"{API}/boards", json={"title": "Брендовая"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    page = TestClient(app).get(f"/b/{share['token']}")
    assert "Пример Студия" in page.text
    assert "Работы скоро появятся" in page.text  # ru-локаль и пустая доска

    # неизвестный ключ отклоняется
    bad = root_client.patch(f"{API}/settings", json={"values": {"hacker_key": "x"}})
    assert bad.status_code == 422


def test_logo_upload(root_client):
    response = root_client.post(
        f"{API}/settings/logo",
        files={"file": ("logo.png", png_bytes(size=(64, 64)), "image/png")},
    )
    assert response.status_code == 201
    path = response.json()["brand_logo_path"]
    assert path == "/branding/logo.png"
    assert TestClient(app).get(path).status_code == 200


def test_logo_delete_resets_to_default(root_client, manager_client):
    root_client.post(
        f"{API}/settings/logo",
        files={"file": ("logo.png", png_bytes(size=(64, 64)), "image/png")},
    )
    assert manager_client.delete(f"{API}/settings/logo").status_code == 403

    response = root_client.delete(f"{API}/settings/logo")
    assert response.status_code == 200
    assert response.json()["brand_logo_path"] == ""
    assert root_client.get(f"{API}/settings").json()["brand_logo_path"] == ""
    # файл удалён с диска
    assert TestClient(app).get("/branding/logo.png").status_code == 404


def test_og_default_image_upload_and_pin_page(root_client, manager_client):
    response = root_client.post(
        f"{API}/settings/og-image",
        files={"file": ("og.png", png_bytes(size=(1200, 630)), "image/png")},
    )
    assert response.status_code == 201
    path = response.json()["og_default_image"]
    assert path == "/branding/og-default.png"
    assert TestClient(app).get(path).status_code == 200

    # PIN-доска использует дефолтную OG-картинку, а не обложку
    board = manager_client.post(f"{API}/boards", json={"title": "OG-доска"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={"pin": "1234"}).json()
    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "og-default.png" in page.text
    assert "card.webp" not in page.text  # обложка не светится


def test_og_default_image_delete_resets_to_default(root_client, manager_client):
    root_client.post(
        f"{API}/settings/og-image",
        files={"file": ("og.png", png_bytes(size=(1200, 630)), "image/png")},
    )
    assert manager_client.delete(f"{API}/settings/og-image").status_code == 403

    response = root_client.delete(f"{API}/settings/og-image")
    assert response.status_code == 200
    assert response.json()["og_default_image"] == ""
    assert root_client.get(f"{API}/settings").json()["og_default_image"] == ""
    assert TestClient(app).get("/branding/og-default.png").status_code == 404

    # PIN-доска после сброса больше не отдаёт og:image
    board = manager_client.post(f"{API}/boards", json={"title": "Без OG"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={"pin": "1234"}).json()
    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "og-default.png" not in page.text


def test_healthz():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}
