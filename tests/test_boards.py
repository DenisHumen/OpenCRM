from config.settings import get_settings
from tests.conftest import API, png_bytes


def _board(client, title="Логотипы для кофеен"):
    response = client.post(f"{API}/boards", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def _upload_png(client, board_id, name="logo.png", color=(217, 119, 87)):
    response = client.post(
        f"{API}/boards/{board_id}/works",
        files={"file": (name, png_bytes(color=color), "image/png")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_upload_work_processing_pipeline(manager_client):
    board = _board(manager_client)
    work = _upload_png(manager_client, board["id"])
    assert work["status"] == "processing"

    # фоновая обработка в TestClient выполняется сразу после ответа
    ready = manager_client.get(f"{API}/boards/{board['id']}/works/{work['id']}").json()
    assert ready["status"] == "ready"
    assert ready["width"] == 640 and ready["height"] == 480
    assert ready["blurhash"]
    assert ready["media"]["card"].endswith("card.webp")

    # производные файлы на диске, оригинал не в публичном списке
    media_dir = get_settings().media_dir
    work_dir = next(media_dir.glob("*"))
    names = {p.name for p in work_dir.iterdir()}
    assert {"original.png", "large.webp", "card.webp", "thumb.webp"} <= names

    # медиа отдаётся с корректным MIME и вечным кэшем; оригинал снаружи недоступен
    media = manager_client.get(ready["media"]["card"])
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/webp"
    assert "immutable" in media.headers["cache-control"]
    assert manager_client.get(f"/media/{work_dir.name}/original.png").status_code == 404


def test_upload_names_the_work_after_the_file(manager_client):
    """Свежая работа не должна быть безымянной: подпись на витрине рисуется
    только при заполненном title, а имя файла почти всегда осмысленно."""
    board = _board(manager_client, "Имена работ")
    work = _upload_png(manager_client, board["id"], name="sigma_science.packaging.png")
    assert work["title"] == "sigma science packaging"  # без расширения и разделителей


def test_title_from_filename_rules():
    from core.services.board_service import title_from_filename

    assert title_from_filename("logo.png") == "logo"
    assert title_from_filename("Айдентика кофейни.jpg") == "Айдентика кофейни"
    assert title_from_filename("brand_guide_v2.final.pdf") == "brand guide v2 final"
    assert title_from_filename("  spaced  .webp") == "spaced"
    # имя без расширения не портим
    assert title_from_filename("README") == "README"
    assert len(title_from_filename("и" * 500 + ".png")) == 200


def test_freshly_uploaded_work_is_captioned_on_the_showcase(manager_client):
    board = _board(manager_client, "Подписи")
    _upload_png(manager_client, board["id"], name="Логотип кофейни.png")
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    from fastapi.testclient import TestClient

    from web.main import app

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert "Логотип кофейни" in page
    assert "Логотип кофейни.png" not in page  # расширение в подписи не нужно


def test_upload_rejects_unknown_type(manager_client):
    board = _board(manager_client, "Мусор")
    response = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("hack.exe", b"MZ\x90\x00 not a picture", "image/png")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_reorder_and_cover(manager_client):
    board = _board(manager_client, "Порядок")
    w1 = _upload_png(manager_client, board["id"], "a.png", (200, 60, 60))
    w2 = _upload_png(manager_client, board["id"], "b.png", (60, 200, 60))
    w3 = _upload_png(manager_client, board["id"], "c.png", (60, 60, 200))

    # порядок по умолчанию — порядок загрузки
    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    assert [w["id"] for w in detail["works"]] == [w1["id"], w2["id"], w3["id"]]

    reorder = manager_client.put(
        f"{API}/boards/{board['id']}/works/order",
        json={"work_ids": [w3["id"], w1["id"], w2["id"]]},
    )
    assert reorder.status_code == 200
    assert [w["id"] for w in reorder.json()["items"]] == [w3["id"], w1["id"], w2["id"]]

    # неполный список — ошибка
    bad = manager_client.put(
        f"{API}/boards/{board['id']}/works/order", json={"work_ids": [w1["id"]]}
    )
    assert bad.status_code == 422

    # обложка: только работа этой доски
    other_board = _board(manager_client, "Другая")
    foreign = _upload_png(manager_client, other_board["id"])
    assert (
        manager_client.patch(
            f"{API}/boards/{board['id']}", json={"cover_work_id": foreign["id"]}
        ).status_code
        == 422
    )
    ok = manager_client.patch(
        f"{API}/boards/{board['id']}", json={"cover_work_id": w2["id"]}
    )
    assert ok.status_code == 200
    assert ok.json()["cover_work_id"] == w2["id"]


def test_delete_work_removes_files_and_cover(manager_client):
    board = _board(manager_client, "Удаление работ")
    work = _upload_png(manager_client, board["id"])
    manager_client.patch(f"{API}/boards/{board['id']}", json={"cover_work_id": work["id"]})

    media_dirs_before = set(get_settings().media_dir.glob("*"))
    assert (
        manager_client.delete(f"{API}/boards/{board['id']}/works/{work['id']}").status_code
        == 200
    )
    media_dirs_after = set(get_settings().media_dir.glob("*"))
    assert len(media_dirs_before - media_dirs_after) == 1  # каталог работы удалён

    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    assert detail["cover_work_id"] is None
    assert detail["works"] == []


def test_board_soft_delete(manager_client):
    board = _board(manager_client, "Доска на удаление")
    assert manager_client.delete(f"{API}/boards/{board['id']}").status_code == 200
    assert manager_client.get(f"{API}/boards/{board['id']}").status_code == 404
    listing = manager_client.get(f"{API}/boards", params={"search": "Доска на удаление"}).json()
    assert listing["total"] == 0


def test_work_title_description_update(manager_client):
    board = _board(manager_client, "Подписи")
    work = _upload_png(manager_client, board["id"])
    updated = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}",
        json={"title": "Логотип, вариант 1", "description": "Тёплая палитра"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Логотип, вариант 1"


def test_the_board_list_asks_a_fixed_number_of_questions(manager_client):
    """Список досок не растёт запросами вместе с числом досок.

    Карточка доски собиралась поштучно: работы, обложка, просмотры, ссылки,
    клиент — пять запросов на доску, то есть двести пятьдесят на страницу из
    пятидесяти. И собиралась она так в трёх местах сразу — в списке, на
    дашборде и в палитре Ctrl+K, — слово в слово.

    Считаем сами запросы, а не время: время на стенде зависит от диска и
    соседей, число обращений — только от кода.
    """
    from sqlalchemy import event

    from database.session import engine

    def cost_of_listing(boards_count: int) -> int:
        client = manager_client.post(
            f"{API}/clients", json={"name": f"Заказчик досок {boards_count}"}
        ).json()
        for i in range(boards_count):
            created = _board(manager_client, title=f"Доска {boards_count}-{i}")
            manager_client.patch(
                f"{API}/boards/{created['id']}", json={"client_id": client["id"]}
            )
            _upload_png(manager_client, created["id"])

        queries = []
        listener = lambda conn, cursor, statement, *rest: queries.append(statement)
        event.listen(engine, "before_cursor_execute", listener)
        try:
            listed = manager_client.get(f"{API}/boards?per_page=200")
            assert listed.status_code == 200, listed.text
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(queries)

    few = cost_of_listing(2)
    many = cost_of_listing(10)

    assert many <= few, (
        f"восемь лишних досок стоили лишних запросов: было {few}, стало {many}"
    )


def test_the_three_places_show_the_same_board_card(manager_client):
    """Список, дашборд и палитра показывают одну и ту же карточку.

    Пока сборка была скопирована трижды, поле, добавленное в одном месте, в
    двух других не появлялось — и заметить это можно было только глазами.
    """
    client = manager_client.post(f"{API}/clients", json={"name": "Общий заказчик"}).json()
    board = _board(manager_client, title="Доска для трёх экранов")
    manager_client.patch(f"{API}/boards/{board['id']}", json={"client_id": client["id"]})
    _upload_png(manager_client, board["id"])

    def find(items):
        return next((b for b in items if b["id"] == board["id"]), None)

    in_list = find(manager_client.get(f"{API}/boards?per_page=200").json()["items"])
    in_palette = find(
        manager_client.get(f"{API}/search", params={"q": "Доска для трёх"}).json()["boards"]["items"]
    )
    in_dashboard = find(manager_client.get(f"{API}/dashboard").json()["recent_boards"])

    assert in_list and in_palette, "доска потерялась в списке или в палитре"
    assert set(in_list) == set(in_palette), "палитра отдаёт другой набор полей"
    assert in_list["works_count"] == in_palette["works_count"] == 1
    assert in_list["client_name"] == in_palette["client_name"] == "Общий заказчик"
    if in_dashboard is not None:   # дашборд показывает только четыре свежих
        assert set(in_dashboard) <= set(in_list)
        assert in_dashboard["works_count"] == 1
