from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.conftest import API, png_bytes
from web.main import app


def _published_board(client, title="Витрина"):
    board = client.post(f"{API}/boards", json={"title": title, "description": "Подборка работ"}).json()
    upload = client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("work.png", png_bytes(), "image/png")},
    )
    assert upload.status_code == 202
    client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    return board


def _share(client, board_id, **kwargs):
    response = client.post(f"{API}/boards/{board_id}/shares", json=kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def test_public_showcase_and_view_counter(manager_client):
    board = _published_board(manager_client)
    share = _share(manager_client, board["id"])

    visitor = TestClient(app)
    page = visitor.get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "Витрина" in page.text
    assert "og:image" in page.text
    assert "card.webp" in page.text

    # повторный визит
    visitor.get(f"/b/{share['token']}")

    views = manager_client.get(f"{API}/shares/{share['id']}/views").json()
    assert views["views_count"] == 2
    assert views["last_viewed_at"] is not None

    # data-эндпоинт
    data = visitor.get(f"/b/{share['token']}/data").json()
    assert data["board"]["title"] == "Витрина"
    assert len(data["works"]) == 1
    assert data["works"][0]["media"]["large"].endswith("large.webp")


def test_unpublished_board_is_closed(manager_client):
    board = _published_board(manager_client, "Черновик")
    share = _share(manager_client, board["id"])
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": False})
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 404


def test_revoke_and_reactivate(manager_client):
    board = _published_board(manager_client, "Отзыв")
    share = _share(manager_client, board["id"])
    visitor = TestClient(app)
    assert visitor.get(f"/b/{share['token']}").status_code == 200

    revoked = manager_client.patch(f"{API}/shares/{share['id']}", json={"is_active": False})
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert visitor.get(f"/b/{share['token']}").status_code == 404

    manager_client.patch(f"{API}/shares/{share['id']}", json={"is_active": True})
    assert visitor.get(f"/b/{share['token']}").status_code == 200


def test_expired_link(manager_client):
    board = _published_board(manager_client, "Истёкшая")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    share = _share(manager_client, board["id"], expires_at=past)
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 404

    # продлеваем — открывается
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    manager_client.patch(f"{API}/shares/{share['id']}", json={"expires_at": future})
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 200


def test_pin_flow(manager_client):
    board = _published_board(manager_client, "Защищённая")
    share = _share(manager_client, board["id"], pin="4821")
    assert share["has_pin"] is True

    visitor = TestClient(app)
    page = visitor.get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "pin" in page.text.lower()
    assert "Защищённая" not in page.text  # PIN-страница не раскрывает доску
    assert "card.webp" not in page.text  # и обложку — даже в OG (допустима только брендовая заглушка)

    wrong = visitor.post(f"/b/{share['token']}/pin", data={"pin": "0000"})
    assert wrong.status_code == 401

    right = visitor.post(f"/b/{share['token']}/pin", data={"pin": "4821"}, follow_redirects=False)
    assert right.status_code == 303

    opened = visitor.get(f"/b/{share['token']}")
    assert opened.status_code == 200
    assert "Защищённая" in opened.text

    # другой посетитель без PIN-cookie всё ещё видит форму
    stranger = TestClient(app)
    assert "Защищённая" not in stranger.get(f"/b/{share['token']}").text


def test_pin_longer_than_four_digits(manager_client):
    # PIN разрешён 4–8 цифр; экран ввода не должен отправлять код на 4-й цифре
    board = _published_board(manager_client, "Длинный PIN")
    share = _share(manager_client, board["id"], pin="123456")

    visitor = TestClient(app)
    assert "Длинный PIN" not in visitor.get(f"/b/{share['token']}").text

    # префикс верного кода доступа не даёт
    assert visitor.post(f"/b/{share['token']}/pin", data={"pin": "1234"}).status_code == 401

    ok = visitor.post(f"/b/{share['token']}/pin", data={"pin": "123456"}, follow_redirects=False)
    assert ok.status_code == 303
    assert "Длинный PIN" in visitor.get(f"/b/{share['token']}").text


def test_pin_rate_limit(manager_client):
    board = _published_board(manager_client, "PIN лимит")
    share = _share(manager_client, board["id"], pin="7777")
    visitor = TestClient(app)
    for _ in range(5):
        assert visitor.post(f"/b/{share['token']}/pin", data={"pin": "1111"}).status_code == 401
    limited = visitor.post(f"/b/{share['token']}/pin", data={"pin": "7777"})
    assert limited.status_code == 429


def test_bad_pin_validation(manager_client):
    board = _published_board(manager_client, "PIN валидация")
    response = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"pin": "12ab"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_pin"


def test_regenerate_kills_old_token(manager_client):
    board = _published_board(manager_client, "Перегенерация")
    share = _share(manager_client, board["id"])
    visitor = TestClient(app)
    assert visitor.get(f"/b/{share['token']}").status_code == 200

    regen = manager_client.post(f"{API}/shares/{share['id']}/regenerate")
    assert regen.status_code == 200
    new = regen.json()
    assert new["token"] != share["token"]

    assert visitor.get(f"/b/{share['token']}").status_code == 404
    assert visitor.get(f"/b/{new['token']}").status_code == 200


def test_remove_pin(manager_client):
    board = _published_board(manager_client, "Снятие PIN")
    share = _share(manager_client, board["id"], pin="5555")
    visitor = TestClient(app)
    assert "Снятие PIN" not in visitor.get(f"/b/{share['token']}").text

    manager_client.patch(f"{API}/shares/{share['id']}", json={"pin": None})
    assert "Снятие PIN" in visitor.get(f"/b/{share['token']}").text


def test_multiple_links_per_board(manager_client):
    board = _published_board(manager_client, "Несколько ссылок")
    a = _share(manager_client, board["id"])
    b = _share(manager_client, board["id"], pin="9999")
    listing = manager_client.get(f"{API}/boards/{board['id']}/shares").json()["items"]
    assert {link["id"] for link in listing} >= {a["id"], b["id"]}
    # отзыв одной не трогает другую
    manager_client.patch(f"{API}/shares/{a['id']}", json={"is_active": False})
    visitor = TestClient(app)
    assert visitor.get(f"/b/{a['token']}").status_code == 404
    assert visitor.get(f"/b/{b['token']}").status_code == 200
