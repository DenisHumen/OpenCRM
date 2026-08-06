"""Заказы: строки, резерв, отгрузка, приёмка, отмена.

Главное, что проверяется, — **резерв и остаток остаются разными числами**.
Списать при создании заказа значит слить «продали» и «отложили» в одно, и на
вопрос «что физически лежит на полке» ответить станет нечем. Поэтому почти
каждая проверка ниже сводится к одному: тронулся ли склад тогда, когда трогаться
не должен.
"""

import itertools

import pytest

from tests.conftest import API

ORDERS = f"{API}/orders"
STOCK = f"{API}/warehouse"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(autouse=True)
def blocks_on(root_client):
    """Заказы стоят на бланках, а склад им нужен для проведения."""
    for key in ("documents", "warehouse", "orders"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    yield


@pytest.fixture
def client_row(root_client):
    return root_client.post(f"{API}/clients", json={"name": f"Покупатель {uniq()}"}).json()


def product(root_client, stock="10", cost="100", price="500", service=False):
    item = root_client.post(
        f"{STOCK}/products",
        json={
            "name": f"Товар {uniq()}", "sku": f"ORD-{uniq()}",
            "cost": None if service else cost, "price": price, "is_service": service,
        },
    ).json()
    if stock and not service:
        root_client.post(
            f"{STOCK}/moves", json={"product_id": item["id"], "kind": "in", "quantity": stock}
        )
    return item


def order_with(root_client, client_row, item, quantity="3", kind="sales_order"):
    created = root_client.post(ORDERS, json={"kind": kind, "client_id": client_row["id"]})
    assert created.status_code == 201, created.text
    order = created.json()
    added = root_client.post(
        f"{ORDERS}/{order['id']}/lines",
        json={"product_id": item["id"], "quantity": quantity},
    )
    assert added.status_code == 201, added.text
    return order


def stock_of(root_client, product_id) -> int:
    row = root_client.get(f"{STOCK}/products/{product_id}").json()
    return row["stock_milli"]


def promises(root_client, product_id) -> dict:
    """Резерв и ожидание — через сервис: отдельной ручки у них пока нет, а
    проверять надо само число, а не то, как оно доехало до экрана."""
    from core.services import order_service
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return order_service.availability(db, [product_id])[product_id]
    finally:
        db.close()


# --- заказ и строки -----------------------------------------------------------


def test_zakaz_eto_blank_so_svoim_nomerom(root_client, client_row):
    """Номер, статусы и печать берутся у бланка — второй раз их не пишем."""
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    assert order["number"], "заказ остался без номера"
    assert order["status"] == "issued"
    assert order["kind"] == "sales_order"


def test_nazvanie_pozitsii_snimok(root_client, client_row):
    """Товар переименуют, а в заказе клиента останется то, что он заказывал."""
    item = product(root_client)
    order = order_with(root_client, client_row, item)

    root_client.patch(f"{STOCK}/products/{item['id']}", json={"name": "Совсем другое название"})

    lines = root_client.get(f"{ORDERS}/{order['id']}").json()["lines"]
    assert lines[0]["name"] == item["name"], "название поехало вслед за справочником"


def test_tsena_fiksiruetsya_pri_dobavlenii(root_client, client_row):
    """Прайс поменяли — старые заказы не трогаются, иначе сумма, названная
    клиенту вчера, сегодня станет другой."""
    # Цена в API — минорные единицы (копейки), а не рубли: деньги в проекте
    # целые от начала до конца, и «500» здесь означает пять рублей.
    item = product(root_client, price=50000)
    order = order_with(root_client, client_row, item, quantity="2")

    root_client.patch(f"{STOCK}/products/{item['id']}", json={"price": 99900})

    order = root_client.get(f"{ORDERS}/{order['id']}").json()
    assert order["lines"][0]["price"] == 50000
    assert order["total"] == 100000, "сумма заказа поехала за прайсом"


def test_razovaya_pozitsiya_bez_kartochki(root_client, client_row):
    """«Доставка» и «упаковка» бывают в каждом втором заказе, и заводить ради
    них справочник значит замусорить его одноразовыми строками."""
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    added = root_client.post(
        f"{ORDERS}/{order['id']}/lines", json={"name": "Доставка", "quantity": "1", "price": 30000}
    )
    assert added.status_code == 201, added.text
    assert added.json()["product_id"] is None


def test_stroka_bez_imeni_otvergaetsya(root_client, client_row):
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    denied = root_client.post(f"{ORDERS}/{order['id']}/lines", json={"quantity": "1"})
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "line_name_required"


# --- резерв -------------------------------------------------------------------


def test_zakaz_ne_trogaet_sklad_no_rezerviruet(root_client, client_row):
    """Самая частая ошибка блока — списать при создании заказа.

    Тогда «продали» и «отложили» становятся одним, и на вопрос «что физически
    лежит на полке» ответить нечем.
    """
    item = product(root_client, stock="10")
    order_with(root_client, client_row, item, quantity="3")

    assert stock_of(root_client, item["id"]) == 10000, "заказ тронул склад"
    numbers = promises(root_client, item["id"])
    assert numbers["reserved_milli"] == 3000
    assert numbers["available_milli"] == 7000


def test_rezerv_ne_perezhivayet_zakaz(root_client, client_row):
    """Раз резерв считается запросом, а не хранится, обещание исчезает само.

    Хранимое число дало бы вечный призрачный резерв, и найти его источник было
    бы нечем.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="4")
    assert promises(root_client, item["id"])["reserved_milli"] == 4000

    cancelled = root_client.post(f"{ORDERS}/{order['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text

    numbers = promises(root_client, item["id"])
    assert numbers["reserved_milli"] == 0
    assert numbers["available_milli"] == 10000
    assert stock_of(root_client, item["id"]) == 10000, "отмена тронула склад"


def test_zakaz_postavshchiku_daet_ozhidaetsya(root_client, client_row):
    """Зеркальное число: сколько приедет. Нужно, чтобы не заказать дважды."""
    item = product(root_client, stock="2")
    order_with(root_client, client_row, item, quantity="5", kind="purchase_order")

    numbers = promises(root_client, item["id"])
    assert numbers["expected_milli"] == 5000
    assert numbers["stock_milli"] == 2000
    assert numbers["reserved_milli"] == 0, "закупка не должна резервировать"


# --- проведение ---------------------------------------------------------------


def test_otgruzka_spisyvaet_i_snimaet_rezerv(root_client, client_row):
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")

    shipped = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["status"] == "closed"

    assert stock_of(root_client, item["id"]) == 7000
    numbers = promises(root_client, item["id"])
    assert numbers["reserved_milli"] == 0, "резерв пережил отгрузку"
    assert numbers["available_milli"] == 7000


def test_dvoynaya_otgruzka_ne_prokhodit(root_client, client_row):
    """Нажали дважды — списалось дважды.

    Защита стоит на условной смене статуса, а не на проверке «уже отгружен»:
    проверка гоняется — двое прочитали `issued`, оба списали, — а условный
    UPDATE пропускает ровно одного.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")

    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    again = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert again.status_code == 422, again.text
    assert again.json()["error"]["code"] == "order_finished"
    assert stock_of(root_client, item["id"]) == 7000, "списалось дважды"


def test_ne_khvataet_na_sklade_ostanavlivaet(root_client, client_row):
    """У ручного движения минус разрешён, у отгрузки — нет: отгрузить нечего.

    В отказе названы позиции поимённо: «не хватает товара» без списка отправляет
    человека сверять заказ со складом построчно руками.
    """
    item = product(root_client, stock="2")
    order = order_with(root_client, client_row, item, quantity="5")

    denied = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "not_enough_stock"
    assert item["name"] in denied.json()["error"]["message"]
    assert stock_of(root_client, item["id"]) == 2000

    forced = root_client.post(f"{ORDERS}/{order['id']}/close", json={"confirm_negative": True})
    assert forced.status_code == 200, forced.text
    assert stock_of(root_client, item["id"]) == -3000


def test_priyomka_ot_postavshchika_prikhoduet(root_client, client_row):
    item = product(root_client, stock="1")
    order = order_with(root_client, client_row, item, quantity="6", kind="purchase_order")

    received = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert received.status_code == 200, received.text

    assert stock_of(root_client, item["id"]) == 7000
    assert promises(root_client, item["id"])["expected_milli"] == 0, "ожидание пережило приёмку"


def test_pustoy_zakaz_ne_provoditsya(root_client, client_row):
    """Провести пустой значит закрыть его, ничего не сделав, — и обнаружить это,
    когда клиент придёт за товаром."""
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    denied = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "order_is_empty"


def test_usluga_v_zakaze_sklada_ne_kasaetsya(root_client, client_row):
    """Услуга в заказе законна (доставка, настройка), но остатка у неё нет."""
    service = product(root_client, service=True, stock=None)
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    root_client.post(
        f"{ORDERS}/{order['id']}/lines", json={"product_id": service["id"], "quantity": "1"}
    )

    shipped = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert shipped.status_code == 200, shipped.text
    moves = root_client.get(f"{STOCK}/products/{service['id']}/moves").json()
    assert moves["total"] == 0, "по услуге появилось движение склада"


# --- отмена проведения --------------------------------------------------------


def test_otmena_provedeniya_obratnymi_dvizheniyami(root_client, client_row):
    """Сотри мы движения — остаток сошёлся бы, а «куда делись пять матриц»
    осталось бы без ответа: ошибка стала бы невидимой."""
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="4")
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert stock_of(root_client, item["id"]) == 6000

    reverted = root_client.post(f"{ORDERS}/{order['id']}/revert")
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["status"] == "cancelled"

    assert stock_of(root_client, item["id"]) == 10000
    moves = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    kinds = [m["kind"] for m in moves if m["document_id"] == order["id"]]
    assert "out" in kinds and "return" in kinds, "прежние движения стёрты"


def test_neprovedennyy_zakaz_otmenit_provedenie_nelzya(root_client, client_row):
    item = product(root_client, stock="5")
    order = order_with(root_client, client_row, item, quantity="1")
    denied = root_client.post(f"{ORDERS}/{order['id']}/revert")
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "order_not_closed"


# --- сборка сканером ----------------------------------------------------------


def test_sborka_skanerom_schitaet_sobrannoe_otdelno(root_client, client_row):
    """Расхождение «заказано пять, собрано четыре» видно построчно ДО отгрузки,
    а не на выдаче — для этого собранное и живёт отдельно от заказанного."""
    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    item = product(root_client, stock="10")
    code = f"46{uniq()}0"
    root_client.post(f"{API}/labels/products/{item['id']}/barcodes", json={"code": code})
    order = order_with(root_client, client_row, item, quantity="3")

    for _ in range(2):
        picked = root_client.post(f"{ORDERS}/{order['id']}/pick", json={"code": code})
        assert picked.status_code == 200, picked.text

    line = root_client.get(f"{ORDERS}/{order['id']}").json()["lines"][0]
    assert line["quantity_milli"] == 3000
    assert line["picked_milli"] == 2000, "собранное не считается"


def test_chuzhoy_kod_v_zakaze_nazvan_v_otkaze(root_client, client_row):
    """Пустой ответ после писка сканера читается как «сканер сломался»."""
    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    inside = product(root_client, stock="5")
    outside = product(root_client, stock="5")
    stranger = f"46{uniq()}1"
    root_client.post(f"{API}/labels/products/{outside['id']}/barcodes", json={"code": stranger})
    order = order_with(root_client, client_row, inside, quantity="1")

    denied = root_client.post(f"{ORDERS}/{order['id']}/pick", json={"code": stranger})
    assert denied.status_code == 404, denied.text
    assert denied.json()["error"]["code"] == "product_not_in_order"
    assert outside["name"] in denied.json()["error"]["message"]


# --- блоки включаются и выключаются -------------------------------------------


def test_bez_sklada_zakaz_rabotaet(root_client, client_row):
    """Заказ на услуги складу не нужен, и связь между блоками мягкая.

    Объявить её жёсткой значило бы запретить заказы тому, у кого склада нет.
    """
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    root_client.post(
        f"{ORDERS}/{order['id']}/lines", json={"name": "Консультация", "quantity": "1", "price": 100000}
    )
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        listed = root_client.get(ORDERS)
        assert listed.status_code == 200, listed.text
        card = root_client.get(f"{ORDERS}/{order['id']}")
        assert card.status_code == 200
        assert card.json()["total"] == 100000
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})


def test_vyklyuchennyy_blok_ne_stiraet_dvizheniya(root_client, client_row):
    """Выключенный блок прячет раздел, а не стирает историю склада.

    Резерв при этом перестаёт считаться — и «доступно» снова равно остатку.
    Это правильно: раздела нет, обещаний тоже.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="2")
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})

    root_client.post(f"{API}/modules/orders", json={"enabled": False})
    try:
        assert root_client.get(ORDERS).status_code == 403
        assert stock_of(root_client, item["id"]) == 8000, "движения пропали вместе с блоком"
        numbers = promises(root_client, item["id"])
        assert numbers["reserved_milli"] == 0
        assert numbers["available_milli"] == numbers["stock_milli"]
    finally:
        root_client.post(f"{API}/modules/orders", json={"enabled": True})


def test_provesti_zakaz_pravo_otdelnoye(manager_client, root_client, client_row):
    """Набирать позиции и двигать склад — разные полномочия: сборщик набирает,
    отгружает старший."""
    item = product(root_client, stock="5")
    order = order_with(root_client, client_row, item, quantity="1")

    denied = manager_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert denied.status_code == 403, denied.text
    assert stock_of(root_client, item["id"]) == 5000


def test_zakaz_bez_klienta_sozdayotsya(root_client):
    """Заказ без клиента законен, и это не поблажка.

    У заказа поставщику клиента нет по устройству. У заказа покупателя он
    бывает не сразу: у стойки сначала набивают позиции, а карточку заводят,
    когда покупатель назвал телефон, — и часто не заводят вовсе.

    Без подстановки имени не создавался НИ ОДИН заказ: система отвечала
    «укажите клиента» на запрос, где клиента может не быть по существу. Поймано
    живым прогоном на стенде дважды — второй раз кнопкой на экране, которая
    создаёт ровно такой запрос.
    """
    for kind in ("purchase_order", "sales_order"):
        created = root_client.post(ORDERS, json={"kind": kind})
        assert created.status_code == 201, f"{kind}: {created.text}"
        assert created.json()["client_id"] is None

    named = root_client.post(
        ORDERS, json={"kind": "purchase_order", "client_name": "ООО «Поставщик»"}
    )
    assert named.status_code == 201, named.text


def test_pechat_zakaza_daet_tablitsu_pozitsiy(root_client, client_row):
    """Форма отличается от квитанции по существу, а не оформлением.

    Квитанция отвечает на вопрос «что вы у меня взяли», заказ — «что и почём мне
    отдадут»: значит таблица с количествами и суммами, а не описание одной вещи.
    """
    item = product(root_client, stock="10", price=50000)
    order = order_with(root_client, client_row, item, quantity="2")
    root_client.post(
        f"{ORDERS}/{order['id']}/lines", json={"name": "Доставка", "quantity": "1", "price": 30000}
    )

    page = root_client.get(f"{ORDERS}/{order['id']}/print")
    assert page.status_code == 200, page.text
    assert order["number"] in page.text
    assert item["name"] in page.text
    assert "Доставка" in page.text
    # Итог считается сервером и печатается один раз: 2 × 500 + 300 = 1300.
    assert "1300.00" in page.text
    # Номер уходит в штрихкод: заказ находят сканером так же, как квитанцию.
    assert "<svg" in page.text


def test_sobran_otdelnyy_shag(root_client, client_row):
    """Между «принят» и «отгружен» есть состояние, которое видит сборщик."""
    item = product(root_client, stock="5")
    order = order_with(root_client, client_row, item, quantity="1")

    ready = root_client.post(f"{ORDERS}/{order['id']}/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"
    # Резерв держится: товар ещё наш, отгрузки не было.
    assert promises(root_client, item["id"])["reserved_milli"] == 1000

    again = root_client.post(f"{ORDERS}/{order['id']}/ready")
    assert again.status_code == 422, "собранный заказ собрали второй раз"
