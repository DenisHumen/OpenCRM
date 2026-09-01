"""Списание при закрытии заявки: ровно один раз и ни разу лишнего.

Главные проверки здесь — про ПОВТОР. Этап откатывают руками каждый день, а
движение склада не отменяется удалением, только обратным движением: списав
второй раз, остаток занизишь молча, и заметят это при инвентаризации.

Разбор правила — `docs/19-sborka-zakaza.md` §Р4.
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API

WH = f"{API}/warehouse"


@pytest.fixture(scope="module", autouse=True)
def bloki(root_client: TestClient):
    from core.services import modules_service

    for blok in ("warehouse", "documents", "orders"):
        root_client.post(f"{API}/modules/{blok}", json={"enabled": True})
    modules_service.invalidate()
    yield
    for blok in ("orders", "warehouse"):
        root_client.post(f"{API}/modules/{blok}", json={"enabled": False})
    modules_service.invalidate()


@pytest.fixture
def tovar(root_client: TestClient) -> dict:
    otvet = root_client.post(f"{WH}/products", json={"name": "Товар под списание", "price": 100_000})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


@pytest.fixture
def zayavka(root_client: TestClient) -> dict:
    klient = root_client.post(f"{API}/clients", json={"name": "Покупатель списания"}).json()
    otvet = root_client.post(
        f"{API}/deals", json={"title": "Заявка под списание", "client_id": klient["id"]}
    )
    assert otvet.status_code == 201, otvet.text
    return {"id": otvet.json()["id"], "client_id": klient["id"]}


def etap(client: TestClient, kind: str) -> str:
    etapy = client.get(f"{API}/pipeline/stages").json()["items"]
    return next(e["key"] for e in etapy if e["kind"] == kind)


def prihod(client: TestClient, product_id: int, skolko: str) -> None:
    otvet = client.post(f"{WH}/moves", json={"product_id": product_id, "kind": "in", "quantity": skolko})
    assert otvet.status_code == 201, otvet.text


def ostatok(client: TestClient, product_id: int) -> int:
    return client.get(f"{WH}/products/{product_id}").json()["stock_milli"]


def peredvinut(client: TestClient, deal_id: int, kluch: str, **extra) -> None:
    otvet = client.post(f"{API}/deals/{deal_id}/move", json={"stage": kluch, **extra})
    assert otvet.status_code == 200, otvet.text


def test_vyigrannaya_zayavka_spisyvaet_tovar(root_client, tovar, zayavka):
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})

    peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

    assert ostatok(root_client, tovar["id"]) == 7000


def test_zakryli_dvazhdy_spisalos_odin_raz(root_client, tovar, zayavka):
    """Этап откатывают руками каждый день — формула обязана это пережить."""
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})

    won = etap(root_client, "won")
    peredvinut(root_client, zayavka["id"], won)
    assert ostatok(root_client, tovar["id"]) == 7000

    # откатили этап и закрыли снова
    peredvinut(root_client, zayavka["id"], etap(root_client, "open"))
    peredvinut(root_client, zayavka["id"], won)
    assert ostatok(root_client, tovar["id"]) == 7000, "списалось второй раз"


def test_proigrannaya_ne_spisyvaet(root_client, tovar, zayavka):
    """Товар по проигранной заявке никуда не уехал."""
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})

    peredvinut(root_client, zayavka["id"], etap(root_client, "lost"), lost_reason="передумали")

    assert ostatok(root_client, tovar["id"]) == 10000


def test_dobavili_stroku_posle_otkata_spisalas_tolko_raznitsa(root_client, tovar, zayavka):
    """Закрыли на трёх, откатили, добавили ещё два — уйти должны только два."""
    prihod(root_client, tovar["id"], "10")
    stroki = f"{API}/deals/{zayavka['id']}/lines"
    root_client.post(stroki, json={"product_id": tovar["id"], "quantity": "3"})
    won = etap(root_client, "won")
    peredvinut(root_client, zayavka["id"], won)
    assert ostatok(root_client, tovar["id"]) == 7000

    peredvinut(root_client, zayavka["id"], etap(root_client, "open"))
    root_client.post(stroki, json={"product_id": tovar["id"], "quantity": "2"})
    peredvinut(root_client, zayavka["id"], won)

    assert ostatok(root_client, tovar["id"]) == 5000


def test_svoya_trata_i_usluga_ne_spisyvayutsya(root_client, zayavka):
    """У упаковки нет карточки, у услуги нет остатка — списывать нечего."""
    usluga = root_client.post(
        f"{WH}/products", json={"name": "Выезд мастера", "is_service": True, "price": 50_000}
    ).json()
    stroki = f"{API}/deals/{zayavka['id']}/lines"
    root_client.post(stroki, json={"name": "Упаковка", "quantity": "1", "price": 1000})
    root_client.post(stroki, json={"product_id": usluga["id"], "quantity": "1"})

    peredvinut(root_client, zayavka["id"], etap(root_client, "won"))
    # Услуга не имеет остатка вовсе — карточка отдаёт null, и это не ноль.
    assert root_client.get(f"{WH}/products/{usluga['id']}").json()["stock_milli"] is None


def test_bron_ischezaet_posle_spisaniya(root_client, tovar, zayavka):
    """Закрыли — товар ушёл, и держать его больше некому."""
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})
    assert root_client.get(f"{WH}/products/{tovar['id']}/availability").json()["reserved_milli"] == 3000

    peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

    est = root_client.get(f"{WH}/products/{tovar['id']}/availability").json()
    assert est["reserved_milli"] == 0
    assert est["stock_milli"] == 7000
    assert est["available_milli"] == 7000


def test_uzhe_ushedshee_pod_zayavku_ne_spisyvaetsya_vtoroy_raz(root_client, tovar, zayavka):
    """Товар уехал раньше закрытия — закрытие не имеет права повторить расход.

    Так работает отгрузка накладной: её движения несут `deal_id` заявки. Здесь
    то же самое движение делается напрямую — проверяется механизм вычитания, а
    не путь, которым движение появилось.
    """
    prihod(root_client, tovar["id"], "10")
    root_client.post(f"{API}/deals/{zayavka['id']}/lines", json={"product_id": tovar["id"], "quantity": "3"})

    ushlo = root_client.post(
        f"{WH}/moves",
        json={
            "product_id": tovar["id"],
            "kind": "out",
            "quantity": "3",
            "deal_id": zayavka["id"],
        },
    )
    assert ushlo.status_code == 201, ushlo.text
    assert ostatok(root_client, tovar["id"]) == 7000

    peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

    assert ostatok(root_client, tovar["id"]) == 7000, "закрытие списало то, что уже ушло"
