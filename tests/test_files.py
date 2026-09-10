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


# --- блок «Файлы»: дерево, свои папки, загрузка ---

def test_derevo_pokazyvaet_raboty_dosok(root_client, manager_client):
    """Работы досок видны в дереве и в содержимом ветки доски.

    Дерево повторяет раскладку `storage/`: у работ своя ветка, у своих файлов
    своя. Плоского списка, который был здесь раньше, больше нет — он отвечал
    только за медиа досок и не знал ни о вложениях карточек, ни о снимках
    товара, лежащих на том же диске.
    """
    board, work = _board_with_work(manager_client, "Доска для дерева")

    otvet = root_client.get(f"{API}/files/tree")
    assert otvet.status_code == 200, otvet.text
    derevo = otvet.json()["tree"]
    assert "storage" in otvet.json(), "полоса занятого места приходит тем же ответом"
    doski = next(v for v in derevo["kids"] if v["id"] == "boards")
    moya = next(d for d in doski["kids"] if d["name"] == "Доска для дерева")
    assert moya["n"] >= 1
    assert any(k["id"] == f"board:{board['id']}:image" for k in moya["kids"])

    stroki = root_client.get(f"{API}/files?node=board:{board['id']}").json()
    zapis = next(f for f in stroki["items"] if f["id"] == f"work:{work['id']}")
    assert zapis["where"] == "Доска для дерева"
    assert zapis["open"] == f"/boards/{board['id']}"
    assert zapis["size_bytes"] > 0
    assert zapis["kind"] == "image"


def test_vetka_ne_poyavlyaetsya_bez_prava(manager_client, root_client):
    """Ветка появляется вместе с правом на раздел, из которого файлы.

    Не пустая ветка, а именно её отсутствие: файл клиента — данные клиента, и
    обойти права на них через менеджер файлов нельзя. Узел, на который права
    нет, отвечает отказом, а не пустым списком: пустой читается как «файлов
    нет», и человек решает, что их удалили.

    Должность, заведённая до появления блока, права на него не получает —
    пресеты засевают только НОВЫЕ роли. Поэтому менеджер тестового стенда сюда
    не входит вовсе, и это верно: доступ к чужим файлам выдаёт человек, а не
    обновление.
    """
    assert manager_client.get(f"{API}/files/tree").status_code == 403

    korni = {v["id"] for v in root_client.get(f"{API}/files/tree").json()["tree"]["kids"]}
    assert {"boards", "clients", "own"} <= korni

    # Выключенный блок уносит свою ветку целиком, а его узел отвечает отказом.
    assert root_client.post(f"{API}/modules/boards", json={"enabled": False}).status_code == 200
    try:
        bez_dosok = {v["id"] for v in root_client.get(f"{API}/files/tree").json()["tree"]["kids"]}
        assert "boards" not in bez_dosok
        assert "own" in bez_dosok, "свои папки блоком досок не закрываются"
        otkaz = root_client.get(f"{API}/files?node=boards")
        assert otkaz.status_code == 403 and otkaz.json()["error"]["code"] == "permission_denied"
    finally:
        assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200


def test_svoya_papka_prinimaet_fayl_i_unosit_ego_s_soboy(root_client):
    """Папка, файл в ней, выдача файла и уборка папки вместе с диском."""
    papka = root_client.post(f"{API}/files/folders", json={"name": "Договоры"})
    assert papka.status_code == 201, papka.text
    papka_id = papka.json()["id"]

    dvazhdy = root_client.post(f"{API}/files/folders", json={"name": "Договоры"})
    assert dvazhdy.status_code == 422 and dvazhdy.json()["error"]["code"] == "folder_exists"

    vlozhennaya = root_client.post(
        f"{API}/files/folders", json={"name": "2026", "parent_id": papka_id}
    ).json()

    zalivka = root_client.post(
        f"{API}/files?folder_id={vlozhennaya['id']}",
        files={"file": ("dogovor.txt", "аренда, редакция 3".encode("utf-8"), "text/plain")},
    )
    assert zalivka.status_code == 201, zalivka.text
    nomer = int(zalivka.json()["id"].split(":")[1])

    from core.services import fayly_service
    from database.repositories import fayly as fayly_repo
    from database.session import SessionLocal

    with SessionLocal() as db:
        na_diske = fayly_service.fayl_na_diske(fayly_repo.fayl(db, nomer))
    assert na_diske.exists()

    otdacha = root_client.get(f"{API}/files/{nomer}/download")
    assert otdacha.status_code == 200
    assert otdacha.headers["content-disposition"].endswith('"dogovor.txt"')

    v_vetke = root_client.get(f"{API}/files?node=folder:{vlozhennaya['id']}").json()
    assert [f["id"] for f in v_vetke["items"]] == [f"stored:{nomer}"]

    # Папка уходит вместе с вложенной и её файлами — и с диска тоже.
    assert root_client.delete(f"{API}/files/folders/{papka_id}").status_code == 200
    assert not na_diske.exists()
    assert root_client.get(f"{API}/files/{nomer}/download").status_code == 404


def test_chuzhoy_tip_ne_prinimaetsya(root_client):
    """Приёмка общая со вложениями карточек, и перечень расширений тот же."""
    otkaz = root_client.post(
        f"{API}/files", files={"file": ("virus.exe", b"MZ\x90", "application/octet-stream")}
    )
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "file_type_not_allowed"


def test_udalenie_raboty_ostalos_pod_nastroykami(root_client, manager_client):
    """Работа доски сносится С ДОСКИ, и право на это осталось настройками.

    Правом блока «Файлы» такое закрываться не должно: тот, кому открыли папку
    с бумагами фирмы, не получает вместе с ней возможность разобрать чужое
    портфолио.
    """
    board, work = _board_with_work(manager_client, "Удаление файла")
    work_dir = media_service.work_dir(_uid_of(work))
    assert work_dir.exists()

    assert manager_client.delete(f"{API}/system/files/{work['id']}").status_code == 403

    resp = root_client.delete(f"{API}/system/files/{work['id']}")
    assert resp.status_code == 200
    assert "storage" in resp.json()

    assert not work_dir.exists()
    ostalis = root_client.get(f"{API}/files?node=board:{board['id']}").json()["items"]
    assert f"work:{work['id']}" not in [f["id"] for f in ostalis]
    assert manager_client.get(f"{API}/boards/{board['id']}").status_code == 200
