import itertools

from tests.conftest import API

_schyot = itertools.count(1)


def uniq() -> str:
    """Своя метка на каждую проверку: набор гоняется в обоих порядках, и
    оставленные соседом клиенты не должны попадаться на глаза."""
    return f"{next(_schyot):05d}"


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


# --- выгрузка списка клиентов -------------------------------------------------


def test_vygruzka_otdayot_to_zhe_chto_i_ekran(manager_client):
    """Файл обязан содержать ровно то, что показывает список.

    Выгрузка, собранная своим отбором, однажды отдала бы не то, что человек
    видит на экране, — и заметить это можно было бы только сверкой двух
    файлов. Поэтому проверяется ПАРА: что нашлось по метке на экране, то и
    лежит в файле.
    """
    metka = f"vygruzka{uniq()}"
    svoi = _create(manager_client, name=f"Клиент выгрузки {metka}", tags=[metka])["id"]
    _create(manager_client, name=f"Посторонний {metka}-нет")

    otvet = manager_client.get(f"{API}/clients/export.csv?tag={metka}")
    assert otvet.status_code == 200, otvet.text
    assert otvet.headers["content-type"].startswith("text/csv")
    assert "attachment" in otvet.headers["content-disposition"]

    # BOM — не придирка: без него Excel открывает файл в системной кодировке,
    # и кириллица превращается в кракозябры при верном содержимом.
    assert otvet.content.startswith(b"\xef\xbb\xbf"), "файл без метки UTF-8 — Excel покажет кракозябры"
    tekst = otvet.content.decode("utf-8-sig")
    assert f"Клиент выгрузки {metka}" in tekst
    assert f"Посторонний {metka}-нет" not in tekst, "в файл попал клиент мимо отбора"

    # Отбор экрана и отбор файла — одни и те же строки, а не «похожие».
    na_ekrane = manager_client.get(f"{API}/clients?tag={metka}").json()
    assert na_ekrane["total"] == len([s for s in tekst.splitlines()[1:] if s.strip()])
    assert svoi


def test_vygruzka_podpisyvaet_otvetstvennogo_imenem(manager_client, root_client):
    """`manager_id=7` в таблице Excel не отвечает ни на один вопрос.

    Номер в выгрузке означает, что человек пойдёт сверять его с другим
    экраном, — то есть выгрузка не заменяет работу, а добавляет её.
    """
    metka = f"otvetstvennyy{uniq()}"
    ya = root_client.get(f"{API}/auth/me").json()
    _create(manager_client, name=f"С ответственным {metka}", tags=[metka],
            manager_id=ya["id"])
    tekst = manager_client.get(f"{API}/clients/export.csv?tag={metka}").content.decode("utf-8-sig")
    assert ya["name"] in tekst, "ответственный подписан номером, а не именем: " + tekst


def test_slishkom_bolshaya_vygruzka_otkazyvaet_a_ne_obrezaet(manager_client, monkeypatch):
    """Молчаливое обрезание хуже отказа.

    Файл, в котором тихо недостаёт половины клиентов, выглядит полным: ни
    строки о том, что часть осталась за краем. Человек уносит его в работу и
    обнаруживает пропажу тогда, когда обзвонил всех «оставшихся».

    Предел выкручивается в единицу, а не заводятся десять тысяч клиентов:
    проверяется правило, а не терпение базы.
    """
    from core.services import client_service

    metka = f"predel{uniq()}"
    for nomer in range(2):
        _create(manager_client, name=f"Много {metka} {nomer}", tags=[metka])
    monkeypatch.setattr(client_service, "PREDEL_VYGRUZKI", 1)

    otvet = manager_client.get(f"{API}/clients/export.csv?tag={metka}")
    assert otvet.status_code == 422, "выгрузка молча обрезалась"
    assert otvet.json()["error"]["code"] == "export_too_large"


#: Первые байты настоящего PNG. Через fromhex, а не escape-последовательностью:
#: подпись здесь — двоичные данные, и вид у неё должен быть двоичный.
PNG_ZAGOLOVOK = bytes.fromhex("89504e470d0a1a0a")


def test_fayl_klienta_proveryaetsya_po_soderzhimomu(manager_client):
    """Файл, назвавшийся не тем, не принимается — и тип берём не у загрузившего.

    Расширение выбирает тот, кто загружает; содержимое — нет. Пока сходились они
    только на слово, `otchet.pdf` мог оказаться чем угодно, а `logotip.png` —
    страницей со скриптом.

    Сегодня файл отдаётся вложением с `nosniff`, и браузер его не рисует. Но это
    ровно тот довод, который уже подводил с SVG: безопасность держалась на одном
    заголовке в одном маршруте, а появись предпросмотр в списке файлов — и
    подделка сработала бы в сессии сотрудника.
    """
    client_id = _create(manager_client, name="Клиент подделок")["id"]

    # Исполняемый под видом документа.
    podlog = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("otchet.pdf", bytes.fromhex("4d5a9000") + b" executable", "application/pdf")},
    )
    assert podlog.status_code == 422, podlog.text
    assert podlog.json()["error"]["code"] == "file_content_mismatch", podlog.text

    # Страница со скриптом под видом картинки — то, что оживёт при первом же
    # предпросмотре.
    kartinka = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("logotip.png", b"<html><script>alert(1)</script>", "image/png")},
    )
    assert kartinka.status_code == 422, kartinka.text
    assert kartinka.json()["error"]["code"] == "file_content_mismatch"

    # Настоящий PNG проходит.
    nastoyashchiy = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("logotip.png", PNG_ZAGOLOVOK + b"telo", "image/png")},
    )
    assert nastoyashchiy.status_code == 201, nastoyashchiy.text

    # Тип в ответе — наш, а не присланный. Иначе в заголовок ответа сотруднику
    # уходило бы значение, выбранное тем, кто загрузил файл.
    lozhnyy_tip = manager_client.post(
        f"{API}/clients/{client_id}/files",
        files={"file": ("smeta.pdf", b"%PDF-1.4 smeta", "text/html")},
    )
    assert lozhnyy_tip.status_code == 201, lozhnyy_tip.text
    otdacha = manager_client.get(lozhnyy_tip.json()["download_url"])
    assert otdacha.status_code == 200
    assert otdacha.headers["content-type"].startswith("application/pdf"), (
        f"наружу ушёл присланный тип: {otdacha.headers['content-type']}"
    )
    assert otdacha.headers.get("x-content-type-options") == "nosniff"
