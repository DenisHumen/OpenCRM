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


# --- ссылки наружу ---

def _svoy_fayl(client, imya="dogovor.txt", telo="аренда"):
    """Свой файл в корне «Загрузок». Содержимое сверяется с расширением, и
    выдуманный PNG приёмка не пропустит — картинку берём настоящую."""
    bayty = png_bytes() if imya.endswith(".png") else telo.encode("utf-8")
    tip = "image/png" if imya.endswith(".png") else "text/plain"
    otvet = client.post(f"{API}/files", files={"file": (imya, bayty, tip)})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()["id"]


def test_ssylka_otdayot_fayl_tolko_v_svoyom_rezhime(root_client):
    """«Смотреть» без «скачать» — отдельное решение, и оно отказывает, а не
    прячет кнопку.

    Спрятанная кнопка означает «не нашёл», а не «нельзя»: адрес выдачи в
    разметке страницы виден любому, кто её сохранил.
    """
    nomer = _svoy_fayl(root_client, "smotret.txt", "только смотреть")
    vypusk = root_client.post(f"{API}/files/{nomer}/link", json={"rezhim": "view", "krug": "link"})
    assert vypusk.status_code == 201, vypusk.text
    ssylka = vypusk.json()["link"]
    token = ssylka["url"].rsplit("/", 1)[-1]

    guest = TestClient(app)
    stranitsa = guest.get(f"/f/{token}")
    assert stranitsa.status_code == 200
    assert "smotret.txt" in stranitsa.text
    assert guest.get(f"/f/{token}/download").status_code == 403, "смотреть — значит не скачивать"

    pravka = root_client.patch(
        f"{API}/files/links/{ssylka['id']}", json={"rezhim": "download", "krug": "link"}
    )
    assert pravka.status_code == 200, pravka.text
    skachalos = guest.get(f"/f/{token}/download")
    assert skachalos.status_code == 200
    assert skachalos.content.decode("utf-8") == "только смотреть"
    assert "smotret.txt" in skachalos.headers["content-disposition"]


def test_kod_zakryvaet_i_stranitsu_i_bayty(root_client):
    """Код закрывает ОБА пути. У витрин однажды было наоборот: страница
    отвечала 401, а файлы 200 — то есть «доступно навсегда тому, кому однажды
    показали», ровно то, от чего код и заводят."""
    nomer = _svoy_fayl(root_client, "pod-kodom.txt", "секрет")
    vypusk = root_client.post(
        f"{API}/files/{nomer}/link",
        json={"rezhim": "download", "krug": "code", "pin": "4079"},
    )
    assert vypusk.status_code == 201, vypusk.text
    ssylka = vypusk.json()["link"]
    assert ssylka["has_code"] is True
    token = ssylka["url"].rsplit("/", 1)[-1]

    guest = TestClient(app)
    stranitsa = guest.get(f"/f/{token}")
    assert stranitsa.status_code == 200
    assert "pod-kodom.txt" not in stranitsa.text, "страница кода не раскрывает даже имени"
    assert guest.get(f"/f/{token}/download").status_code == 401, "байты закрыты тем же кодом"

    assert guest.post(f"/f/{token}/pin", data={"pin": "1111"}).status_code == 401
    proshli = guest.post(f"/f/{token}/pin", data={"pin": "4079"}, follow_redirects=False)
    assert proshli.status_code == 303
    assert "pod-kodom.txt" in guest.get(f"/f/{token}").text
    assert guest.get(f"/f/{token}/download").status_code == 200


def test_srok_i_otzyv_zakryvayut_ssylku(root_client):
    """Истёкший срок и отзыв закрывают ссылку одинаково — снаружи не видно,
    что именно случилось."""
    nomer = _svoy_fayl(root_client, "srok.txt", "до вторника")
    ssylka = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "download", "krug": "link"}
    ).json()["link"]
    token = ssylka["url"].rsplit("/", 1)[-1]
    guest = TestClient(app)
    assert guest.get(f"/f/{token}").status_code == 200

    root_client.patch(
        f"{API}/files/links/{ssylka['id']}",
        json={"rezhim": "download", "krug": "link", "expires_at": "2020-01-01T00:00:00"},
    )
    assert guest.get(f"/f/{token}").status_code == 404
    assert guest.get(f"/f/{token}/download").status_code == 404

    root_client.patch(
        f"{API}/files/links/{ssylka['id']}",
        json={"rezhim": "download", "krug": "link", "expires_at": None},
    )
    assert guest.get(f"/f/{token}").status_code == 200

    assert root_client.delete(f"{API}/files/links/{ssylka['id']}").status_code == 200
    assert guest.get(f"/f/{token}").status_code == 404
    assert root_client.get(f"{API}/files/{nomer}/link").json()["link"] is None


def test_ssylka_u_fayla_odna(root_client):
    """Второй выпуск правит первую, а не заводит вторую: два набора условий на
    одни байты означали бы, что отзывать надо обе, помня о второй."""
    nomer = _svoy_fayl(root_client, "odna.txt", "раз")
    pervaya = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "view", "krug": "link"}
    ).json()["link"]
    vtoraya = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "download", "krug": "link"}
    ).json()["link"]
    assert vtoraya["id"] == pervaya["id"]
    assert vtoraya["rezhim"] == "download"
    assert vtoraya["url"] == pervaya["url"], "адрес не меняется: его уже переслали"
    root_client.delete(f"{API}/files/links/{pervaya['id']}")


def test_krug_po_kodu_bez_koda_ne_zavoditsya(root_client):
    """«По коду» без кода — открытая ссылка, которая называется закрытой."""
    nomer = _svoy_fayl(root_client, "bez-koda.txt", "нет кода")
    otkaz = root_client.post(f"{API}/files/{nomer}/link", json={"rezhim": "view", "krug": "code"})
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "link_code_required"

    ssylka = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "view", "krug": "code", "pin": "4079"}
    ).json()["link"]
    # Переключение обратно снимает код: оставленный хвост вернулся бы вместе с
    # переключением и потребовал бы кода, которого никто не называл.
    obratno = root_client.patch(
        f"{API}/files/links/{ssylka['id']}", json={"rezhim": "view", "krug": "link"}
    ).json()["link"]
    assert obratno["has_code"] is False
    root_client.delete(f"{API}/files/links/{ssylka['id']}")


def test_udalenie_fayla_unosit_ssylku(root_client):
    """Каскад: снесли файл — ссылка на него не ведёт в никуда."""
    nomer = _svoy_fayl(root_client, "unesyot.txt", "уйдёт")
    ssylka = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "download", "krug": "link"}
    ).json()["link"]
    token = ssylka["url"].rsplit("/", 1)[-1]
    guest = TestClient(app)
    assert guest.get(f"/f/{token}").status_code == 200

    assert root_client.delete(f"{API}/files/{nomer.split(':')[1]}").status_code == 200
    assert guest.get(f"/f/{token}").status_code == 404


def test_vyklyuchennyy_blok_gasit_ssylki(root_client):
    """Выключенный блок исчезает ЦЕЛИКОМ: разосланные ссылки перестают отдавать
    бумаги фирмы, а не только пункт уходит из меню."""
    nomer = _svoy_fayl(root_client, "vyklyuchat.txt", "тайна")
    ssylka = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "download", "krug": "link"}
    ).json()["link"]
    token = ssylka["url"].rsplit("/", 1)[-1]
    guest = TestClient(app)
    assert guest.get(f"/f/{token}").status_code == 200

    assert root_client.post(f"{API}/modules/files", json={"enabled": False}).status_code == 200
    try:
        assert guest.get(f"/f/{token}").status_code == 404
        assert guest.get(f"/f/{token}/download").status_code == 404
    finally:
        assert root_client.post(f"{API}/modules/files", json={"enabled": True}).status_code == 200
    assert guest.get(f"/f/{token}").status_code == 200
    root_client.delete(f"{API}/files/links/{ssylka['id']}")


def test_bez_prava_delitsya_nelzya(manager_client, root_client):
    """Право `files.share` отдельное: смотреть файл внутри фирмы и положить его
    в интернет — разные полномочия."""
    nomer = _svoy_fayl(root_client, "chuzhoye.txt", "не всем")
    assert manager_client.post(f"{API}/files/{nomer}/link", json={"rezhim": "view", "krug": "link"}).status_code == 403
    assert manager_client.get(f"{API}/files/{nomer}/link").status_code == 403


def test_pravka_sroka_ne_snimaet_kod(root_client):
    """Поле, которого в теле НЕТ, значит «не трогай», а не «сними».

    Пока и то и другое было одним `None`, смена срока у ссылки под кодом
    отвечала «нужен код»: поправить срок было нельзя вовсе, а понять почему —
    неоткуда, потому что кода никто и не трогал.
    """
    nomer = _svoy_fayl(root_client, "srok-i-kod.txt", "оба")
    ssylka = root_client.post(
        f"{API}/files/{nomer}/link",
        json={"rezhim": "view", "krug": "code", "pin": "4079"},
    ).json()["link"]
    assert ssylka["has_code"] is True

    # Прислали всё, кроме кода — код на месте.
    tolko_srok = root_client.patch(
        f"{API}/files/links/{ssylka['id']}",
        json={"rezhim": "view", "krug": "code", "expires_at": "2027-01-01T00:00:00"},
    )
    assert tolko_srok.status_code == 200, tolko_srok.text
    assert tolko_srok.json()["link"]["has_code"] is True
    assert tolko_srok.json()["link"]["expires_at"].startswith("2027-01-01")

    # Прислали пустой код вместе с кругом «по ссылке» — сняли.
    snyali = root_client.patch(
        f"{API}/files/links/{ssylka['id']}",
        json={"rezhim": "view", "krug": "link", "pin": None},
    )
    assert snyali.status_code == 200, snyali.text
    assert snyali.json()["link"]["has_code"] is False
    root_client.delete(f"{API}/files/links/{ssylka['id']}")


# --- защита просмотра ---

def test_bayty_na_prosmotr_trebuyut_klyucha(root_client):
    """Адрес картинки со страницы живёт десять минут и только по подписи.

    Это то, что защита вправду закрывает: «скопировал адрес картинки и
    переслал». Снимок экрана она не закрывает — и в окне владельца так и
    написано.
    """
    nomer = _svoy_fayl(root_client, "kartinka.png", "PNG")
    ssylka = root_client.post(
        f"{API}/files/{nomer}/link", json={"rezhim": "view", "krug": "link"}
    ).json()["link"]
    token = ssylka["url"].rsplit("/", 1)[-1]

    guest = TestClient(app)
    stranitsa = guest.get(f"/f/{token}")
    assert stranitsa.status_code == 200
    # Ключ страница выдаёт сама — вынимаем его оттуда, как это делает браузер.
    import re

    nayden = re.search(rf"/f/{token}/view\?k=([\w\.\-]+)", stranitsa.text)
    assert nayden, "страница не выдала ключ просмотра"
    klyuch = nayden.group(1)

    assert guest.get(f"/f/{token}/view").status_code == 403, "без ключа байты не отдаются"
    assert guest.get(f"/f/{token}/view?k=podelka").status_code == 403, "подделка не проходит"
    otvet = guest.get(f"/f/{token}/view?k={klyuch}")
    assert otvet.status_code == 200
    assert otvet.headers["content-disposition"] == "inline", "показ, а не сохранение"
    assert otvet.headers["cache-control"] == "no-store"
    root_client.delete(f"{API}/files/links/{ssylka['id']}")


def test_klyuch_odnoy_ssylki_ne_otkryvaet_druguyu(root_client):
    """Ключ привязан к ссылке: подпись от соседней не открывает эту."""
    pervyy = _svoy_fayl(root_client, "pervyy.png", "один")
    vtoroy = _svoy_fayl(root_client, "vtoroy.png", "два")
    a = root_client.post(f"{API}/files/{pervyy}/link", json={"rezhim": "view", "krug": "link"}).json()["link"]
    b = root_client.post(f"{API}/files/{vtoroy}/link", json={"rezhim": "view", "krug": "link"}).json()["link"]
    token_a = a["url"].rsplit("/", 1)[-1]
    token_b = b["url"].rsplit("/", 1)[-1]

    guest = TestClient(app)
    import re

    klyuch_b = re.search(rf"/f/{token_b}/view\?k=([\w\.\-]+)", guest.get(f"/f/{token_b}").text).group(1)
    assert guest.get(f"/f/{token_a}/view?k={klyuch_b}").status_code == 403

    root_client.delete(f"{API}/files/links/{a['id']}")
    root_client.delete(f"{API}/files/links/{b['id']}")


def test_kod_zakryvaet_i_prosmotr(root_client):
    """Показ закрыт тем же кодом, что и страница: иначе «по коду» означало бы
    «страница по коду, а картинка всем»."""
    nomer = _svoy_fayl(root_client, "zakryto.png", "тайна")
    ssylka = root_client.post(
        f"{API}/files/{nomer}/link",
        json={"rezhim": "view", "krug": "code", "pin": "4079"},
    ).json()["link"]
    token = ssylka["url"].rsplit("/", 1)[-1]

    guest = TestClient(app)
    # Ключа у гостя нет вовсе — страница его не выдала, она показала код.
    assert "/view?k=" not in guest.get(f"/f/{token}").text
    assert guest.get(f"/f/{token}/view").status_code == 401, "сначала код, потом ключ"
    root_client.delete(f"{API}/files/links/{ssylka['id']}")
