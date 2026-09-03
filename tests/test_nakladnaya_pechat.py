"""Печатная форма накладной: бумага, которая уезжает вместе с грузом.

Накладную подписывают В МОМЕНТ передачи, стоя у машины, и спорят потом по тому
листу, что остался на руках. Отсюда состав проверок: на бумаге обязаны быть
позиции, единицы, складывающийся итог и имя того, кто отпустил, — а того, чего
там быть не должно, быть не должно ни при каких правах.

**Две беды, ради которых файл написан.**

1. *Печаталось не то.* Ручки печати у накладной не было вовсе, а общая печать
   бланка (`/documents/{id}/print`) брала запись по номеру, ни о чём не
   спрашивая, — и накладная выходила КВИТАНЦИЕЙ ПРИЁМА: две половины с линией
   отреза, пустые поля «что приняли», ни перечня, ни сумм. Заказ — так же.

2. *Шапка молча теряла адрес.* Шаблоны спрашивали `company.address`, а в снимке
   такого ключа нет никогда — есть `legal_address`. Jinja на несуществующий
   ключ не жалуется, поэтому акт и заказ год печатались без адреса фирмы, и
   покраснеть было нечему.

Вторая беда — про класс ошибок, а не про одно поле, поэтому её стережёт разбор
самих шаблонов: имя поля, которого снимок не умеет отдавать, отбивается сразу.
"""

import itertools
import pathlib
import re

import pytest

from core.services import company_service
from tests.conftest import API, make_manager

WAYBILLS = f"{API}/waybills"
STOCK = f"{API}/warehouse"
SHABLONY = pathlib.Path(__file__).resolve().parent.parent / "web" / "public" / "templates"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(scope="module", autouse=True)
def blocks_on(root_client):
    """Накладные стоят на бланках, а склад им нужен для проведения.

    Раз на файл, а не на тест: переключение блока пишется в журнал действий, и
    проверки журнала краснеют от чужого шума на первой странице.
    """
    for key in ("documents", "warehouse", "orders", "waybills", "companies"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    yield


def product(root_client, *, stock="10", price="500", unit="pcs"):
    item = root_client.post(
        f"{STOCK}/products",
        json={
            "name": f"Матрица {uniq()}",
            "sku": f"WBP-{uniq()}",
            "cost": "100",
            "price": price,
            "unit": unit,
        },
    )
    assert item.status_code == 201, item.text
    item = item.json()
    root_client.post(
        f"{STOCK}/moves", json={"product_id": item["id"], "kind": "in", "quantity": stock}
    )
    return item


def chernovik(client, *, klient_id, tovar, quantity="3", kind="waybill_out"):
    created = client.post(WAYBILLS, json={"kind": kind, "client_id": klient_id})
    assert created.status_code == 201, created.text
    waybill = created.json()
    added = client.post(
        f"{WAYBILLS}/{waybill['id']}/lines",
        json={"product_id": tovar["id"], "quantity": quantity},
    )
    assert added.status_code == 201, added.text
    return waybill


def provedyonnaya(client, *, klient_id, tovar, quantity="3"):
    waybill = chernovik(client, klient_id=klient_id, tovar=tovar, quantity=quantity)
    posted = client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert posted.status_code == 200, posted.text
    return posted.json()


def _shapka(html: str) -> str:
    """Заголовок листа — то, что человек читает первым."""
    nayd = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    return nayd.group(1) if nayd else ""


@pytest.fixture
def klient(root_client):
    # Имя нарочно НЕ «Получатель»: это подпись столбца на бумаге, и проверка
    # «у приходной второй стороной стоит поставщик» находила слово в имени
    # клиента и зеленела на настоящей беде.
    return root_client.post(f"{API}/clients", json={"name": f"Контрагент {uniq()}"}).json()


# --- бумага выходит, и на ней то, что нужно -----------------------------------


def test_provedyonnaya_pechataetsya(root_client, klient):
    """Сторож, ничего не печатающий, зеленел бы на любой беде ниже."""
    tovar = product(root_client)
    waybill = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)

    stranitsa = root_client.get(f"{WAYBILLS}/{waybill['id']}/print")
    assert stranitsa.status_code == 200, stranitsa.text
    html = stranitsa.text
    assert waybill["number"] in html, "на бумаге нет номера накладной"
    assert tovar["name"] in html, "на бумаге нет позиции — печатать было незачем"
    assert klient["name"] in html, "на бумаге нет получателя"


def test_bumaga_nazyvaet_sebya_i_vtoruyu_storonu(root_client, klient):
    """НАЙДЕНО ЖИВОЙ ПРОБОЙ: заголовок был пуст, а вкладка называлась «№ 2026-000006».

    Ручка отдавала `title` и `party_label`, а шаблон спрашивал `t.title` и
    `t.party` — имена не совпали. Jinja на несуществующий ключ не жалуется, и
    лист выходил без единого слова о том, что это за бумага. Ни одна проверка не
    покраснела: они искали номер и название позиции, а те на месте.

    У приходной вторая сторона — ПОСТАВЩИК, а не получатель: печатать «Получатель:
    ООО Ромашка» на бумаге, по которой товар приехал ОТ Ромашки, значит печатать
    неправду.
    """
    tovar = product(root_client)
    rashodnaya = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)
    html = root_client.get(f"{WAYBILLS}/{rashodnaya['id']}/print").text
    # Смотрим в САМУ шапку, а не в лист целиком: то же слово стоит в заголовке
    # вкладки и в колонтитуле, и проверка «есть где-нибудь» зеленела на пустом
    # `h1` — проверено подрывом.
    assert "Расходная накладная" in _shapka(html), (
        "бумага не называет себя: заголовок пуст, и получателю не сказано, что "
        "он подписывает"
    )
    assert "Получатель" in html, "у расходной не названа вторая сторона"

    tovar_in = product(root_client)
    chern = chernovik(
        root_client, klient_id=klient["id"], tovar=tovar_in, kind="waybill_in"
    )
    assert root_client.post(f"{WAYBILLS}/{chern['id']}/post", json={}).status_code == 200
    prihodnaya = root_client.get(f"{WAYBILLS}/{chern['id']}/print").text
    assert "Приходная накладная" in _shapka(prihodnaya), "приходная называет себя расходной"
    assert "Поставщик" in prihodnaya, (
        "у приходной вторая сторона названа получателем — товар приехал ОТ него, "
        "а не к нему"
    )
    assert "Получатель" not in prihodnaya


def test_summy_strok_skladyvayutsya_v_itog(root_client, klient):
    """Получатель складывает столбец глазами и обязан получить «Итого».

    **Случай взят точным, а не любым.** Одна строка сходится при любом способе
    счёта, и проверка на ней зеленеет даже с округлением на каждой строке —
    проверено подрывом. Беда живёт на ДРОБНЫХ количествах в НЕСКОЛЬКИХ строках:
    по 0,5 штуки по 123.45 каждая дают 61.725 в строке; округли их по
    отдельности — и на листе встанет 61.73 + 61.73 = 123.46 под «Итого 123.45».

    Правило «округлять на итоге» менять нельзя (оно же в налогах), поэтому
    сходиться обязаны строки: `document_service.line_totals` считает их
    нарастающим итогом.
    """
    pervyy = product(root_client, price="12345", stock="10")
    vtoroy = product(root_client, price="12345", stock="10")
    waybill = chernovik(root_client, klient_id=klient["id"], tovar=pervyy, quantity="0.5")
    dobavlena = root_client.post(
        f"{WAYBILLS}/{waybill['id']}/lines",
        json={"product_id": vtoroy["id"], "quantity": "0.5"},
    )
    assert dobavlena.status_code == 201, dobavlena.text
    posted = root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert posted.status_code == 200, posted.text
    waybill = posted.json()

    html = root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text
    dengi = [int(x.replace(".", "")) for x in re.findall(r"(\d+\.\d\d)\s*[A-Z]{3}", html)]
    # Порядок на листе: цена и сумма у каждой строки, последним — «Итого».
    assert len(dengi) == 5, f"на бумаге ожидались две цены, две суммы и итог, а нашлось {dengi}"
    stroki, itogo = dengi[1:-1:2], dengi[-1]
    assert sum(stroki) == itogo, (
        f"столбец сумм даёт {sum(stroki)}, а под ним напечатано «Итого {itogo}». "
        "Получатель подписывает лист, который не сходится сам с собой"
    )
    assert itogo == waybill["total"], "итог на бумаге разошёлся с итогом накладной"


def test_edinica_izmereniya_na_bumage(root_client, klient):
    """«2» без единицы получатель не сверит с тем, что в коробке.

    Килограммы и метры меняют смысл позиции целиком, и пустая клетка в столбце
    единиц читается как недописанная строка, а не как «штуки».
    """
    tovar = product(root_client, unit="kg", stock="10")
    waybill = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)

    html = root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text
    assert "кг" in html, "единица измерения на бумагу не попала"


def test_edinica_perezhivaet_udalenie_tovara(root_client, klient):
    """Бумага печатается такой, какой её выписали, — включая столбец «Ед.».

    Единица бралась из товара живьём, а удалённых репозиторий не отдаёт: одно
    удаление опустошало клетку РАЗОМ у всех прошлых накладных с этим товаром.
    На листе оставалось «3» без единицы — штуки это, килограммы или метры,
    сказать нечем, и строка выглядит недописанной.
    """
    tovar = product(root_client, unit="kg", stock="10")
    waybill = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)
    do = root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text
    assert "кг" in do, "единица не попала на бумагу ещё до удаления"

    ubrat = root_client.delete(f"{API}/warehouse/products/{tovar['id']}")
    assert ubrat.status_code in (200, 204), ubrat.text

    posle = root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text
    assert "кг" in posle, (
        "удаление товара опустошило столбец «Ед.» на уже выписанной накладной"
    )


def test_podpisi_stoyat_po_napravleniyu(root_client, klient):
    """У приходной наш сотрудник ПРИНЯЛ товар, а не отпустил его.

    По приходной накладной товар приехал ОТ поставщика: отпустил поставщик,
    принял наш кладовщик. Имя проведшего стояло под «Отпустил» при любом виде
    бумаги — то есть лист утверждал неправду и оставлял пустой ровно ту
    строку, под которой стоит настоящая подпись.
    """
    tovar = product(root_client)
    rashod = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)
    html_out = root_client.get(f"{WAYBILLS}/{rashod['id']}/print").text
    otpustil_out = _posle(html_out, "Отпустил")
    poluchil_out = _posle(html_out, "Получил")
    assert otpustil_out, "у расходной пусто под «Отпустил»"
    assert not poluchil_out, "у расходной наше имя уехало под «Получил»"

    chern = chernovik(
        root_client,
        klient_id=klient["id"],
        tovar=tovar,
        kind="waybill_in",
    )
    provesti = root_client.post(f"{WAYBILLS}/{chern['id']}/post", json={})
    assert provesti.status_code == 200, provesti.text
    html_in = root_client.get(f"{WAYBILLS}/{chern['id']}/print").text
    assert not _posle(html_in, "Отпустил"), (
        "у приходной наше имя стоит под «Отпустил» — товар отпустил поставщик"
    )
    assert _posle(html_in, "Получил") == otpustil_out, (
        "у приходной пуста строка «Получил» — та самая, под которой подпись"
    )


def _posle(html: str, podpis: str) -> str:
    """Имя, напечатанное сразу за подписью: `Отпустил<b>Root</b>`."""
    nayd = re.search(re.escape(podpis) + r"<b>([^<]*)</b>", html)
    return (nayd.group(1) if nayd else "").strip()

def test_adres_firmy_popadaet_v_shapku(root_client, klient):
    """БЫЛО: шаблоны спрашивали `company.address`, а такого ключа нет никогда.

    В снимке лежит `legal_address`; Jinja на несуществующий ключ не жалуется и
    подставляет пустоту. Акт и заказ печатали шапку без адреса, и покраснеть
    было нечему — поэтому проверка стоит на самой бумаге, а не на словаре.
    """
    firma = root_client.post(
        f"{API}/companies",
        json={
            "name": f"Фирма {uniq()}",
            "legal_address": f"г. Киев, ул. Печатная, {uniq()}",
            "tax_number": f"TAX{uniq()}",
        },
    )
    assert firma.status_code == 201, firma.text
    firma = firma.json()
    root_client.post(f"{API}/companies/{firma['id']}/default")

    tovar = product(root_client)
    waybill = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)

    html = root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text
    assert firma["legal_address"] in html, (
        "адреса фирмы нет в шапке накладной: снова спрашиваем поле, которого в "
        "снимке не бывает"
    )
    assert firma["tax_number"] in html, "налогового номера нет в шапке"


def test_shablony_prosyat_tolko_sushchestvuyushchie_polya_firmy():
    """Разбор шаблонов: имя поля, которого снимок не отдаёт, — молчаливая дыра.

    Это сторож КЛАССА ошибок, а не одного `company.address`. Jinja на любой
    несуществующий ключ отвечает пустотой, и следующее такое поле точно так же
    не уронит ничего и точно так же выйдет на бумагу пробелом.
    """
    mozhno = {"id", "name"} | set(company_service.SNAPSHOT_FIELDS)
    plohie: list[str] = []
    for shablon in sorted(SHABLONY.glob("*_print.html")):
        # Комментарии Jinja выбрасываем: в них имя поля как раз ОБСУЖДАЕТСЯ, и
        # первый же прогон покраснел на разборе, объясняющем эту самую беду.
        tekst = re.sub(r"\{#.*?#\}", "", shablon.read_text(encoding="utf-8"), flags=re.S)
        for pole in set(re.findall(r"company\.(\w+)", tekst)):
            if pole not in mozhno:
                plohie.append(f"{shablon.name}: company.{pole}")
    assert not plohie, (
        "печатная форма спрашивает поле фирмы, которого в снимке не бывает: "
        + ", ".join(sorted(plohie))
        + ". Оно не уронит ничего и просто не напечатается"
    )


def test_mnogostranichnost_zalozhena_v_shablon():
    """Пятьдесят позиций на A4 не помещаются, и это первая форма, где так.

    Проверяем устройство, а не вид: измерить разбиение на листы нечем — оно
    случается в принтере. Но без повтора шапки второй лист приезжает столбцами
    без названий, а без запрета разрыва позиция ломается пополам и читается как
    две разные.
    """
    tekst = (SHABLONY / "waybill_print.html").read_text(encoding="utf-8")
    assert "table-header-group" in tekst, (
        "шапка таблицы не объявлена повторяющейся: на втором листе останутся "
        "голые столбцы без названий"
    )
    assert "page-break-inside: avoid" in tekst, (
        "позицию разрешено разрывать между листами — половина строки на одном "
        "листе и половина на другом читаются как две разные позиции"
    )


# --- чего на бумаге быть не должно --------------------------------------------


def test_chernovik_ne_pechataetsya(root_client, klient):
    """Напечатанный черновик — лист с подписью под перечнем, который изменят.

    Модуль целиком построен вокруг деления «до проведения — после»: черновик
    правится, проведённая нет. Бумага с номером и местом для подписи по
    правящейся записи это деление обходит.
    """
    tovar = product(root_client)
    waybill = chernovik(root_client, klient_id=klient["id"], tovar=tovar)

    otkaz = root_client.get(f"{WAYBILLS}/{waybill['id']}/print")
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "waybill_not_posted"


def test_otmenyonnaya_ne_pechataetsya(root_client, klient):
    """По отменённому черновику НИЧЕГО не происходило: товар не уезжал."""
    tovar = product(root_client)
    waybill = chernovik(root_client, klient_id=klient["id"], tovar=tovar)
    assert root_client.post(
        f"{WAYBILLS}/{waybill['id']}/cancel", json={"note": ""}
    ).status_code == 200

    otkaz = root_client.get(f"{WAYBILLS}/{waybill['id']}/print")
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "waybill_not_posted"


def test_bez_prava_na_summy_stolbtsov_deneg_net(root_client, klient):
    """Право `view_amounts` обходить печатью нельзя — ту же дыру уже находили
    у наклеек.

    Роль заводится здесь, а не берётся готовая: нужен человек ровно с
    `waybills.view` и без `waybills.view_amounts`. Обычный менеджер упёрся бы в
    403 на самой накладной, проверка пропустилась бы, и дыру не стерёг бы никто.
    """
    tovar = product(root_client, price="6173")
    waybill = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)

    vidit = root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text
    assert "61.73" in vidit, "у владельца цена на бумагу не попала вовсе"

    rol = root_client.post(
        f"{API}/roles",
        json={"name": f"Экспедитор {uniq()}", "permissions": ["waybills.view"]},
    )
    assert rol.status_code == 201, rol.text
    pochta = f"ekspeditor-{uniq()}@test.local"
    ekspeditor = make_manager(root_client, pochta)
    lyudi = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in lyudi if u["email"] == pochta)
    assert root_client.post(
        f"{API}/roles/assign/{user_id}", json={"role_id": rol.json()["id"]}
    ).status_code == 200

    try:
        bez_summ = ekspeditor.get(f"{WAYBILLS}/{waybill['id']}/print")
        assert bez_summ.status_code == 200, bez_summ.text
        assert "61.73" not in bez_summ.text, (
            "цена напечаталась тому, кому суммы не положены: право обошли печатью"
        )
        # Столбцы именно ИСЧЕЗАЮТ, а не пустеют: пустая колонка «Сумма» под
        # подписью читается как «бесплатно», а не как «вам не показано».
        assert "Итого" not in bez_summ.text, "столбец сумм остался пустым вместо того, чтобы исчезнуть"
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{API}/roles/{rol.json()['id']}")


def test_blank_ne_pechataet_nakladnuyu(root_client, klient):
    """БЫЛО: `/documents/{id}/print` печатал накладную квитанцией приёма.

    Две половины с линией отреза, поля «что приняли» пустые, перечня и сумм
    нет. У накладной есть своя форма, и общая ручка обязана отказать, а не
    выдать не ту бумагу.
    """
    tovar = product(root_client)
    waybill = provedyonnaya(root_client, klient_id=klient["id"], tovar=tovar)

    otkaz = root_client.get(f"{API}/documents/{waybill['id']}/print")
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "document_is_a_waybill"


def test_blank_ne_pechataet_zakaz(root_client, klient):
    """Тот же обход, второй бумагой: у заказа своя форма и свои суммы."""
    tovar = product(root_client)
    zakaz = root_client.post(
        f"{API}/orders",
        json={"kind": "sales_order", "client_id": klient["id"]},
    )
    assert zakaz.status_code == 201, zakaz.text

    otkaz = root_client.get(f"{API}/documents/{zakaz.json()['id']}/print")
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "document_is_an_order"


# --- имя того, кто отпустил ---------------------------------------------------


def test_otpustil_perezhivaet_uvolnenie(root_client, klient):
    """Спор «кто мне это отдал» приходит позже, чем увольнение кладовщика.

    `created_by` объявлен SET NULL — вместе со ссылкой пропало бы и имя, причём
    задним числом по всем прошлым накладным сразу. Имя берётся снимком из
    события проведения, и отдельной колонки под это нет намеренно: она стала бы
    вторым местом для факта, который уже записан переходом.
    """
    rol = root_client.post(
        f"{API}/roles",
        json={
            "name": f"Кладовщик {uniq()}",
            "permissions": [
                "waybills.view", "waybills.create", "waybills.edit", "waybills.issue",
                "waybills.view_amounts", "warehouse.view", "clients.view",
            ],
        },
    )
    assert rol.status_code == 201, rol.text
    # Имя берётся то, под которым человек зарегистрировался: ручки
    # переименования сотрудника в системе нет, а имя нужно НАСТОЯЩЕЕ — сверять
    # мы будем именно его, а не подставленное в обход.
    pochta = f"kladovshchik-{uniq()}@test.local"
    imya = pochta.split("@")[0]
    kladovshchik = make_manager(root_client, pochta)
    lyudi = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in lyudi if u["email"] == pochta)
    assert next(u for u in lyudi if u["id"] == user_id)["name"] == imya
    assert root_client.post(
        f"{API}/roles/assign/{user_id}", json={"role_id": rol.json()["id"]}
    ).status_code == 200

    tovar = product(root_client)
    waybill = provedyonnaya(kladovshchik, klient_id=klient["id"], tovar=tovar)
    assert imya in root_client.get(f"{WAYBILLS}/{waybill['id']}/print").text, (
        "имени отпустившего нет на бумаге ещё до всякого увольнения"
    )

    assert root_client.delete(f"{API}/staff/{user_id}").status_code == 200
    try:
        posle = root_client.get(f"{WAYBILLS}/{waybill['id']}/print")
        assert posle.status_code == 200, posle.text
        assert imya in posle.text, (
            "кладовщика уволили — и бумага задним числом перестала отвечать, "
            "кто отпустил товар"
        )
    finally:
        root_client.delete(f"{API}/roles/{rol.json()['id']}")
