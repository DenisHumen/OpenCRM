"""Заказ из заявки и обратно.

Ради этого весь план и затевался: собрать заказ из одного места. Проверяется
здесь не «ответ 201», а три вещи, которые ломаются молча: что бронь при переносе
не удваивается, что упаковка в заказ не попадает, и что заявка видит сборку.

Разбор — `docs/19-sborka-zakaza.md` §6.3.
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API

WH = f"{API}/warehouse"


@pytest.fixture(scope="module", autouse=True)
def bloki(root_client: TestClient):
    from core.services import modules_service

    for blok in ("warehouse", "documents", "orders"):
        otvet = root_client.post(f"{API}/modules/{blok}", json={"enabled": True})
        # Код ответа проверяется: молчаливое переключение однажды откажет —
        # режим обслуживания, зависимость блока, — и файл упадёт не здесь, а на
        # 403 в первом же тесте, где про блоки не сказано ни слова.
        assert otvet.status_code == 200, f"{blok}: {otvet.text}"
    modules_service.invalidate()
    yield
    for blok in ("orders", "warehouse"):
        otvet = root_client.post(f"{API}/modules/{blok}", json={"enabled": False})
        assert otvet.status_code == 200, f"{blok}: {otvet.text}"
    modules_service.invalidate()


@pytest.fixture
def tovar(root_client: TestClient) -> dict:
    otvet = root_client.post(f"{WH}/products", json={"name": "Товар для заказа", "price": 100_000})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


@pytest.fixture
def zayavka(root_client: TestClient) -> int:
    klient = root_client.post(f"{API}/clients", json={"name": "Покупатель заказа"}).json()
    otvet = root_client.post(
        f"{API}/deals", json={"title": "Заявка под заказ", "client_id": klient["id"]}
    )
    assert otvet.status_code == 201, otvet.text
    return otvet.json()["id"]


def zakazy_zayavki(client: TestClient, deal_id: int) -> list[dict]:
    """Заказы заявки — тем же путём, которым их берёт экран.

    Карточка заявки своего списка заказов не отдаёт: врезка `OrdersOfCard`
    спрашивает их отбором, и второй список в ответе карточки разошёлся бы с
    первым при первой же правке.
    """
    otvet = client.get(f"{API}/orders", params={"deal_id": deal_id})
    assert otvet.status_code == 200, otvet.text
    return otvet.json()["items"]


def stroka(client: TestClient, deal_id: int, **polya) -> dict:
    otvet = client.post(f"{API}/deals/{deal_id}/lines", json=polya)
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def test_zakaz_beryot_tovary_zayavki(root_client, tovar, zayavka):
    stroka(root_client, zayavka, product_id=tovar["id"], quantity="3")

    otvet = root_client.post(f"{API}/deals/{zayavka}/order")
    assert otvet.status_code == 201, otvet.text
    zakaz = otvet.json()

    assert zakaz["deal_id"] == zayavka
    assert len(zakaz["lines"]) == 1
    assert zakaz["lines"][0]["product_id"] == tovar["id"]
    assert zakaz["lines"][0]["quantity_milli"] == 3000


def test_svoi_traty_v_zakaz_ne_edut(root_client, tovar, zayavka):
    """По заказу кладовщик собирает коробки, и «упаковка» показывала бы
    «собрано 0 из 1», пока её не отметят руками."""
    stroka(root_client, zayavka, product_id=tovar["id"], quantity="2")
    stroka(root_client, zayavka, name="Упаковка", quantity="1", price=250_000)

    zakaz = root_client.post(f"{API}/deals/{zayavka}/order").json()

    assert len(zakaz["lines"]) == 1, "своя трата уехала в заказ"
    # А из суммы заявки упаковка никуда не делась.
    assert root_client.get(f"{API}/deals/{zayavka}").json()["amount"] == 2 * 100_000 + 250_000


def test_zayavka_bez_tovarov_zakaz_ne_zavodit(root_client, zayavka):
    stroka(root_client, zayavka, name="Только работа", quantity="1", price=100_000)
    otkaz = root_client.post(f"{API}/deals/{zayavka}/order")
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "no_product_lines"


def test_zayavka_vidit_svoy_zakaz_i_ego_sborku(root_client, tovar, zayavka):
    """«Товар собран» на карточке заявки — то, ради чего связь и заводилась."""
    stroka(root_client, zayavka, product_id=tovar["id"], quantity="2")
    zakaz = root_client.post(f"{API}/deals/{zayavka}/order").json()

    do_sborki = zakazy_zayavki(root_client, zayavka)
    assert [z["id"] for z in do_sborki] == [zakaz["id"]]
    assert do_sborki[0]["assembled"] is False

    # Собираем сканом — так это и делают на складе: две штуки, два скана.
    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    kod = "4600000000017"
    assert (
        root_client.post(
            f"{API}/labels/products/{tovar['id']}/barcodes", json={"code": kod}
        ).status_code
        == 201
    )
    for _ in range(2):
        sborka = root_client.post(f"{API}/orders/{zakaz['id']}/pick", json={"code": kod})
        assert sborka.status_code == 200, sborka.text

    assert zakazy_zayavki(root_client, zayavka)[0]["assembled"] is True


def test_zakaz_iz_zayavki_ne_udvaivaet_bron(root_client, tovar, zayavka):
    """Перенос — это перенос, а не второе обещание того же товара."""
    root_client.post(f"{WH}/moves", json={"product_id": tovar["id"], "kind": "in", "quantity": "10"})
    stroka(root_client, zayavka, product_id=tovar["id"], quantity="3")
    assert root_client.get(f"{WH}/products/{tovar['id']}/availability").json()["reserved_milli"] == 3000

    root_client.post(f"{API}/deals/{zayavka}/order")

    assert root_client.get(f"{WH}/products/{tovar['id']}/availability").json()["reserved_milli"] == 3000


def test_zakaz_pritsepliaetsya_i_otsepliaetsya(root_client, tovar, zayavka):
    """Заказ цепляют не к той заявке так же часто, как и к той."""
    zakaz = root_client.post(
        f"{API}/orders", json={"kind": "sales_order", "client_name": "Сам по себе заказ"}
    ).json()
    assert zakaz["deal_id"] is None

    privyazan = root_client.post(f"{API}/orders/{zakaz['id']}/deal", json={"deal_id": zayavka})
    assert privyazan.status_code == 200, privyazan.text
    assert privyazan.json()["deal_id"] == zayavka
    assert [z["id"] for z in zakazy_zayavki(root_client, zayavka)] == [zakaz["id"]]

    otvyazan = root_client.post(f"{API}/orders/{zakaz['id']}/deal", json={"deal_id": None})
    assert otvyazan.json()["deal_id"] is None
    assert zakazy_zayavki(root_client, zayavka) == []


def test_zakrytaya_zayavka_zakaz_ne_prinimaet(root_client, tovar, zayavka):
    stroka(root_client, zayavka, product_id=tovar["id"], quantity="1")
    etapy = root_client.get(f"{API}/pipeline/stages").json()["items"]
    won = next(e["key"] for e in etapy if e["kind"] == "won")
    root_client.post(f"{API}/deals/{zayavka}/move", json={"stage": won})

    otkaz = root_client.post(f"{API}/deals/{zayavka}/order")
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "deal_closed"
