from fastapi.testclient import TestClient

from tests.conftest import API, png_bytes
from web.main import app


def test_dashboard_aggregates(root_client, manager_client):
    # свой клиент, а не расчёт на созданных в других файлах: база одна на весь
    # прогон, и без него тест зеленел только когда его запускали вместе со всеми
    manager_client.post(f"{API}/clients", json={"name": "Дашборд-клиент"})
    board = manager_client.post(f"{API}/boards", json={"title": "Дашборд-доска"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    TestClient(app).get(f"/b/{share['token']}")  # один просмотр

    data = manager_client.get(f"{API}/dashboard").json()
    assert data["clients_total"] >= 1
    assert data["boards_published"] >= 1
    assert data["views_7d"] >= 1
    assert len(data["views_by_day"]) == 7
    assert data["views_by_day"][-1]["count"] >= 1  # сегодняшний просмотр
    assert data["last_view_at"] is not None
    assert len(data["recent_boards"]) >= 1
    first = data["recent_boards"][0]
    assert {"views_count", "has_active_link", "has_pin", "works_count"} <= set(first)


def test_boards_list_extended_fields(manager_client):
    client = manager_client.post(f"{API}/clients", json={"name": "Для доски"}).json()
    board = manager_client.post(
        f"{API}/boards", json={"title": "С клиентом", "client_id": client["id"]}
    ).json()
    manager_client.post(f"{API}/boards/{board['id']}/shares", json={"pin": "1234"})

    listing = manager_client.get(f"{API}/boards", params={"search": "С клиентом"}).json()
    item = listing["items"][0]
    assert item["client_name"] == "Для доски"
    assert item["has_links"] is True
    assert item["has_active_link"] is True
    assert item["has_pin"] is True
    assert item["views_count"] == 0


def test_spa_served_and_api_404_untouched(base_client):
    # SPA отдаётся на корне и на клиентских маршрутах
    for path in ("/", "/clients", "/boards/5"):
        response = base_client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]
    # несуществующий API-путь — честный 404, не index.html
    assert base_client.get("/api/v1/nonexistent").status_code == 404


def test_global_search(manager_client):
    client = manager_client.post(
        f"{API}/clients", json={"name": "Поисковый Клиент", "company": "Ромашка"}
    ).json()
    manager_client.post(f"{API}/boards", json={"title": "Поисковая доска", "client_id": client["id"]})

    # пустой запрос — недавние записи, палитра не открывается пустой
    recent = manager_client.get(f"{API}/search").json()
    assert recent["clients"]["items"] and recent["boards"]["items"]

    by_name = manager_client.get(f"{API}/search", params={"q": "Поисковый"}).json()
    assert any(c["name"] == "Поисковый Клиент" for c in by_name["clients"]["items"])

    by_company = manager_client.get(f"{API}/search", params={"q": "Ромашка"}).json()
    assert any(c["id"] == client["id"] for c in by_company["clients"]["items"])

    by_board = manager_client.get(f"{API}/search", params={"q": "Поисковая доска"}).json()
    board = by_board["boards"]["items"][0]
    assert board["title"] == "Поисковая доска"
    assert board["client_name"] == "Поисковый Клиент"  # палитра показывает клиента доски

    # регистр не важен, в том числе для кириллицы
    for query in ("поисковый", "ПОИСКОВЫЙ", "ромашка"):
        hit = manager_client.get(f"{API}/search", params={"q": query}).json()
        assert any(c["id"] == client["id"] for c in hit["clients"]["items"]), query
    lower_board = manager_client.get(f"{API}/search", params={"q": "поисковая"}).json()
    assert lower_board["boards"]["items"][0]["title"] == "Поисковая доска"

    empty = manager_client.get(f"{API}/search", params={"q": "щщщнеттакого"}).json()
    assert empty["clients"]["items"] == [] and empty["boards"]["items"] == []


def test_search_requires_auth(base_client):
    assert base_client.get(f"{API}/search").status_code == 401


def test_head_requests_supported(base_client):
    # мониторинг и прокси проверяют доступность через HEAD
    assert base_client.head("/healthz").status_code == 200
    assert base_client.head("/").status_code == 200
