from pathlib import Path

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
