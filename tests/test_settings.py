import re

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

    # Язык витрины — настройка на весь сайт, и база у тестов общая. Оставить её
    # переключённой значит передать соседям русскую витрину и падение на первой
    # же английской надписи.
    root_client.patch(f"{API}/settings", json={"values": {"showcase_locale": "en"}})


def test_return_button_label_is_optional_and_bounded(root_client, manager_client):
    """Подпись кнопки возврата: своя, по умолчанию английская, с потолком длины.

    Потолок не придирка: строка растягивает кнопку в шапке публичной витрины,
    и одна длинная надпись развалила бы вёрстку сразу у всех клиентов студии.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "С кнопкой"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    # адрес задан, подписи нет — значение по умолчанию
    root_client.patch(f"{API}/settings", json={"values": {"studio_site_url": "studio.example"}})
    assert "Return to the site" in TestClient(app).get(f"/b/{share['token']}").text

    # своя подпись вытесняет значение по умолчанию
    own = root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_label": "Вернуться на сайт"}}
    )
    assert own.status_code == 200
    page = TestClient(app).get(f"/b/{share['token']}").text
    assert "Вернуться на сайт" in page
    assert "Return to the site" not in page

    # слишком длинную не принимаем
    too_long = root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_label": "я" * 41}}
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "site_label_too_long"

    # ровно по границе — можно
    assert root_client.patch(
        f"{API}/settings", json={"values": {"studio_site_label": "я" * 40}}
    ).status_code == 200

    # Подпись кнопки — тоже настройка на весь сайт. Оставленная своя вытесняет
    # значение по умолчанию у соседей, и тест про кнопку падает, не найдя
    # английской надписи, — при этом сама кнопка работает.
    root_client.patch(f"{API}/settings", json={"values": {"studio_site_label": ""}})


def test_logo_upload(root_client):
    response = root_client.post(
        f"{API}/settings/logo",
        files={"file": ("logo.png", png_bytes(size=(64, 64)), "image/png")},
    )
    assert response.status_code == 201
    path = response.json()["brand_logo_path"]
    assert path.startswith("/branding/logo.png?v=")
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
    assert path.startswith("/branding/og-default.png?v=")
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


def _published_board(manager_client, title: str) -> str:
    board = manager_client.post(f"{API}/boards", json={"title": title}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    return manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()["token"]


def test_showcase_meta_and_footer_are_off_until_switched_on(root_client, manager_client):
    """Счётчик работ и футер с контактами по умолчанию скрыты."""
    assert root_client.get(f"{API}/settings").json()["showcase_show_meta"] == "0"
    assert root_client.get(f"{API}/settings").json()["showcase_show_footer"] == "0"
    root_client.patch(
        f"{API}/settings",
        json={"values": {"brand_name": "Студия", "contact_email": "hello@example.com"}},
    )
    token = _published_board(manager_client, "Тумблеры")

    off = TestClient(app).get(f"/b/{token}").text
    assert 'class="meta"' not in off
    assert "<footer>" not in off
    assert "hello@example.com" not in off

    root_client.patch(
        f"{API}/settings",
        json={"values": {"showcase_show_meta": "1", "showcase_show_footer": "1"}},
    )
    on = TestClient(app).get(f"/b/{token}").text
    assert 'class="meta"' in on
    assert "<footer>" in on
    assert "hello@example.com" in on

    root_client.patch(
        f"{API}/settings",
        json={"values": {"showcase_show_meta": "0", "showcase_show_footer": "0"}},
    )


def test_empty_brand_leaves_no_placeholder_letter(root_client, manager_client):
    """Пустой «Бренд» — пустое место, а не буква-заглушка в квадрате."""
    root_client.delete(f"{API}/settings/logo")
    root_client.patch(
        f"{API}/settings", json={"values": {"brand_name": "", "showcase_show_footer": "1"}}
    )
    token = _published_board(manager_client, "Без бренда")

    page = TestClient(app).get(f"/b/{token}").text
    assert 'class="brand-mark"' not in page
    assert 'class="curated"' not in page
    assert 'class="square"' not in page

    root_client.patch(
        f"{API}/settings", json={"values": {"brand_name": "Студия", "showcase_show_footer": "0"}}
    )
    back = TestClient(app).get(f"/b/{token}").text
    assert 'class="brand-mark"' in back
    # знак студии — первая буква названия, а не постоянная заглушка
    assert re.search(r'class="square">\s*С\s*</div>', back)


def test_showcase_font_is_cached_forever():
    """Шрифт лежит под постоянным именем и не меняется: без max-age браузер
    переспрашивал бы его на каждой загрузке страницы (вереница 304 в логе)."""
    response = TestClient(app).get("/static/fonts/montserrat-latin-wght-normal.woff2")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "max-age=31536000" in response.headers["cache-control"]
    # .woff2 есть в mimetypes не на всякой системе — иначе шрифт уезжает как octet-stream
    assert response.headers["content-type"] == "font/woff2"


def test_healthz():
    # `schema` появился, когда обновление на сервере стало опираться на этот
    # ответ: не дождавшись 200, обновлятор откатывает и код, и базу. Подробностей
    # здесь нет намеренно — адрес открыт наружу (см. tests/test_schema_check.py).
    assert TestClient(app).get("/healthz").json() == {"status": "ok", "schema": "ok"}
