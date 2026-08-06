"""Штрихкоды товара: несколько кодов, свой код, сканирование.

Блок существует ради одного — опознать товар однозначно и за долю секунды.
Поэтому и проверки здесь про однозначность: один код не может означать два
товара, опечатка в наборанном руками коде обязана отвергаться, а не подставлять
соседнюю позицию.
"""

import itertools

import pytest

from core.services import barcode_service
from tests.conftest import API

LABELS = f"{API}/labels"

# База одна на весь набор, а код штрихкода уникален по всей таблице — значит
# два теста с одинаковым числом в тексте столкнулись бы, и падал бы не тот,
# кто виноват, а тот, кто пришёл вторым.
_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):06d}"


@pytest.fixture(autouse=True)
def labels_on(root_client):
    """Блок по умолчанию выключен — включаем на время проверок и гасим за собой.

    Гасим обязательно: набор гоняется в обоих порядках, и включённый блок,
    оставленный соседу, менял бы его условия.
    """
    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    yield
    root_client.post(f"{API}/modules/labels", json={"enabled": False})


@pytest.fixture
def product(root_client):
    created = root_client.post(
        f"{API}/warehouse/products",
        json={"name": "Матрица для наклеек", "sku": f"LBL-{uniq()}", "price_minor": 250000},
    )
    assert created.status_code == 201, created.text
    return created.json()


# --- контрольный разряд ------------------------------------------------------


def test_opechatka_v_nabrannom_kode_otvergaetsya():
    """Код набирают руками, когда наклейка затёрлась или сканер не берёт.

    Одна перепутанная цифра без проверки означает **чужой товар в заказе** —
    молча и правдоподобно. С контрольным разрядом такая опечатка отвергается.
    """
    body = "2000012"
    good = body + barcode_service.check_digit(body)
    assert barcode_service.is_valid_internal(good)

    for position in range(len(body)):
        broken = list(good)
        broken[position] = str((int(broken[position]) + 1) % 10)
        assert not barcode_service.is_valid_internal("".join(broken)), (
            f"опечатка в разряде {position} прошла проверку"
        )


def test_probely_i_defisy_ne_delayut_iz_odnogo_koda_dva():
    """Сканеры и люди пишут код по-разному, а в базе он один."""
    assert barcode_service.clean_code(" 46-01 234 ") == "4601234"
    assert barcode_service.clean_code("4601234") == "4601234"


# --- привязка кодов ----------------------------------------------------------


def test_u_tovara_neskolko_kodov(root_client, product):
    """Штука, блок и коробка — три кода у одного товара, и все три законны."""
    shtuka, blok, korobka = f"46{uniq()}", f"46{uniq()}", f"46{uniq()}"
    for code, pack in ((shtuka, 1000), (blok, 10000), (korobka, 100000)):
        added = root_client.post(
            f"{LABELS}/products/{product['id']}/barcodes",
            json={"code": code, "pack_size_milli": pack},
        )
        assert added.status_code == 201, added.text

    listed = root_client.get(f"{LABELS}/products/{product['id']}/barcodes")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 3
    assert [i["pack_size_milli"] for i in items if i["code"] == blok] == [10000]


def test_odin_kod_ne_mozhet_oznachat_dva_tovara(root_client, product):
    """Иначе сканирование перестаёт быть однозначным — а это весь его смысл."""
    shared = f"46{uniq()}"
    first = root_client.post(
        f"{LABELS}/products/{product['id']}/barcodes", json={"code": shared}
    )
    assert first.status_code == 201

    other = root_client.post(
        f"{API}/warehouse/products", json={"name": "Другой товар", "sku": f"LBL-{uniq()}"}
    ).json()
    clash = root_client.post(
        f"{LABELS}/products/{other['id']}/barcodes", json={"code": shared}
    )
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "barcode_taken"


def test_pervyy_kod_stanovitsya_osnovnym_sam(root_client, product):
    """Иначе печать упёрлась бы в «основной не выбран» там, где выбирать не из чего."""
    added = root_client.post(
        f"{LABELS}/products/{product['id']}/barcodes", json={"code": f"46{uniq()}"}
    ).json()
    assert added["is_primary"] is True

    second = root_client.post(
        f"{LABELS}/products/{product['id']}/barcodes", json={"code": f"46{uniq()}"}
    ).json()
    assert second["is_primary"] is False


def test_osnovnoy_vsegda_odin(root_client, product):
    first_code, second_code = f"46{uniq()}", f"46{uniq()}"
    for code in (first_code, second_code):
        root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": code})
    items = root_client.get(f"{LABELS}/products/{product['id']}/barcodes").json()["items"]
    second = next(i for i in items if i["code"] == second_code)

    root_client.post(f"{LABELS}/products/{product['id']}/barcodes/{second['id']}/primary")

    items = root_client.get(f"{LABELS}/products/{product['id']}/barcodes").json()["items"]
    assert [i["code"] for i in items if i["is_primary"]] == [second_code]


def test_snyali_osnovnoy_osnovnym_stanovitsya_ostavshiysya(root_client, product):
    """Без этого печатать наклейку станет нечем, а человек об этом не узнает."""
    for code in (f"46{uniq()}", f"46{uniq()}"):
        root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": code})
    items = root_client.get(f"{LABELS}/products/{product['id']}/barcodes").json()["items"]
    primary = next(i for i in items if i["is_primary"])

    dropped = root_client.delete(f"{LABELS}/products/{product['id']}/barcodes/{primary['id']}")
    assert dropped.status_code == 200, dropped.text

    left = root_client.get(f"{LABELS}/products/{product['id']}/barcodes").json()["items"]
    assert len(left) == 1
    assert left[0]["is_primary"] is True


def test_chuzhoy_kod_po_nomeru_ne_udalyaetsya(root_client, product):
    """Номер кода в адресе — не пропуск к чужому товару."""
    mine = root_client.post(
        f"{LABELS}/products/{product['id']}/barcodes", json={"code": f"46{uniq()}"}
    ).json()
    other = root_client.post(
        f"{API}/warehouse/products", json={"name": "Чужой", "sku": f"LBL-{uniq()}"}
    ).json()

    denied = root_client.delete(f"{LABELS}/products/{other['id']}/barcodes/{mine['id']}")
    assert denied.status_code == 404, denied.text


# --- свой код ----------------------------------------------------------------


def test_svoy_kod_vydayotsya_odin_raz(root_client, product):
    """У собственного изделия заводского кода нет — свой обязан быть."""
    issued = root_client.post(f"{LABELS}/products/{product['id']}/barcodes/internal")
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]
    assert barcode_service.is_valid_internal(code), code

    again = root_client.post(f"{LABELS}/products/{product['id']}/barcodes/internal")
    assert again.json()["code"] == code, "выдали второй свой код тому же товару"


def test_svoi_kody_ne_povtoryayutsya(root_client):
    """Товары удаляют, а код остаётся занятым: на коробках уже наклеены этикетки."""
    codes = []
    for number in range(3):
        item = root_client.post(
            f"{API}/warehouse/products", json={"name": f"Своё изделие {number}"}
        ).json()
        codes.append(
            root_client.post(f"{LABELS}/products/{item['id']}/barcodes/internal").json()["code"]
        )
    assert len(set(codes)) == 3, codes


# --- сканирование ------------------------------------------------------------


def test_skaner_nakhodit_tovar(root_client, product):
    code = f"46{uniq()}"
    root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": code})
    found = root_client.get(f"{LABELS}/scan/{code}")
    assert found.status_code == 200, found.text
    assert found.json()["id"] == product["id"]


def test_neizvestnyy_kod_nazyvaetsya_v_otkaze(root_client):
    """Пустой ответ после писка сканера читается как «сканер сломался».

    В отказе должен стоять сам код, чтобы экран сказал «код такой-то не найден»
    и предложил завести товар прямо отсюда.
    """
    denied = root_client.get(f"{LABELS}/scan/4600000000")
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "barcode_unknown"
    assert "4600000000" in denied.json()["error"]["message"]


def test_udalyonnyy_tovar_skanerom_ne_nakhoditsya(root_client):
    """Иначе сканер подставил бы позицию, которой в справочнике нет."""
    item = root_client.post(
        f"{API}/warehouse/products", json={"name": "Уйдёт в корзину", "sku": f"LBL-{uniq()}"}
    ).json()
    gone = f"46{uniq()}"
    root_client.post(f"{LABELS}/products/{item['id']}/barcodes", json={"code": gone})
    root_client.delete(f"{API}/warehouse/products/{item['id']}")

    denied = root_client.get(f"{LABELS}/scan/{gone}")
    assert denied.status_code == 404


# --- блок выключается ---------------------------------------------------------


def test_vyklyuchennyy_blok_ne_stiraet_kody(root_client, product):
    """Коды — свойство товара, а не наклейки.

    Выключили печать — не значит отказались от кодов. Ровно как выключенные
    бланки не стирают уже выданные бумаги.
    """
    kept = f"46{uniq()}"
    root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": kept})

    root_client.post(f"{API}/modules/labels", json={"enabled": False})
    assert root_client.get(f"{LABELS}/products/{product['id']}/barcodes").status_code == 403

    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    items = root_client.get(f"{LABELS}/products/{product['id']}/barcodes").json()["items"]
    assert [i["code"] for i in items] == [kept]


def test_sklad_uvodit_naklejki_za_soboy(root_client):
    """Наклейка на товар без товаров бессмысленна — гаснет вместе со складом."""
    switched = root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    assert switched.status_code == 200, switched.text
    state = {item["key"]: item["enabled"] for item in switched.json()["items"]}
    assert state["warehouse"] is False
    assert state["labels"] is False, "наклейки остались включёнными без склада"

    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})


# --- печать ------------------------------------------------------------------


def test_pachka_pechataetsya_v_prislannom_poryadke(root_client):
    """Человек выделил строки на экране и ждёт их в том же виде.

    Отсортировать по-своему значит заставить его искать, где что, уже на
    отпечатанной ленте — а наклейки с ленты снимают по одной.
    """
    codes = []
    ids = []
    for number in range(3):
        item = root_client.post(
            f"{API}/warehouse/products",
            json={"name": f"Позиция {number}", "sku": f"LBL-{uniq()}"},
        ).json()
        ids.append(item["id"])
        code = f"46{uniq()}"
        codes.append(code)
        root_client.post(f"{LABELS}/products/{item['id']}/barcodes", json={"code": code})

    order = [ids[2], ids[0], ids[1]]
    page = root_client.get(f"{LABELS}/print", params={"product_id": order})
    assert page.status_code == 200, page.text

    # Порядок — по тому, где каждый код встретился в странице: коды уникальны,
    # поэтому позиция в тексте и есть позиция наклейки в ленте.
    positions = [page.text.index(codes[i]) for i in (2, 0, 1)]
    assert positions == sorted(positions), "наклейки вышли не в том порядке"


def test_kopiy_stolko_skolko_poprosili(root_client, product):
    """Пришло десять коробок — печатаем десять одинаковых наклеек."""
    code = f"46{uniq()}"
    root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": code})

    page = root_client.get(
        f"{LABELS}/print", params={"product_id": [product["id"]], "copies": 4}
    )
    assert page.status_code == 200
    assert page.text.count('class="label"') == 4


def test_tovar_bez_koda_ne_propadaet_iz_pachki(root_client):
    """Молча пропустить его значит отдать пачку, в которой на одну наклейку меньше.

    Обнаружится это на коробках, когда печатать будет уже поздно.
    """
    without = root_client.post(
        f"{API}/warehouse/products", json={"name": "Без кода", "sku": f"LBL-{uniq()}"}
    ).json()
    page = root_client.get(f"{LABELS}/print", params={"product_id": [without["id"]]})

    assert page.status_code == 200
    assert page.text.count('class="label"') == 1
    assert "код не задан" in page.text


def test_pustaya_pachka_eto_otkaz_a_ne_pustoy_list(root_client):
    """Нажал «печать», ничего не выделив, — надо сказать об этом, а не молчать."""
    denied = root_client.get(f"{LABELS}/print")
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "nothing_to_print"


def test_razmer_rulona_uezzhaet_v_stranitsu(root_client, product):
    """Рулоны бывают 58×40, 40×30, 30×20 — один зашитый размер печатал бы мимо."""
    root_client.patch(
        f"{API}/settings", json={"values": {"label_width_mm": "40", "label_height_mm": "30"}}
    )
    page = root_client.get(f"{LABELS}/print", params={"product_id": [product["id"]]})
    assert "size: 40mm 30mm" in page.text

    root_client.patch(
        f"{API}/settings", json={"values": {"label_width_mm": "58", "label_height_mm": "40"}}
    )


def test_musor_v_nastroykakh_ne_lomaet_pechat(root_client, product):
    """Размер уезжает внутрь тега style, а Jinja экранирует HTML, но не CSS.

    Строка `58mm } body {` закрыла бы правило и дописала своё. Настройки правит
    только root, так что наружу это не дыра, — но одно неверное значение ломало
    бы печать у всех, и виновника в отпечатанной ленте было бы не видно.
    """
    root_client.patch(
        f"{API}/settings",
        json={"values": {"label_width_mm": "58mm } body { display:none", "label_height_mm": ""}},
    )
    page = root_client.get(f"{LABELS}/print", params={"product_id": [product["id"]]})

    assert page.status_code == 200
    assert "display:none" not in page.text
    assert "size: 58mm 40mm" in page.text, "размер не откатился к умолчанию"

    root_client.patch(
        f"{API}/settings", json={"values": {"label_width_mm": "58", "label_height_mm": "40"}}
    )


def test_udalyonnyy_tovar_ne_pechataetsya(root_client):
    """Наклейка на то, чего нет в справочнике, приклеится к коробке навсегда."""
    item = root_client.post(
        f"{API}/warehouse/products", json={"name": "Уйдёт в корзину", "sku": f"LBL-{uniq()}"}
    ).json()
    root_client.post(f"{LABELS}/products/{item['id']}/barcodes", json={"code": f"46{uniq()}"})
    root_client.delete(f"{API}/warehouse/products/{item['id']}")

    page = root_client.get(f"{LABELS}/print", params={"product_id": [item["id"]]})
    assert page.status_code == 200
    assert page.text.count('class="label"') == 0


def test_vyklyuchennyy_blok_ne_pechataet(root_client, product):
    root_client.post(f"{API}/modules/labels", json={"enabled": False})
    denied = root_client.get(f"{LABELS}/print", params={"product_id": [product["id"]]})
    assert denied.status_code == 403


def test_naklejka_szhimaetsya_pod_svoy_rulon(root_client, product):
    """Геометрия считается от высоты рулона, а не задана числом.

    Зашитая высота полосок разумна ровно для одного размера. На 58×40 всё
    помещалось, а на 30×20 подпись с цифрами уезжала за край и **молча
    обрезалась** `overflow: hidden`: штрихкод печатался, номер под ним
    пропадал, и увидеть это можно было только на отпечатанной ленте.

    Замеры в живом браузере после правки: на 58×40 цифры кончаются на 38,5 мм,
    на 30×20 — на 18,5 мм, то есть внутри наклейки в обоих случаях.
    """
    root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": f"46{uniq()}"})

    for height, bar_mm, lines in ((40, "14.0mm", "2"), (30, "10.5mm", "2"), (20, "7.0mm", "1")):
        root_client.patch(
            f"{API}/settings",
            json={"values": {"label_width_mm": "58", "label_height_mm": str(height)}},
        )
        page = root_client.get(f"{LABELS}/print", params={"product_id": [product["id"]]}).text
        assert f"height: {bar_mm}" in page, f"на рулоне {height} мм полоски не подстроились"
        assert f"line-clamp: {lines}" in page, f"на рулоне {height} мм название не ужалось"

    root_client.patch(
        f"{API}/settings", json={"values": {"label_width_mm": "58", "label_height_mm": "40"}}
    )


def test_v_rezhime_prosmotra_pechat_ne_zapuskaetsya(root_client, product):
    """Подбирая размер под свой рулон, на страницу надо смотреть.

    Без этого каждая примерка стоит намотанной ленты: термобумага не
    переиспользуется, а диалог принтера открывается сам.
    """
    root_client.post(f"{LABELS}/products/{product['id']}/barcodes", json={"code": f"46{uniq()}"})
    args = {"product_id": [product["id"]]}

    printing = root_client.get(f"{LABELS}/print", params=args).text
    assert "window.print()" in printing

    watching = root_client.get(f"{LABELS}/print", params={**args, "preview": 1}).text
    assert "addEventListener" not in watching, "режим просмотра всё равно печатает"
    # Кнопка остаётся: посмотрели, размер подошёл — печатаем отсюда же.
    assert "window.print()" in watching
