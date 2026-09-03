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


def test_spisanie_idyot_so_sklada_stroki(root_client, tovar, zayavka):
    """Назвали склад в строке — списывается именно с него, а не с основного."""
    vtoroy = root_client.post(f"{API}/warehouses", json={"name": "Второй склад"})
    assert vtoroy.status_code == 201, vtoroy.text
    sklad = vtoroy.json()["id"]
    try:
        # Приход РОВНО столько, сколько уйдёт: склад с остатком не закрывается,
        # а оставленный склад ломает соседние проверки — «складов больше
        # одного» выводится из данных, и лишний делает его вечно истинным.
        root_client.post(
            f"{WH}/moves",
            json={"product_id": tovar["id"], "kind": "in", "quantity": "2", "warehouse_id": sklad},
        )
        root_client.post(
            f"{API}/deals/{zayavka['id']}/lines",
            json={"product_id": tovar["id"], "quantity": "2", "warehouse_id": sklad},
        )

        peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

        dvizheniya = root_client.get(f"{WH}/products/{tovar['id']}/moves").json()["items"]
        rashod = next(d for d in dvizheniya if d["kind"] == "out")
        assert rashod["warehouse_id"] == sklad, "списали не с того склада"
    finally:
        # Код ответа НЕ проверяем: не сработай списание — на складе остался бы
        # остаток, закрытие отказало бы, и в отчёте стояла бы ошибка про склад
        # вместо той, которую тест искал.
        root_client.delete(f"{API}/warehouses/{sklad}")


def test_svoya_trata_sklad_ne_prinimaet(root_client, zayavka):
    """Упаковку не берут с полки: склад у такой строки — ошибка звонящего."""
    # Склад берём НАСТОЯЩИЙ: с выдуманным номером проверка держалась бы на том,
    # что отказ случается раньше поиска склада, — то есть на порядке строк
    # внутри чужой функции.
    sklad = next(
        s["id"] for s in root_client.get(f"{API}/warehouses").json()["items"] if s["is_default"]
    )
    otkaz = root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"name": "Упаковка", "quantity": "1", "warehouse_id": sklad},
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "line_has_no_warehouse"


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


def test_prihod_pod_zayavku_ne_udvaivaet_spisanie(root_client, tovar, zayavka):
    """Закупка под клиента цепляется к заявке — и не имеет права стать вычетом.

    Приходная накладная наследует `deal_id` заявки. Считай «ушло под заявку» по
    всем движениям со знаком — величина уйдёт в минус, а вычитание минуса
    ПРИБАВИТ: десять в строках превратятся в двадцать со склада.
    """
    prihod(root_client, tovar["id"], "30")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "10"},
    )
    # Приход, помеченный этой же заявкой, — как его пишет приходная накладная.
    root_client.post(
        f"{WH}/moves",
        json={"product_id": tovar["id"], "kind": "in", "quantity": "10",
              "deal_id": zayavka["id"]},
    )
    # И частичный расход под неё же: без него беду не увидеть — отрицательную
    # величину гасит обрезка по нулю, и отбор по видам ничего не меняет.
    root_client.post(
        f"{WH}/moves",
        json={"product_id": tovar["id"], "kind": "out", "quantity": "4",
              "deal_id": zayavka["id"]},
    )
    bylo = ostatok(root_client, tovar["id"])
    assert bylo == 36000

    peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

    # Ушло четыре из десяти — дописать осталось шесть. Считай приход вычетом, и
    # четыре ушедших обнулятся, а списание уйдёт на все десять.
    assert ostatok(root_client, tovar["id"]) == bylo - 6000, "приход зачтён как вычет"


def test_zakrytie_ne_spisyvaet_to_chto_otgruzit_zakaz(root_client, tovar, zayavka):
    """Путь к остатку ровно один: есть открытый заказ — списывает он.

    Иначе закрытие заявки и последующая накладная по её заказу вынесут со
    склада вдвое больше, чем в строках, и заметят это на инвентаризации.
    """
    prihod(root_client, tovar["id"], "20")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "5"},
    )
    zakaz = root_client.post(f"{API}/deals/{zayavka['id']}/order")
    assert zakaz.status_code == 201, zakaz.text

    peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

    assert ostatok(root_client, tovar["id"]) == 20000, (
        "закрытие списало товар, который ещё отгрузит открытый заказ"
    )


def test_vyklyuchennyy_blok_zakazov_ne_daet_spisat_dvazhdy(root_client, tovar, zayavka):
    """Выключенный блок заказов не отменяет открытый заказ — он его прячет.

    Вычитаемое «непогашенное заказами» стояло под `is_enabled("orders")`, и
    выключение блока снимало единственную защиту: заявка списывала то, что ещё
    держит заказ. Отгрузить его после этого можно даже не включая блок обратно —
    накладную по заказу выписывает блок накладных.
    """
    prihod(root_client, tovar["id"], "20")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "5"},
    )
    zakaz = root_client.post(f"{API}/deals/{zayavka['id']}/order")
    assert zakaz.status_code == 201, zakaz.text

    vykl = root_client.post(f"{API}/modules/orders", json={"enabled": False})
    assert vykl.status_code == 200, vykl.text
    try:
        peredvinut(root_client, zayavka["id"], etap(root_client, "won"))
        assert ostatok(root_client, tovar["id"]) == 20000, (
            "закрытие списало товар, который держит открытый заказ: блок выключен, "
            "но заказ жив и отгрузится накладной"
        )
    finally:
        root_client.post(f"{API}/modules/orders", json={"enabled": True})

def test_odin_tovar_s_dvukh_skladov_uhodit_s_oboikh(root_client, tovar, zayavka):
    """Две строки одного товара с разных складов — уйти обязано с обоих.

    Общая куча по товару списала бы всё с первого попавшегося склада: врут
    сразу два остатка, и ни одна цифра при этом не выглядит неправдоподобно.
    """
    vtoroy = root_client.post(f"{API}/warehouses", json={"name": "Склад двух строк"})
    assert vtoroy.status_code == 201, vtoroy.text
    sklad = vtoroy.json()["id"]
    osnovnoy = next(
        s["id"] for s in root_client.get(f"{API}/warehouses").json()["items"] if s["is_default"]
    )
    try:
        prihod(root_client, tovar["id"], "3")
        root_client.post(
            f"{WH}/moves",
            json={"product_id": tovar["id"], "kind": "in", "quantity": "2",
                  "warehouse_id": sklad},
        )
        stroki = f"{API}/deals/{zayavka['id']}/lines"
        root_client.post(stroki, json={"product_id": tovar["id"], "quantity": "2",
                                       "warehouse_id": sklad})
        root_client.post(stroki, json={"product_id": tovar["id"], "quantity": "3",
                                       "warehouse_id": osnovnoy})

        peredvinut(root_client, zayavka["id"], etap(root_client, "won"))

        po_skladam: dict[int, int] = {}
        for d in root_client.get(f"{WH}/products/{tovar['id']}/moves").json()["items"]:
            if d["kind"] == "out":
                po_skladam[d["warehouse_id"]] = (
                    po_skladam.get(d["warehouse_id"], 0) + d["quantity_milli"]
                )
        assert po_skladam == {sklad: -2000, osnovnoy: -3000}, (
            f"списание ушло не с тех складов: {po_skladam}"
        )
    finally:
        root_client.delete(f"{API}/warehouses/{sklad}")


def test_udalyonnyy_tovar_ne_zapiraet_zayavku(root_client, tovar, zayavka):
    """Товар убрали из справочника после набора строки — заявка обязана закрыться.

    Списание ищет товар вместе с удалёнными, а движение — без; подписчик
    выигрыша `participant`, значит смена этапа падает целиком. Заявку не
    закрыть было бы НИЧЕМ: ни кнопкой, ни доской, ни актом — только руками в
    базе. Убрать товар из списка при этом ничем не запрещено.
    """
    prihod(root_client, tovar["id"], "10")
    root_client.post(
        f"{API}/deals/{zayavka['id']}/lines",
        json={"product_id": tovar["id"], "quantity": "3"},
    )
    assert root_client.delete(f"{WH}/products/{tovar['id']}").status_code == 200

    otvet = root_client.post(
        f"{API}/deals/{zayavka['id']}/move", json={"stage": etap(root_client, "won")}
    )
    assert otvet.status_code == 200, f"выигрыш заперт удалённым товаром: {otvet.text}"

    assert root_client.post(f"{WH}/products/{tovar['id']}/restore").status_code == 200
    assert ostatok(root_client, tovar["id"]) == 7000, "списания не было вовсе"
