from tests.conftest import API


def _create(client, name="Иван Петров", **extra):
    payload = {"name": name, **extra}
    response = client.post(f"{API}/clients", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_client_crud_and_search(manager_client):
    created = _create(
        manager_client,
        name="Кофейня Брусника",
        company="ООО Брусника",
        phone="+7 900 123-45-67",
        email="hello@brusnika.ru",
        tags=["логотип", "приоритет"],
    )
    assert created["tags"] == ["логотип", "приоритет"]

    # поиск по названию, телефону и тегу
    for query in ("Брусника", "123-45", "логотип"):
        found = manager_client.get(f"{API}/clients", params={"search": query}).json()
        assert any(c["id"] == created["id"] for c in found["items"]), query

    updated = manager_client.patch(
        f"{API}/clients/{created['id']}", json={"company": "ИП Брусника", "tags": "логотип"}
    )
    assert updated.status_code == 200
    assert updated.json()["company"] == "ИП Брусника"
    assert updated.json()["tags"] == ["логотип"]

    # имя не может стать пустым
    assert (
        manager_client.patch(f"{API}/clients/{created['id']}", json={"name": "  "}).status_code
        == 422
    )


def test_search_is_case_insensitive_for_cyrillic(manager_client):
    # Регистр приводится заранее, одной `search_norm` на запись и на запрос:
    # без этого «брусника» не находила «Брусника»
    created = _create(
        manager_client, name="Анна Ковалёва", company="Кофейня «Брусника»", tags=["Логотип"]
    )
    for query in ("брусника", "БРУСНИКА", "Брусника", "анна", "АННА", "логотип"):
        found = manager_client.get(f"{API}/clients", params={"search": query}).json()
        assert any(c["id"] == created["id"] for c in found["items"]), query


def test_soft_delete_and_root_restore(root_client, manager_client):
    client_id = _create(manager_client, name="Удаляемый")["id"]
    assert manager_client.delete(f"{API}/clients/{client_id}").status_code == 200
    assert manager_client.get(f"{API}/clients/{client_id}").status_code == 404
    # менеджер восстановить не может
    assert manager_client.post(f"{API}/clients/{client_id}/restore").status_code == 403
    # root — может
    assert root_client.post(f"{API}/clients/{client_id}/restore").status_code == 200
    assert manager_client.get(f"{API}/clients/{client_id}").status_code == 200


def test_notes_flow(root_client, manager_client):
    client_id = _create(manager_client, name="Клиент с историей")["id"]

    note = manager_client.post(
        f"{API}/clients/{client_id}/notes",
        json={"kind": "call", "body": "Обсудили логотип, ждёт варианты к пятнице"},
    )
    assert note.status_code == 201
    note_id = note.json()["id"]

    bad_kind = manager_client.post(
        f"{API}/clients/{client_id}/notes", json={"kind": "telepathy", "body": "x"}
    )
    assert bad_kind.status_code == 422

    notes = manager_client.get(f"{API}/clients/{client_id}/notes").json()
    assert notes["total"] == 1

    # чужой менеджер удалить не может, root — может
    from tests.conftest import make_manager

    other = make_manager(root_client, "other-notes@test.local")
    assert (
        other.delete(f"{API}/clients/{client_id}/notes/{note_id}").status_code == 403
    )
    assert (
        root_client.delete(f"{API}/clients/{client_id}/notes/{note_id}").status_code == 200
    )
    assert manager_client.get(f"{API}/clients/{client_id}/notes").json()["total"] == 0


def test_client_files_upload_download_delete(manager_client):
    client_id = _create(manager_client, name="Клиент с файлами")["id"]

    upload = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("бриф.pdf", b"%PDF-1.4 fake content", "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    file = upload.json()
    assert file["original_name"] == "бриф.pdf"

    # скачивание с сессией работает и отдаёт содержимое
    download = manager_client.get(file["download_url"])
    assert download.status_code == 200
    assert download.content == b"%PDF-1.4 fake content"

    # без сессии — 401
    from fastapi.testclient import TestClient

    from web.main import app

    assert TestClient(app).get(file["download_url"]).status_code == 401

    # запрещённый тип
    bad = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("virus.exe", b"MZ...", "application/octet-stream")},
    )
    assert bad.status_code == 422

    assert (
        manager_client.delete(f"{API}/clients/{client_id}/files/{file['id']}").status_code
        == 200
    )
    assert manager_client.get(file["download_url"]).status_code == 404


def test_a_phone_is_found_however_it_was_typed(manager_client):
    """Один номер, записанный по-разному, находится одинаково.

    Колонка `phone_norm` заведена ровно ради этого — «показываем то, что ввёл
    менеджер, а ищем по этому», — и телефония ею пользуется. Поисковая строка не
    пользовалась: две карточки с одним номером находились по-разному в
    зависимости от того, ставил ли менеджер пробелы.
    """
    tight = manager_client.post(
        f"{API}/clients", json={"name": "Слитно", "phone": "+380671112233"}
    ).json()
    spaced = manager_client.post(
        f"{API}/clients", json={"name": "С пробелами", "phone": "+380 67 111 22 33"}
    ).json()

    for needle in ("0671112233", "380671112233", "+380 67 111 22 33"):
        found = manager_client.get(f"{API}/clients", params={"search": needle}).json()["items"]
        names = {c["id"] for c in found}
        assert tight["id"] in names, f"«{needle}»: не нашёлся записанный слитно"
        assert spaced["id"] in names, f"«{needle}»: не нашёлся записанный с пробелами"


def test_filtr_po_metke_ne_nakhodit_chuzhie_metki(manager_client):
    """Метка совпадает целиком, а не подстрокой.

    Метки лежат одной строкой через запятую, и подстрочный поиск по ней
    находил чужие: фильтр `ip` возвращал всех, у кого стоит `vip`. Беда тихая —
    выдача не пустая, она просто не та, и заметить это можно лишь пересчитав
    карточки руками.
    """
    svoy = _create(manager_client, name="Метка своя", tags=["ip"])
    chuzhoy = _create(manager_client, name="Метка чужая", tags=["vip"])
    eshchyo = _create(manager_client, name="Метка внутри слова", tags=["equipment"])

    naydeno = manager_client.get(f"{API}/clients", params={"tag": "ip"}).json()["items"]
    nomera = {c["id"] for c in naydeno}

    assert svoy["id"] in nomera
    assert chuzhoy["id"] not in nomera, "фильтр «ip» вернул карточку с меткой «vip»"
    assert eshchyo["id"] not in nomera, "фильтр «ip» вернул карточку с меткой «equipment»"

    # Метка не первая и не последняя в списке — обрамление запятыми обязано
    # работать и в середине строки.
    seredina = _create(manager_client, name="Метка в середине", tags=["опт", "ip", "срочно"])
    v_seredine = manager_client.get(f"{API}/clients", params={"tag": "ip"}).json()["items"]
    assert seredina["id"] in {c["id"] for c in v_seredine}


def test_svg_klienta_chistitsya_pered_zapisyu(manager_client):
    """`<script>` в файле клиента не должен доезжать до диска.

    Работы досок и брендинг чистятся давно, файлы клиента — не чистились: это
    было единственное место, куда SVG попадал нетронутым.

    Сегодня цена невелика — файл отдаётся вложением с `nosniff`, браузер его не
    рисует. Но тогда безопасность держится на одном заголовке в одном
    маршруте: появится предпросмотр в списке файлов (а он напрашивается) — и
    скрипт сработает на странице сотрудника, в его сессии. Чистый файл на диске
    переживает любую такую правку, грязный — нет.

    Проверяется по СКАЧАННОМУ содержимому, а не по ответу на загрузку: важно,
    что лежит на диске.
    """
    client_id = _create(manager_client, name="Клиент со скриптом")["id"]
    gryaznyy = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<script>alert(1)</script>'
        b'<rect width="10" height="10" onload=alert(2)/>'
        b'<a xlink:href="javas&#99;ript:alert(3)">click</a>'
        b"</svg>"
    )
    zagruzka = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("картинка.svg", gryaznyy, "image/svg+xml")},
    )
    assert zagruzka.status_code == 201, zagruzka.text

    skachano = manager_client.get(zagruzka.json()["download_url"]).content
    for opasnoe in (b"<script", b"onload", b"javas"):
        assert opasnoe not in skachano.lower(), (
            f"в файле клиента осталось {opasnoe!r}: {skachano!r}"
        )
    assert b"<rect" in skachano, "чистка выбросила и саму картинку"

    # Размер в базе обязан совпасть с тем, что вправду лежит: список файлов
    # показывал бы одно, а скачивалось бы другое.
    assert zagruzka.json()["size_bytes"] == len(skachano), (
        "в базе записан размер ДО очистки"
    )
