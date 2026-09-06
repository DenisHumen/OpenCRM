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
    """Заказы стоят на бланках, а склад им нужен для проведения.

    Накладные включаются ЯВНО, а не достаются в наследство от соседнего
    файла. Состояние блока живёт в базе и одно на весь прогон: пока его тут
    не закрепили, исход проверок зависел от того, кто отработал раньше.
    Поймано CI — он гоняет набор дважды, второй раз в обратном порядке
    файлов, и в обратном проверка отмены падала, а в прямом проходила.
    """
    for key in ("documents", "warehouse", "orders", "waybills"):
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
    from core.services import order_service, reserve_service
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return reserve_service.availability(db, [product_id])[product_id]
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


def test_provedyonnyy_zakaz_nazad_ne_otkatyvaetsya(root_client, client_row):
    """Отмены проведения нет (владелец, 05.09.2026): проведённый заказ — свершившееся,
    назад — только возвратом (`tests/test_vozvraty.py`)."""
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="4")
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert stock_of(root_client, item["id"]) == 6000

    otkaz = root_client.post(f"{ORDERS}/{order['id']}/revert", json={})
    # Адреса нет: 404, либо 405 от раздачи интерфейса, которая знает только GET.
    assert otkaz.status_code in (404, 405), otkaz.text
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["status"] == "closed"
    assert stock_of(root_client, item["id"]) == 6000


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


def test_sborka_po_artikulu_bez_shtrihkoda(root_client, client_row):
    """Артикул печатается на наклейке текстом: у товара без своего штрихкода
    это единственное, что можно набрать с коробки."""
    item = product(root_client, stock="3")
    order = order_with(root_client, client_row, item, quantity="2")

    picked = root_client.post(f"{ORDERS}/{order['id']}/pick", json={"code": item["sku"].lower()})
    assert picked.status_code == 200, picked.text
    line = root_client.get(f"{ORDERS}/{order['id']}").json()["lines"][0]
    assert line["picked_milli"] == 1000

    denied = root_client.post(f"{ORDERS}/{order['id']}/pick", json={"code": "NO-SUCH-SKU"})
    assert denied.status_code == 404, denied.text
    assert denied.json()["error"]["code"] == "barcode_unknown"


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


def test_dve_stroki_odnogo_tovara_skladyvayutsya_pri_proverke(root_client, client_row):
    """НАЙДЕНО РАЗБОРОМ: остаток уходил в минус мимо подтверждения.

    Один товар в заказе встречается дважды запросто: две цены, два комментария,
    два исполнителя. Пока каждая строка сверялась с остатком поодиночке, две
    строки по 5 при остатке 6 проходили обе — а списать предстояло 10.
    Подтверждение, которое здесь для того и стоит («увести склад в минус можно,
    но с ведома человека»), не спрашивалось вовсе.
    """
    item = product(root_client, stock="6")
    order = order_with(root_client, client_row, item, quantity="5")
    dobavka = root_client.post(
        f"{ORDERS}/{order['id']}/lines",
        json={"product_id": item["id"], "quantity": "5"},
    )
    assert dobavka.status_code == 201, dobavka.text

    otkaz = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert otkaz.status_code == 422, (
        f"списали 10 при остатке 6 не спросив: {otkaz.status_code} {otkaz.text}"
    )
    assert otkaz.json()["error"]["code"] == "not_enough_stock"
    # Названа ОБЩАЯ потребность, а не половина: иначе человек сверяет и не
    # понимает, почему 6 меньше 5.
    assert "10" in otkaz.json()["error"]["message"], otkaz.json()["error"]["message"]
    assert stock_of(root_client, item["id"]) == 6000

    # С ведома человека — по-прежнему можно.
    forced = root_client.post(f"{ORDERS}/{order['id']}/close", json={"confirm_negative": True})
    assert forced.status_code == 200, forced.text
    assert stock_of(root_client, item["id"]) == -4000


def test_dve_stroki_odnogo_tovara_v_predelakh_ostatka_prokhodyat(root_client, client_row):
    """И наоборот: сумма в пределах остатка отказывать не должна.

    Иначе исправление превратилось бы в запрет повторять товар в заказе.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="4")
    assert root_client.post(
        f"{ORDERS}/{order['id']}/lines",
        json={"product_id": item["id"], "quantity": "4"},
    ).status_code == 201

    zakryt = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert zakryt.status_code == 200, zakryt.text
    assert stock_of(root_client, item["id"]) == 2000


# --- переезд закрытия на накладную --------------------------------------------
#
# К остатку теперь ведёт ОДИН путь: заказ не двигает склад сам, он выписывает
# накладную и проводит её. Прежде путей было два, и держались они друг от друга
# взаимным запретом в коде, а не устройством.


def waybills_on(root_client, enabled: bool = True):
    otvet = root_client.post(f"{API}/modules/waybills", json={"enabled": enabled})
    assert otvet.status_code == 200, otvet.text


def test_zakrytie_vypisyvaet_nakladnuyu_i_sklad_dvigaetsya_odin_raz(root_client, client_row):
    """После закрытия существует бумага, а товар ушёл ровно один раз.

    Главное здесь — второе. Переезд ошибиться может в обе стороны: не двинуть
    склад вовсе (тогда бумага есть, а товара на полке лишнего) или двинуть
    дважды (заказ сам плюс накладная). Первое видно сразу, второе — только по
    числу, поэтому остаток проверяется точным значением, а не «уменьшился».
    """
    waybills_on(root_client)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")

    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    assert stock_of(root_client, item["id"]) == 7_000, "склад двинулся не один раз"

    nakladnye = root_client.get(f"{API}/waybills?basis_id={order['id']}").json()["items"]
    assert len(nakladnye) == 1, f"по закрытому заказу нет ровно одной накладной: {nakladnye}"
    assert nakladnye[0]["status"] == "issued", "накладная осталась черновиком"
    assert nakladnye[0]["kind"] == "waybill_out"


def test_zakaz_postavshchiku_zakryvaetsya_prikhodnoy(root_client, client_row):
    """Приёмка обязана прибавить, а не списать.

    Вид накладной берётся у заказа. Ошибись здесь — и остаток сойдётся сам с
    собой, а расхождение с полкой всплывёт на инвентаризации.
    """
    waybills_on(root_client)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="4", kind="purchase_order")

    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    assert stock_of(root_client, item["id"]) == 14_000, "приёмка списала товар"

    nakladnaya = root_client.get(f"{API}/waybills?basis_id={order['id']}").json()["items"][0]
    assert nakladnaya["kind"] == "waybill_in"


def test_vozvrat_vozvrashchaet_tovar_i_ostavlyaet_bumagu(root_client, client_row):
    """Возврат возвращает товар — и оставляет бумагу о возврате: приходную по нему.

    Проверяется и то и другое: остаток вернулся, и приходная существует. Одного
    остатка мало — вернуть можно и голыми движениями, а тогда на вопрос «по
    какой бумаге вернули» ответить нечем.
    """
    waybills_on(root_client)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert stock_of(root_client, item["id"]) == 7_000

    vozvrat = root_client.post(f"{ORDERS}/{order['id']}/returns").json()
    otvet = root_client.post(f"{API}/returns/{vozvrat['id']}/post", json={})
    assert otvet.status_code == 200, otvet.text
    assert stock_of(root_client, item["id"]) == 10_000, "возврат не вернул товар"

    bumagi = root_client.get(f"{API}/waybills?basis_id={order['id']}").json()["items"]
    assert len(bumagi) == 1, "по заказу должна остаться одна исходная накладная"
    prihod = root_client.get(f"{API}/waybills?basis_id={vozvrat['id']}").json()["items"]
    assert len(prihod) == 1, "возврат прошёл без бумаги — приходная не выписана"
    assert prihod[0]["status"] == "issued", "приходная осталась черновиком, товар вернулся мимо неё"
    assert prihod[0]["kind"] == "waybill_in"


def test_bez_bloka_nakladnykh_zakaz_rabotaet_po_prezhnemu(root_client, client_row):
    """Выключенный блок исчезает целиком — и не мешает заказам работать.

    Блок, которого нет, не может выписать бумагу. Заказ в этом случае двигает
    склад сам, как делал всегда; двойной отгрузки здесь быть не может, потому
    что накладной по этому заказу не существует и завести её нечем.
    """
    waybills_on(root_client, False)
    try:
        item = product(root_client, stock="10")
        order = order_with(root_client, client_row, item, quantity="2")
        assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
        assert stock_of(root_client, item["id"]) == 8_000, "без накладных склад не двинулся"

        # И возврат по-прежнему возвращает — голыми движениями: бумаги склада нет.
        vozvrat = root_client.post(f"{ORDERS}/{order['id']}/returns").json()
        assert root_client.post(f"{API}/returns/{vozvrat['id']}/post", json={}).status_code == 200
        assert stock_of(root_client, item["id"]) == 10_000
    finally:
        waybills_on(root_client)


def test_staryy_zakaz_bez_nakladnoy_vozvrashchaetsya_prikhodnoy(root_client, client_row):
    """Заказы, закрытые ДО переезда, накладных не имеют — и возвращаться обязаны.

    Задним числом бумага им не выписывается: накладная с сегодняшним номером и
    вчерашней датой это подделка. Возврат же выписывает СВОЮ приходную сегодняшним
    числом — это честно: товар приехал сегодня. Проверяется единственным способом,
    каким такой заказ можно завести сегодня, — закрытием при выключенном блоке накладных.
    """
    waybills_on(root_client, False)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="5")
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert stock_of(root_client, item["id"]) == 5_000

    # Блок включили обратно — заказ от этого накладной не обзавёлся.
    waybills_on(root_client)
    assert root_client.get(f"{API}/waybills?basis_id={order['id']}").json()["items"] == []

    vozvrat = root_client.post(f"{ORDERS}/{order['id']}/returns").json()
    assert root_client.post(f"{API}/returns/{vozvrat['id']}/post", json={}).status_code == 200
    assert stock_of(root_client, item["id"]) == 10_000, "старый заказ не вернул товар"
    prihod = root_client.get(f"{API}/waybills?basis_id={vozvrat['id']}").json()["items"]
    assert [w["kind"] for w in prihod] == ["waybill_in"]


def test_pri_vyklyuchennom_sklade_zakaz_ne_pishet_dvizheniy(root_client, client_row):
    """Третья беда прямого вызова: он шёл мимо проверки блока склада.

    Прежде `close` звал склад напрямую, без `is_enabled`, и при ВЫКЛЮЧЕННОМ
    складе закрытие заказа всё равно писало движения — то есть выключенный блок
    продолжал работать. Накладная делает то же самое событием и потому ведёт
    себя правильно.
    """
    waybills_on(root_client)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        otvet = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
        assert otvet.status_code == 200, otvet.text
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})

    assert stock_of(root_client, item["id"]) == 10_000, (
        "закрытие заказа двинуло склад при выключенном блоке склада"
    )


def test_bez_nakladnykh_pri_vyklyuchennom_sklade_zakaz_ne_pishet_dvizheniy(
    root_client, client_row
):
    """Та же беда на второй половине развилки — там, где накладных нет.

    Развилка в `close` сделана по блоку НАКЛАДНЫХ, и проверку блока склада
    унаследовала только одна её половина. «Заказы включены, накладные
    выключены, склад выключен» — состояние законное: и заказам, и накладным
    нужны только бланки.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")
    waybills_on(root_client, False)
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        otvet = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
        assert otvet.status_code == 200, otvet.text
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
        waybills_on(root_client)

    assert stock_of(root_client, item["id"]) == 10_000, (
        "закрытие без накладной двинуло склад при выключенном блоке склада"
    )
    # Закрытие, не тронувшее склад, обязано сказать об этом словами: иначе в
    # истории оно неотличимо от обычной отгрузки.
    istoriya = root_client.get(f"{API}/documents/{order['id']}").json()["events"]
    assert any("warehouse module off" in (e["note"] or "") for e in istoriya), (
        "закрытие без движений в истории названо обычной отгрузкой"
    )


def test_pri_vyklyuchennom_sklade_nekhvatka_ne_meshaet_zakryt(root_client, client_row):
    """Выключенный блок не отказывает от имени остатка, которого никто не видит.

    Складское в закрытии — это не одни движения. Проверка нехватки стояла до
    всякой оглядки на блок: заказ на пять при остатке одна вставал с
    `not_enough_stock`, хотя раздела склада в системе нет и человеку нечем ни
    посмотреть остаток, ни поправить его.
    """
    item = product(root_client, stock="1")
    order = order_with(root_client, client_row, item, quantity="5")
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        otvet = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
        assert otvet.status_code == 200, otvet.text
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})

    assert stock_of(root_client, item["id"]) == 1_000, "остаток тронут при выключенном складе"


def test_pri_vyklyuchennom_sklade_sebestoimost_ne_snimaetsya(root_client, client_row):
    """Снимок себестоимости — тоже складское, и при выключенном складе его нет.

    Проверяются обе стороны разом: выбросить снимок совсем так же неверно, как
    делать его мимо выключенного блока, — карточка заказа перестала бы
    отвечать, во сколько обошлась отгрузка.
    """
    item = product(root_client, stock="10", cost="100")
    so_skladom = order_with(root_client, client_row, item, quantity="1")
    assert root_client.post(f"{ORDERS}/{so_skladom['id']}/close", json={}).status_code == 200
    stroka = root_client.get(f"{ORDERS}/{so_skladom['id']}").json()["lines"][0]
    assert stroka["cost"] == 100, "заказ не помнит, во сколько обошлась отгрузка"

    bez_sklada = order_with(root_client, client_row, item, quantity="1")
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        assert root_client.post(f"{ORDERS}/{bez_sklada['id']}/close", json={}).status_code == 200
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    stroka = root_client.get(f"{ORDERS}/{bez_sklada['id']}").json()["lines"][0]
    assert stroka["cost"] is None, "себестоимость снята при выключенном складе"


def test_bez_nakladnykh_pri_vyklyuchennom_sklade_vozvrat_ne_pishet_dvizheniy(
    root_client, client_row
):
    """Остаток сошёлся — это ещё не значит, что склад не трогали.

    Списание и возврат гасят друг друга, и по одному остатку выключенный блок
    неотличим от включённого. Поэтому спрашиваем число движений: их обязано
    остаться столько же, сколько было до закрытия.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")
    waybills_on(root_client, False)
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
        vozvrat = root_client.post(f"{ORDERS}/{order['id']}/returns").json()
        otvet = root_client.post(f"{API}/returns/{vozvrat['id']}/post", json={})
        assert otvet.status_code == 200, otvet.text
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
        waybills_on(root_client)

    assert stock_of(root_client, item["id"]) == 10_000
    dvizheniya = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()
    assert dvizheniya["total"] == 1, (
        "склад был выключен, а движений прибавилось: остаток сошёлся лишь потому, "
        "что возврат вернул то, что закрытие не имело права списывать"
    )


def test_vozvrat_pri_vyklyuchennom_sklade_ne_dvigaet_ego_obratno(root_client, client_row):
    """Склад выключили между закрытием и возвратом — обратных движений нет тоже.

    Накладная в том же положении не пишет ничего (`waybill_service.provesti`),
    и возврат обязан вести себя так же — и сказать об этом в своей истории.
    """
    waybills_on(root_client, False)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="3")
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    assert stock_of(root_client, item["id"]) == 7_000, "с включённым складом заказ обязан списать"
    vozvrat = root_client.post(f"{ORDERS}/{order['id']}/returns").json()

    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        otvet = root_client.post(f"{API}/returns/{vozvrat['id']}/post", json={})
        assert otvet.status_code == 200, otvet.text
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
        waybills_on(root_client)

    assert stock_of(root_client, item["id"]) == 7_000, (
        "возврат вернул товар при выключенном блоке склада"
    )
    istoriya = root_client.get(f"{API}/returns/{vozvrat['id']}").json()["events"]
    assert any("warehouse module off" in (e["note"] or "") for e in istoriya), (
        "возврат без движений в истории назван обычным"
    )


def test_kartochka_zakaza_pokazyvaet_svoyu_nakladnuyu(root_client, client_row):
    """Бумага, которую нельзя найти, всё равно что не выписана.

    Закрытие теперь выписывает накладную. Не покажи мы КАКУЮ — человек знает,
    что она где-то есть, и ищет её глазами по всему списку накладных.

    У заказов, закрытых до переезда, список пуст, и это честнее выдуманного
    номера: задним числом бумага им не выписывается.
    """
    waybills_on(root_client)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="2")

    do = root_client.get(f"{ORDERS}/{order['id']}").json()
    # С 05.09.2026 черновик по заказу заводится сам с первой товарной строки:
    # карточка показывает его сразу, а закрытие проводит ЕГО, не заводя второго.
    assert [w["status"] for w in do["waybills"]] == ["draft"], "у заказа с товаром нет своего черновика"

    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    posle = root_client.get(f"{ORDERS}/{order['id']}").json()
    assert len(posle["waybills"]) == 1, "закрытие завело вторую накладную вместо проведения черновика"
    bumaga = posle["waybills"][0]
    assert bumaga["id"] == do["waybills"][0]["id"]
    assert bumaga["number"], "накладная без номера — по чему её искать"
    assert bumaga["status"] == "issued"


def test_bez_bloka_nakladnykh_kartochka_o_nikh_ne_upominaet(root_client, client_row):
    """Выключенный блок исчезает целиком, включая упоминания о себе.

    Пустой список означал бы «накладных нет», а правда — «накладных не
    бывает». Это разные ответы, и второй нельзя изображать первым.
    """
    waybills_on(root_client, False)
    try:
        item = product(root_client, stock="10")
        order = order_with(root_client, client_row, item, quantity="1")
        assert "waybills" not in root_client.get(f"{ORDERS}/{order['id']}").json()
    finally:
        waybills_on(root_client)


def test_chastichnaya_otgruzka_snimaet_rezerv_na_otgruzhennoe(root_client, client_row):
    """Отгруженное перестаёт быть обещанным — даже если заказ ещё открыт.

    Резерв считается запросом по строкам НЕЗАКРЫТЫХ заказов. Пока отгрузка
    случалась только вместе с закрытием, этого хватало: закрылся — вышел из
    отбора. Но накладную можно выписать по заказу и провести, не закрывая его
    (`POST /waybills/from-order/{id}`), и тогда товар уходит со склада ДВАЖДЫ:
    физически — движением, и на бумаге — резервом, который остался прежним.

    Ошибка тихая и в опасную сторону: «доступно» занижается, и товар, лежащий
    на полке, система считает обещанным. Продавец отказывает покупателю,
    глядя на число, которого нет.
    """
    waybills_on(root_client)
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="4")

    do = promises(root_client, item["id"])
    assert do["reserved_milli"] == 4000 and do["available_milli"] == 6000

    # Отгружаем половину заказа накладной, заказ при этом НЕ закрываем.
    nakladnaya = root_client.post(f"{API}/waybills/from-order/{order['id']}")
    assert nakladnaya.status_code in (200, 201), nakladnaya.text
    bumaga = nakladnaya.json()
    stroki = root_client.get(f"{API}/waybills/{bumaga['id']}").json()["lines"]
    assert stroki, "накладная по заказу вышла пустой"
    pravka = root_client.patch(
        f"{API}/waybills/{bumaga['id']}/lines/{stroki[0]['id']}", json={"quantity": "1"}
    )
    assert pravka.status_code == 200, pravka.text
    proveli = root_client.post(f"{API}/waybills/{bumaga['id']}/post", json={})
    assert proveli.status_code == 200, proveli.text

    assert stock_of(root_client, item["id"]) == 9000, "накладная не списала товар"
    # Строка заказа знает, сколько уехало: «отгружено 1 из 4» (план З-07).
    [stroka] = root_client.get(f"{API}/orders/{order['id']}").json()["lines"]
    assert stroka["shipped_milli"] == 1000 and stroka["quantity_milli"] == 4000
    posle = promises(root_client, item["id"])
    assert posle["reserved_milli"] == 3000, (
        f"резерв не уменьшился на отгруженное: {posle['reserved_milli']} вместо 3000"
    )
    assert posle["available_milli"] == 6000, (
        f"доступное посчитано дважды: {posle['available_milli']} вместо 6000"
    )

    # Сторно возвращает товар — и обещание вместе с ним. Отгруженное считается
    # по ДВИЖЕНИЯМ, а не по бумагам, и возврат учитывается сам собой: иначе
    # пришлось бы отдельно вычитать сторно и помнить об этом вечно.
    storno = root_client.post(f"{API}/waybills/{bumaga['id']}/reverse")
    assert storno.status_code == 201, storno.text
    obratno = root_client.post(
        f"{API}/waybills/{storno.json()['id']}/post", json={"confirm_negative": True}
    )
    assert obratno.status_code == 200, obratno.text

    assert stock_of(root_client, item["id"]) == 10000, "сторно не вернуло товар"
    vernuli = promises(root_client, item["id"])
    assert vernuli["reserved_milli"] == 4000, (
        f"обещание не вернулось вместе с товаром: {vernuli['reserved_milli']}"
    )
    assert vernuli["available_milli"] == 6000

def test_istoriya_zakaza_prihodit_na_kartochku(root_client, client_row):
    """Примечание, которого не показывают, — запись для никого.

    Закрытие при выключенном складе пишет в историю «движений нет», и это
    единственное место, где человек отличает «списали» от «не списали»:
    остаток не изменился, бумаги нет, а почему — не сказано нигде. Пока
    карточка заказа историю не отдавала, запись существовала для журнала
    базы и больше ни для кого.
    """
    item = product(root_client, stock="10")
    order = order_with(root_client, client_row, item, quantity="2")
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200

    karta = root_client.get(f"{ORDERS}/{order['id']}")
    assert karta.status_code == 200, karta.text
    events = karta.json().get("events")
    assert events, "карточка заказа не отдаёт историю переходов"
    assert any(e["to_status"] == "closed" for e in events), (
        "в истории нет закрытия — показывать нечего"
    )
    assert all("author_name" in e and "created_at" in e for e in events), (
        "у записи нет автора или времени: спор о сроках такой историей не решить"
    )


def test_klient_u_zakaza_privyazyvaetsya_poka_zakaz_otkryt(root_client, client_row):
    """Клиент у заказа необязателен и меняется только пока заказ открыт.

    Просьба владельца 05.09.2026: у стойки клиента часто негде взять, но когда
    он есть, отгрузки должны копиться в его истории — на будущую статистику.
    Проведённый заказ записан, и для кого он был, уже не меняется.
    """
    item = product(root_client, stock="10")
    created = root_client.post(ORDERS, json={"kind": "sales_order"})
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["client_id"] is None and order["client_name"] is None

    privyazan = root_client.post(f"{ORDERS}/{order['id']}/client", json={"client_id": client_row["id"]})
    assert privyazan.status_code == 200, privyazan.text
    assert privyazan.json()["client_id"] == client_row["id"]
    assert privyazan.json()["client_name"] == client_row["name"], "имя приходит вместе с номером"

    v_spiske = root_client.get(ORDERS, params={"client_id": client_row["id"], "per_page": 200}).json()["items"]
    assert next(o for o in v_spiske if o["id"] == order["id"])["client_name"] == client_row["name"]
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["client_name"] == client_row["name"]

    assert root_client.post(f"{ORDERS}/{order['id']}/client", json={"client_id": 999999}).status_code == 404
    assert root_client.post(f"{ORDERS}/{order['id']}/client", json={"client_id": None}).json()["client_id"] is None
    root_client.post(f"{ORDERS}/{order['id']}/client", json={"client_id": client_row["id"]})

    root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    otkaz = root_client.post(f"{ORDERS}/{order['id']}/client", json={"client_id": None})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "order_finished"
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["client_id"] == client_row["id"], "записанное не переписалось"


def test_srok_zakaza_i_prosrochka(root_client, client_row):
    """Срок заказа (план З-05): назначается при заведении и правкой, просрочка
    считается сервером у открытого, отбор `overdue=1` и число на сводке."""
    vchera = "2026-01-01T10:00:00Z"
    zavtra = "2099-01-01T10:00:00Z"
    item = product(root_client)
    order = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"], "due_at": vchera}
    ).json()
    root_client.post(f"{ORDERS}/{order['id']}/lines", json={"product_id": item["id"], "quantity": "1"})
    karta = root_client.get(f"{ORDERS}/{order['id']}").json()
    assert karta["due_at"] and karta["overdue"] is True

    prosrochennye = root_client.get(ORDERS, params={"overdue": 1, "per_page": 200}).json()["items"]
    assert any(o["id"] == order["id"] for o in prosrochennye)
    svodka = root_client.get(f"{API}/dashboard").json()
    assert svodka["orders_week"]["overdue_count"] >= 1

    pravka = root_client.patch(f"{ORDERS}/{order['id']}", json={"due_at": zavtra})
    assert pravka.status_code == 200, pravka.text
    assert pravka.json()["overdue"] is False
    assert not any(o["id"] == order["id"] for o in root_client.get(ORDERS, params={"overdue": 1, "per_page": 200}).json()["items"])
    assert root_client.patch(f"{ORDERS}/{order['id']}", json={"due_at": None}).json()["due_at"] is None
    istoriya = root_client.get(f"{ORDERS}/{order['id']}").json()["events"]
    assert any("due" in (e["note"] or "") for e in istoriya), "перенос срока не попал в историю"

    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    otkaz = root_client.patch(f"{ORDERS}/{order['id']}", json={"due_at": zavtra})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "order_finished"
