"""Строки заявки: из чего складывается её сумма.

Здесь проверяется не «ответ 201», а арифметика итога и то, ради чего строки
заводились: сумма перестала быть числом, набранным на глаз. Отдельно стоит
сторож кэша `deals.amount` — сознательного отступления от правила «производное
не хранится» (`docs/19-sborka-zakaza.md` §Р5).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database.models import Deal, DealLine
from tests.conftest import API

WH = f"{API}/warehouse"


@pytest.fixture(scope="module", autouse=True)
def sklad_vklyuchen(root_client: TestClient):
    """Раздел строк закрыт блоком склада целиком — без него его нет."""
    from core.services import modules_service

    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    modules_service.invalidate()
    yield
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    modules_service.invalidate()


@pytest.fixture
def zayavka(root_client: TestClient) -> int:
    klient = root_client.post(f"{API}/clients", json={"name": "Заказчик строк"}).json()
    otvet = root_client.post(
        f"{API}/deals", json={"title": "Поставка серверов", "client_id": klient["id"]}
    )
    assert otvet.status_code == 201, otvet.text
    return otvet.json()["id"]


def tovar(client: TestClient, **polya) -> dict:
    payload = {"name": "Сервер Dell R640", "cost": 3_900_000, "price": 4_500_000}
    payload.update(polya)
    otvet = client.post(f"{WH}/products", json=payload)
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def stroka(client: TestClient, deal_id: int, **polya) -> dict:
    otvet = client.post(f"{API}/deals/{deal_id}/lines", json=polya)
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def test_tovar_i_svoya_trata_v_odnoy_summe(root_client, zayavka):
    """Ради этого строки и заводились: три сервера плюс упаковка одним итогом."""
    server = tovar(root_client)
    stroka(root_client, zayavka, product_id=server["id"], quantity="3")
    stroka(root_client, zayavka, name="Упаковка и обрешётка", quantity="1", price=250_000)

    spisok = root_client.get(f"{API}/deals/{zayavka}/lines").json()
    assert [s["kind"] for s in spisok["items"]] == ["product", "extra"]
    # 3 × 45 000.00 + 2 500.00
    assert spisok["total_minor"] == 3 * 4_500_000 + 250_000

    kartochka = root_client.get(f"{API}/deals/{zayavka}").json()
    assert kartochka["amount"] == spisok["total_minor"], "сумма заявки не сошлась с итогом"


def test_tsena_i_nazvanie_snimok_a_ne_ssylka(root_client, zayavka):
    """Товар переименуют и переоценят — проданная заявка обязана не измениться."""
    server = tovar(root_client, name="Сервер до переоценки", price=1_000_000)
    dobavlena = stroka(root_client, zayavka, product_id=server["id"], quantity="2")
    assert dobavlena["price_minor"] == 1_000_000

    root_client.patch(f"{WH}/products/{server['id']}", json={"name": "Сервер после", "price": 9_000_000})

    posle = root_client.get(f"{API}/deals/{zayavka}/lines").json()["items"][0]
    assert posle["name"] == "Сервер до переоценки"
    assert posle["price_minor"] == 1_000_000
    assert posle["total_minor"] == 2_000_000


def test_svoya_tsena_perebivaet_praysovuyu(root_client, zayavka):
    server = tovar(root_client, price=4_500_000)
    dobavlena = stroka(root_client, zayavka, product_id=server["id"], quantity="1", price=4_000_000)
    assert dobavlena["price_minor"] == 4_000_000


def test_ubrali_poslednyuyu_stroku_summa_snova_ne_nazvana(root_client, zayavka):
    """Ноль означал бы «отдаём бесплатно» — а строк просто нет."""
    dobavlena = stroka(root_client, zayavka, name="Разовая работа", quantity="1", price=100_000)
    assert root_client.get(f"{API}/deals/{zayavka}").json()["amount"] == 100_000

    assert root_client.delete(f"{API}/deals/{zayavka}/lines/{dobavlena['id']}").status_code == 200
    assert root_client.get(f"{API}/deals/{zayavka}").json()["amount"] is None


def test_stroka_bez_tseny_ne_obnulyaet_itog(root_client, zayavka):
    """«Цену ещё не назвали» — это не «ноль»: остальное уже посчитано."""
    stroka(root_client, zayavka, name="Доставка", quantity="1", price=50_000)
    bez_tseny = stroka(root_client, zayavka, name="Согласуем позже", quantity="1")
    assert bez_tseny["price_minor"] is None and bez_tseny["total_minor"] is None
    assert root_client.get(f"{API}/deals/{zayavka}/lines").json()["total_minor"] == 50_000


def test_drobnoe_kolichestvo_schitaetsya_v_tysyachnykh(root_client, zayavka):
    """1,5 кг по 200.00 — это 300.00, а не 200 и не 30 000."""
    plyonka = tovar(root_client, name="Плёнка", unit="kg", price=20_000)
    dobavlena = stroka(root_client, zayavka, product_id=plyonka["id"], quantity="1.5")
    assert dobavlena["quantity_milli"] == 1500
    assert dobavlena["total_minor"] == 30_000


def test_nulevoe_kolichestvo_otkaz(root_client, zayavka):
    otkaz = root_client.post(f"{API}/deals/{zayavka}/lines", json={"name": "Ничего", "quantity": "0"})
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "quantity_not_positive"


def test_svoya_trata_bez_nazvaniya_otkaz(root_client, zayavka):
    otkaz = root_client.post(f"{API}/deals/{zayavka}/lines", json={"quantity": "1"})
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "name_required"


def test_nazvanie_tovarnoy_stroki_ne_pravitsya(root_client, zayavka):
    """Это снимок названия товара: переписать его значит соврать о проданном."""
    server = tovar(root_client)
    dobavlena = stroka(root_client, zayavka, product_id=server["id"], quantity="1")
    otkaz = root_client.patch(
        f"{API}/deals/{zayavka}/lines/{dobavlena['id']}", json={"name": "Другое"}
    )
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "name_is_snapshot"


def test_tovar_po_artikulu(root_client, zayavka):
    """Магазин знает артикул, которым торгует, а наших номеров записи — нет."""
    server = tovar(root_client, name="По артикулу")
    dobavlena = stroka(root_client, zayavka, sku=server["sku"], quantity="1")
    assert dobavlena["product_id"] == server["id"]


def test_zakrytaya_zayavka_stroki_ne_menyaet(root_client, zayavka):
    """По закрытой уже посчитана прибыль: правка задним числом развела бы отчёты."""
    server = tovar(root_client)
    stroka(root_client, zayavka, product_id=server["id"], quantity="1")
    etapy = root_client.get(f"{API}/pipeline/stages").json()["items"]
    won = next(e["key"] for e in etapy if e["kind"] == "won")
    assert root_client.post(f"{API}/deals/{zayavka}/move", json={"stage": won}).status_code == 200

    otkaz = root_client.post(f"{API}/deals/{zayavka}/lines", json={"name": "Ещё", "quantity": "1"})
    assert otkaz.status_code == 422
    assert otkaz.json()["error"]["code"] == "deal_closed"


def test_chuzhaya_stroka_ne_pravitsya_po_svoey_zayavke(root_client, zayavka):
    """Без сверки со своей заявкой строку соседа правили бы по её номеру."""
    chuzhaya = stroka(root_client, zayavka, name="Своя", quantity="1", price=1000)
    klient = root_client.post(f"{API}/clients", json={"name": "Второй заказчик"}).json()
    drugaya = root_client.post(
        f"{API}/deals", json={"title": "Вторая заявка", "client_id": klient["id"]}
    ).json()["id"]

    otkaz = root_client.patch(
        f"{API}/deals/{drugaya}/lines/{chuzhaya['id']}", json={"quantity": "5"}
    )
    assert otkaz.status_code == 404
    assert otkaz.json()["error"]["code"] == "line_not_found"


def test_summa_zayavki_ravna_summe_strok_po_vsey_baze(db):
    """Сторож кэша: `deals.amount` — хранимое производное (§Р5).

    Защита здесь слабее, чем у остатка склада: не «расхождение невозможно», а
    «расхождение поймает эта проверка». Она и есть цена отступления от правила,
    и повод вернуться к честному `JOIN` — её первое срабатывание на боевой базе.
    """
    po_strokam = dict(
        db.execute(
            select(
                DealLine.deal_id,
                func.sum(DealLine.price_minor * DealLine.quantity_milli),
            )
            .where(DealLine.price_minor.is_not(None))
            .group_by(DealLine.deal_id)
        ).all()
    )
    so_strokami = set(db.scalars(select(DealLine.deal_id).distinct()))
    if not so_strokami:
        pytest.skip("заявок со строками нет — сверять нечего")

    for deal in db.scalars(select(Deal).where(Deal.id.in_(so_strokami))):
        dolzhno = int(po_strokam.get(deal.id, 0)) // 1000
        assert deal.amount == dolzhno, f"заявка {deal.id}: {deal.amount} вместо {dolzhno}"
