from fastapi.testclient import TestClient

from core.services import media_service
from tests.conftest import API, png_bytes
from web.main import app


def _board_with_work(client, title="Файлы"):
    """Доска с одной обработанной работой. Возвращает (board, work-with-media)."""
    board = client.post(f"{API}/boards", json={"title": title}).json()
    up = client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("work.png", png_bytes(), "image/png")},
    )
    assert up.status_code == 202, up.text
    # ответ на загрузку ещё «processing»; перечитываем — фоновая обработка уже прошла
    work = client.get(f"{API}/boards/{board['id']}/works/{up.json()['id']}").json()
    assert work["status"] == "ready", work
    return board, work


def _uid_of(work) -> str:
    # media.large = /media/<uid>/large.webp
    return work["media"]["large"].split("/")[2]


# --- задача 2: удаление доски уносит файлы работ ---

def test_delete_board_removes_work_files(manager_client):
    board, work = _board_with_work(manager_client, "Удаляемая доска")
    work_dir = media_service.work_dir(_uid_of(work))
    assert work_dir.exists()

    # ссылка + просмотр: проверим, что каскад сносит share_links/share_views
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    TestClient(app).get(f"/b/{share['token']}")

    assert manager_client.delete(f"{API}/boards/{board['id']}").status_code == 200

    assert not work_dir.exists()                                    # файлы удалены с диска
    assert manager_client.get(f"{API}/boards/{board['id']}").status_code == 404  # доска исчезла
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 404         # ссылка каскадом


# --- задача 1: менеджер файлов (root) ---

def test_file_manager_lists_media_with_metadata(root_client, manager_client):
    board, work = _board_with_work(manager_client, "Доска для менеджера")

    # только root
    assert manager_client.get(f"{API}/system/files").status_code == 403

    listing = root_client.get(f"{API}/system/files").json()
    assert "storage" in listing
    entry = next(f for f in listing["items"] if f["id"] == work["id"])
    assert entry["board_id"] == board["id"]
    assert entry["board_title"] == "Доска для менеджера"
    assert entry["size_bytes"] > 0            # реальный размер на диске (оригинал + превью)
    assert entry["created_at"] is not None
    assert entry["last_viewed_at"] is None    # ещё не смотрели
    assert entry["thumb"].endswith("thumb.webp")

    # публичный просмотр опубликованной доски → появляется дата последнего просмотра
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 200

    refreshed = next(
        f for f in root_client.get(f"{API}/system/files").json()["items"] if f["id"] == work["id"]
    )
    assert refreshed["last_viewed_at"] is not None


def test_file_manager_delete_removes_file(root_client, manager_client):
    board, work = _board_with_work(manager_client, "Удаление файла")
    work_dir = media_service.work_dir(_uid_of(work))
    assert work_dir.exists()

    # менеджер не может удалять
    assert manager_client.delete(f"{API}/system/files/{work['id']}").status_code == 403

    resp = root_client.delete(f"{API}/system/files/{work['id']}")
    assert resp.status_code == 200
    assert "storage" in resp.json()

    assert not work_dir.exists()                                   # файлы удалены
    ids = [f["id"] for f in root_client.get(f"{API}/system/files").json()["items"]]
    assert work["id"] not in ids                                   # пропала из менеджера
    assert manager_client.get(f"{API}/boards/{board['id']}").status_code == 200  # доска цела
