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


def test_adres_zapisyvaetsya_i_otdayotsya(manager_client):
    """Адрес отправки: четыре поля, а не одна строка. Разбор — docs/19 §Р7."""
    klient = _create(
        manager_client,
        name=f"Получатель {uniq()}",
        country="ua",
        city="Киев",
        zip_code="01001",
        address="ул. Крещатик, 1, кв. 5",
    )
    # Код страны приводится к верхнему регистру: пришедшее с сайта `ua` и
    # набранное руками `UA` — одна и та же страна, а не две.
    assert klient["country"] == "UA"
    assert klient["city"] == "Киев"
    assert klient["zip_code"] == "01001"

    kartochka = manager_client.get(f"{API}/clients/{klient['id']}").json()
    assert kartochka["address"] == "ул. Крещатик, 1, кв. 5"


def test_adres_pravitsya_po_chastyam(manager_client):
    klient = _create(manager_client, name=f"Переезд {uniq()}", city="Львов", zip_code="79000")
    otvet = manager_client.patch(f"{API}/clients/{klient['id']}", json={"city": "Одесса"})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["city"] == "Одесса"
    assert otvet.json()["zip_code"] == "79000", "прислали город, а сбросился индекс"


def test_pustoy_adres_norma(manager_client):
    """Половина клиентов забирает сама — пустой адрес это не дыра в данных."""
    klient = _create(manager_client, name=f"Самовывоз {uniq()}")
    assert klient["country"] == "" and klient["city"] == ""
    assert klient["zip_code"] == "" and klient["address"] == ""


def test_strana_tolko_dvumya_bukvami(manager_client):
    """Название страны пишут по-разному, и отбор по нему не собрать."""
    otkaz = manager_client.post(
        f"{API}/clients", json={"name": f"Страна {uniq()}", "country": "Украина"}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "country_invalid"


def test_dlinnyy_adres_otkaz_a_ne_pyatisotka(manager_client):
    """Строка длиннее колонки роняла бы вставку отказом базы.

    Магазин, приславший длинный адрес, получал бы 500 вместо «слишком длинно».
    """
    otkaz = manager_client.post(
        f"{API}/clients", json={"name": f"Длинный {uniq()}", "address": "у" * 301}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "address_too_long"


def test_strana_po_kodu_nomera():
    """Код набора называет страну — спрашивать её второй раз незачем."""
    from core.strany import strana_po_nomeru

    assert strana_po_nomeru("380671234567") == "UA"
    assert strana_po_nomeru("48123456789") == "PL"
    assert strana_po_nomeru("79161234567") == "RU"
    # Казахстан делит «+7» с Россией, но расходится во второй цифре.
    assert strana_po_nomeru("77011234567") == "KZ"
    assert strana_po_nomeru("38512345678") == "HR"


def test_ni_odin_kod_ne_pristavka_drugogo():
    """Приставочных пар в таблице нет — и подбор от длинного к короткому это
    страхует, а не чинит.

    Проверка стоит здесь ради ПОПОЛНЕНИЯ таблицы: добавят `1` рядом с `12` — и
    все номера второй страны молча станут первой. Поймать это глазами нельзя,
    потому что ломается не добавленная строка, а соседняя.
    """
    from core.strany import KODY_STRAN

    kody = sorted(KODY_STRAN)
    pary = [
        (korotkiy, dlinnyy)
        for korotkiy in kody
        for dlinnyy in kody
        if dlinnyy != korotkiy and dlinnyy.startswith(korotkiy)
    ]
    assert not pary, f"код — приставка другого: {pary[:5]}"


def test_obshchiy_kod_ne_dayot_strany():
    """`+1` — это и США, и Канада. Флаг наугад хуже отсутствующего: по нему
    однажды посчитают доставку по чужому тарифу, и заметят это на почте."""
    from core.strany import strana_po_nomeru

    assert strana_po_nomeru("12125551234") == ""
    # Местный номер без кода страны тоже молчит — дописать чужую страну хуже,
    # чем не дописать никакой (тот же довод, что у normalize_phone).
    assert strana_po_nomeru("0671234567") == ""
    assert strana_po_nomeru("") == ""


def test_strana_podstavlyaetsya_iz_nomera(manager_client):
    klient = _create(manager_client, name=f"По номеру {uniq()}", phone="+380 67 123 45 67")
    assert klient["country"] == "UA"


def test_nazvannaya_strana_silnee_nomera(manager_client):
    """Человек сказал страну явно — подсказка по номеру её не перебивает."""
    klient = _create(
        manager_client, name=f"Явно {uniq()}", phone="+380 67 123 45 67", country="PL"
    )
    assert klient["country"] == "PL"


def test_strana_edet_za_nomerom_poka_s_ney_ne_sporili(manager_client):
    klient = _create(manager_client, name=f"Переезд {uniq()}", phone="+380 67 123 45 67")
    assert klient["country"] == "UA"

    stalo = manager_client.patch(
        f"{API}/clients/{klient['id']}", json={"phone": "+48 12 345 67 89"}
    )
    assert stalo.status_code == 200, stalo.text
    assert stalo.json()["country"] == "PL", "страна осталась от прежнего номера"


def test_ruchnaya_strana_perezhivaet_pravku_nomera(manager_client):
    """Правка телефона не имеет права молча затирать страну, названную человеком."""
    klient = _create(
        manager_client, name=f"Своя страна {uniq()}", phone="+380 67 123 45 67", country="DE"
    )
    stalo = manager_client.patch(
        f"{API}/clients/{klient['id']}", json={"phone": "+48 12 345 67 89"}
    )
    assert stalo.json()["country"] == "DE"


def test_svodka_klienta_v_kartochke(root_client):
    """Справа от паспорта — заявки, деньги, последний контакт, бумаги, кто ведёт
    (владелец, 06.09.2026: пустое место заполнить полезным)."""
    me = root_client.get(f"{API}/auth/me").json()
    klient = root_client.post(f"{API}/clients", json={"name": "Сводка карточки", "manager_id": me["id"]}).json()
    root_client.post(f"{API}/deals", json={"title": "Открытая", "client_id": klient["id"], "amount": 5_000})
    stages = {s["kind"]: s["key"] for s in root_client.get(f"{API}/pipeline/stages").json()["items"]}
    vyigrana = root_client.post(f"{API}/deals", json={"title": "Выигранная", "client_id": klient["id"], "amount": 7_000}).json()
    assert root_client.post(f"{API}/deals/{vyigrana['id']}/move", json={"stage": stages["won"]}).status_code == 200
    root_client.post(f"{API}/clients/{klient['id']}/notes", json={"kind": "note", "body": "Звонил, просил счёт"})

    svodka = root_client.get(f"{API}/clients/{klient['id']}").json()["svodka"]
    assert svodka["open_count"] == 1 and svodka["open_amount"] == 5_000
    assert svodka["won_count"] == 1 and svodka["won_amount"] == 7_000
    assert svodka["lost_count"] == 0
    assert svodka["last_contact"]["kind"] == "note" and "просил счёт" in svodka["last_contact"]["body"]
    assert svodka["last_contact"]["at"]
    assert svodka["manager_name"] == me["name"]
    assert svodka["received_12m"] is None, "деньги выключены — плитки нет"


def test_posledniy_kontakt_ne_sistemnaya_zapis(root_client):
    """Системная строка ленты («Stage: …», «Document … issued») свежее звонка
    занимала место последнего контакта (проба 06.09.2026)."""
    klient = root_client.post(f"{API}/clients", json={"name": "Контакт, не система"}).json()
    root_client.post(f"{API}/clients/{klient['id']}/notes", json={"kind": "call", "body": "Договорились о встрече"})
    zayavka = root_client.post(f"{API}/deals", json={"title": "После звонка", "client_id": klient["id"]}).json()
    stages = {s["kind"]: s["key"] for s in root_client.get(f"{API}/pipeline/stages").json()["items"]}
    assert root_client.post(f"{API}/deals/{zayavka['id']}/move", json={"stage": stages["won"]}).status_code == 200
    lenta = root_client.get(f"{API}/clients/{klient['id']}/notes").json()["items"]
    assert lenta[0]["kind"] == "stage", "перенос по доске пишет системную запись, и она свежее звонка"

    svodka = root_client.get(f"{API}/clients/{klient['id']}").json()["svodka"]
    assert svodka["last_contact"]["kind"] == "call" and "о встрече" in svodka["last_contact"]["body"]


def test_spisok_klientov_znaet_zayavki_i_posledniy_kontakt(root_client):
    """Колонки списка (владелец, 06.09.2026): заявок и на сколько, последний
    контакт — по два запроса на страницу, не на строку."""
    klient = root_client.post(f"{API}/clients", json={"name": "Список со сводкой"}).json()
    root_client.post(f"{API}/deals", json={"title": "Первая", "client_id": klient["id"], "amount": 4_000})
    root_client.post(f"{API}/deals", json={"title": "Вторая", "client_id": klient["id"]})
    root_client.post(f"{API}/clients/{klient['id']}/notes", json={"kind": "call", "body": "Перезвонил", "direction": "out"})

    stroki = root_client.get(f"{API}/clients", params={"search": "Список со сводкой"}).json()["items"]
    [stroka] = [s for s in stroki if s["id"] == klient["id"]]
    assert stroka["deals_open"] == 2 and stroka["deals_open_amount"] == 4_000
    assert stroka["deals_won"] == 0
    assert stroka["last_contact_at"], "звонок в ленте — это контакт"
