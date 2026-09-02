"""Накладные: черновик, проведение, неизменяемость, сторнирование.

Главное, что проверяется, — **бумага и склад не расходятся**. Три правила, на
которых стоит модуль, и каждое проверяется с обеих сторон:

1. черновик правится, проведённая — нет (и не через интерфейс, а через API:
   спрятанная кнопка закрывает экран, а не правило);
2. исправляют сторнированием, а не правкой — движения обязаны остаться оба;
3. к остатку ведёт ровно один путь: двойная отгрузка по заказу невозможна ни с
   какой стороны.
"""

import itertools

import pytest
from sqlalchemy import delete, select, update

from core import exceptions as errors
from database.models.document import Document, DocumentLine
from tests.conftest import API

WAYBILLS = f"{API}/waybills"
ORDERS = f"{API}/orders"
STOCK = f"{API}/warehouse"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(scope="module", autouse=True)
def blocks_on(root_client):
    """Накладные стоят на бланках, а склад им нужен для проведения.

    **Раз на файл, а не на каждую проверку.** Переключение блока пишется в
    журнал действий, и четыре переключения на каждый из двух десятков тестов —
    это под сотню записей, которые вытесняют с первой страницы журнала всё
    остальное. Соседний тест (`test_audit.test_switching_a_module_records_both_states`)
    от этого и покраснел: он считает записи про СВОЙ блок, а страница отдаёт
    полсотни строк, и чужие переключения выталкивают его записи за её край.

    Ровно та же беда уже описана в докстроке того теста — «падал не от поломки,
    а от собственного соседства». Здесь она чинится с другой стороны: не
    засорять.
    """
    for key in ("documents", "warehouse", "orders", "waybills"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    yield


@pytest.fixture
def client_row(root_client):
    return root_client.post(f"{API}/clients", json={"name": f"Получатель {uniq()}"}).json()


def product(root_client, stock="10", cost="100", price="500", service=False):
    item = root_client.post(
        f"{STOCK}/products",
        json={
            "name": f"Товар {uniq()}", "sku": f"WB-{uniq()}",
            "cost": None if service else cost, "price": price, "is_service": service,
        },
    ).json()
    if stock and not service:
        root_client.post(
            f"{STOCK}/moves", json={"product_id": item["id"], "kind": "in", "quantity": stock}
        )
    return item


def ostatok(root_client, item) -> int:
    """Остаток в ТЫСЯЧНЫХ — как везде на складе.

    Не в штуках: правило проекта про целые в тысячных касается и проверок.
    Сравнивать «7» со строкой удобнее глазами, но однажды сравнилось бы «7.0» с
    «7», и тест позеленел бы или покраснел не по делу.
    """
    card = root_client.get(f"{STOCK}/products/{item['id']}").json()
    return card["stock_milli"]


def chernovik(root_client, client_row, item, quantity="3", kind="waybill_out"):
    created = root_client.post(WAYBILLS, json={"kind": kind, "client_id": client_row["id"]})
    assert created.status_code == 201, created.text
    waybill = created.json()
    added = root_client.post(
        f"{WAYBILLS}/{waybill['id']}/lines",
        json={"product_id": item["id"], "quantity": quantity},
    )
    assert added.status_code == 201, added.text
    return waybill


# --- черновик -----------------------------------------------------------------


def test_nakladnaya_rozhdaetsya_chernovikom(root_client, client_row):
    """Не «выданной». Её собирают, а собранное на полпути не документ."""
    created = root_client.post(WAYBILLS, json={"kind": "waybill_out", "client_id": client_row["id"]})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"
    assert created.json()["pravitsya"] is True


def test_chernovik_ne_dvigaet_sklad(root_client, client_row):
    """Пока бумага набирается, товар лежит на полке.

    Это главное отличие накладной от самой себя же проведённой, и проверять его
    надо первым: спутай мы моменты — и остаток начнёт падать при наборе, то есть
    при действии, которое человек считает черновым и отменяет не задумываясь.
    """
    item = product(root_client, stock="10")
    chernovik(root_client, client_row, item, quantity="3")
    assert ostatok(root_client, item) == 10_000, "черновик тронул склад"


def test_pustuyu_provesti_nelzya(root_client, client_row):
    """Провести пустую значит закрыть её, ничего не отгрузив."""
    created = root_client.post(WAYBILLS, json={"kind": "waybill_out", "client_id": client_row["id"]})
    otvet = root_client.post(f"{WAYBILLS}/{created.json()['id']}/post", json={})
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "waybill_is_empty"


def test_uslugu_otgruzit_nelzya(root_client, client_row):
    """У услуги нет остатка и быть не может.

    У ЗАКАЗА услуга законна — её продают. У накладной нет: накладная про
    физическое перемещение вещи, и строка-услуга в ней ничего не двигала бы,
    оставаясь при этом похожей на настоящую.
    """
    usluga = product(root_client, service=True)
    created = root_client.post(WAYBILLS, json={"kind": "waybill_out", "client_id": client_row["id"]})
    otvet = root_client.post(
        f"{WAYBILLS}/{created.json()['id']}/lines",
        json={"product_id": usluga["id"], "quantity": "1"},
    )
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "service_has_no_stock"


# --- проведение ---------------------------------------------------------------


def test_provedenie_spisyvaet_so_sklada(root_client, client_row):
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")

    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "issued"
    assert otvet.json()["pravitsya"] is False
    assert ostatok(root_client, item) == 7_000


def test_prihodnaya_nakladnaya_pribavlyaet(root_client, client_row):
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="4", kind="waybill_in")
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert otvet.status_code == 200, otvet.text
    assert ostatok(root_client, item) == 14_000


def test_nehvatka_ostanavlivaet(root_client, client_row):
    """Отгрузить нечего физически — и это не предупреждение, а остановка."""
    item = product(root_client, stock="2")
    waybill = chernovik(root_client, client_row, item, quantity="5")
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "not_enough_stock"
    assert ostatok(root_client, item) == 2_000, "склад тронулся при отказе"


def test_nehvatku_mozhno_podtverdit_i_eto_zapisyvaetsya(root_client, client_row):
    """«Отгрузили в минус» — решение человека, и через месяц спросят чьё."""
    item = product(root_client, stock="2")
    waybill = chernovik(root_client, client_row, item, quantity="5")
    otvet = root_client.post(
        f"{WAYBILLS}/{waybill['id']}/post", json={"confirm_negative": True}
    )
    assert otvet.status_code == 200, otvet.text
    assert ostatok(root_client, item) == -3_000

    # История лежит ВНУТРИ карточки бланка, отдельной ручки у неё нет.
    karta = root_client.get(f"{API}/documents/{waybill['id']}")
    assert karta.status_code == 200, karta.text
    zapisi = karta.json()["events"]
    assert any("нехватк" in (z.get("note") or "") for z in zapisi), (
        f"подтверждение нехватки не записано в историю: {zapisi}"
    )


def test_dvoynoe_provedenie_ne_spisyvaet_dvazhdy(root_client, client_row):
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    assert root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={}).status_code == 200
    vtoroy = root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    # 422, а не 409: второе нажатие упирается не в гонку, а в то, что бумага
    # уже не черновик. Гонка (двое одновременно) ловится условной сменой
    # статуса ниже по коду и отвечает 409 — здесь же нажатие второе, а не
    # одновременное.
    assert vtoroy.status_code == 422, vtoroy.text
    assert vtoroy.json()["error"]["code"] == "waybill_is_final"
    assert ostatok(root_client, item) == 7_000, "второе проведение списало ещё раз"


# --- неизменяемость -----------------------------------------------------------


def test_provedennuyu_pravit_nelzya(root_client, client_row):
    """Через API, а не через экран: спрятанная кнопка правила не держит.

    Три способа изменить бумагу проверяются по отдельности нарочно. Закрыть один
    и забыть про два — это ровно то, что случается, когда неизменяемость держат
    интерфейсом: кнопки нет, а ручка отвечает 200.
    """
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    stroka = root_client.get(f"{WAYBILLS}/{waybill['id']}").json()["lines"][0]
    assert root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={}).status_code == 200

    dobavit = root_client.post(
        f"{WAYBILLS}/{waybill['id']}/lines",
        json={"product_id": item["id"], "quantity": "1"},
    )
    assert dobavit.status_code == 422, "в проведённую добавили строку"
    assert dobavit.json()["error"]["code"] == "waybill_is_final"

    popravit = root_client.patch(
        f"{WAYBILLS}/{waybill['id']}/lines/{stroka['id']}", json={"quantity": "99"}
    )
    assert popravit.status_code == 422, "строку проведённой поправили"

    udalit = root_client.delete(f"{WAYBILLS}/{waybill['id']}/lines/{stroka['id']}")
    assert udalit.status_code == 422, "строку проведённой удалили"

    # И количество вправду осталось прежним, а не «отказали, но записали».
    posle = root_client.get(f"{WAYBILLS}/{waybill['id']}").json()
    assert len(posle["lines"]) == 1
    assert posle["lines"][0]["quantity_milli"] == 3000


def test_provedennuyu_otmenit_nelzya(root_client, client_row):
    """Отменить проведённую значило бы объявить несостоявшимся состоявшееся."""
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/cancel", json={"note": ""})
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "waybill_is_final"
    assert ostatok(root_client, item) == 7_000, "отмена тронула склад"


def test_chernovik_otmenit_mozhno_i_sklad_ne_tronut(root_client, client_row):
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/cancel", json={"note": "передумали"})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "cancelled"
    assert ostatok(root_client, item) == 10_000


def test_obshchaya_smena_statusa_ne_beryot_nakladnuyu(root_client, client_row):
    """`POST /documents/{id}/status` закрыл бы бумагу, не тронув склад.

    Ровно так однажды закрывались заказы — статус менялся, товар оставался на
    полке, расхождение всплывало на инвентаризации. Накладная закрыта от этого
    сразу, а не после такого же случая.
    """
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    otvet = root_client.post(
        f"{API}/documents/{waybill['id']}/status", json={"status": "closed"}
    )
    assert otvet.status_code == 422, "накладную закрыли общей ручкой бланков"
    assert otvet.json()["error"]["code"] == "document_is_a_waybill"
    assert ostatok(root_client, item) == 10_000


# --- сторожа на ORM: обход службы -----------------------------------------------
#
# Проверки выше идут через API и стерегут службу. Эти идут МИМО неё — прямо по
# объектам сессии, как ходит соседний модуль или скрипт обслуживания. Довод тот
# же, что записан у журнала действий: службу обходят не злонамеренно, а по
# невнимательности, и обходят её ровно в тот заход, когда разбирают случай, ради
# которого бумага и велась.
#
# Граница у сторожей честная и здесь же названа: `text("UPDATE ...")` и любой
# клиент базы снаружи ими не закрыты. Проверять то, чего нет, нечем.


def provedennaya(root_client, client_row):
    """Проведённая накладная и её единственная строка."""
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    assert root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={}).status_code == 200
    return waybill, item


def test_storozh_ne_dayot_dopisat_stroku_mimo_sluzhby(root_client, client_row, db):
    waybill, item = provedennaya(root_client, client_row)

    db.add(
        DocumentLine(
            document_id=waybill["id"],
            product_id=item["id"],
            name_snapshot="дописано мимо службы",
            quantity_milli=1_000,
        )
    )
    with pytest.raises(errors.ForbiddenError) as otkaz:
        db.flush()
    assert otkaz.value.code == "waybill_immutable"
    db.rollback()

    assert len(root_client.get(f"{WAYBILLS}/{waybill['id']}").json()["lines"]) == 1


def test_storozh_ne_dayot_popravit_stroku_mimo_sluzhby(root_client, client_row, db):
    waybill, _ = provedennaya(root_client, client_row)
    stroka = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == waybill["id"])
    ).one()

    stroka.quantity_milli = 99_000
    with pytest.raises(errors.ForbiddenError) as otkaz:
        db.flush()
    assert otkaz.value.code == "waybill_immutable"
    db.rollback()

    posle = root_client.get(f"{WAYBILLS}/{waybill['id']}").json()
    assert posle["lines"][0]["quantity_milli"] == 3_000


def test_storozh_ne_dayot_udalit_stroku_mimo_sluzhby(root_client, client_row, db):
    waybill, _ = provedennaya(root_client, client_row)
    stroka = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == waybill["id"])
    ).one()

    db.delete(stroka)
    with pytest.raises(errors.ForbiddenError) as otkaz:
        db.flush()
    assert otkaz.value.code == "waybill_immutable"
    db.rollback()

    assert len(root_client.get(f"{WAYBILLS}/{waybill['id']}").json()["lines"]) == 1


def test_sebestoimost_pishetsya_odin_raz(root_client, client_row, db):
    """Самая узкая щель во всём стороже — и потому проверяется отдельно.

    Проведение снимает себестоимость ПОСЛЕ смены статуса, то есть пишет в
    строку уже непроведённой бумаги. Запрет в лоб отказал бы в самом проведении,
    поэтому в стороже назван один законный переход: `NULL → число`.

    Проверяется он с обеих сторон, иначе исключение незаметно превращается в
    дыру: первая половина — проведение прошло и себестоимость записана (значит
    щель открыта там, где нужно); вторая — «число → другое число» отбито
    (значит щель не шире, чем объявлено).
    """
    waybill, item = provedennaya(root_client, client_row)
    stroka = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == waybill["id"])
    ).one()
    # Сверяем с карточкой товара, а не с числом в тексте: себестоимость
    # хранится в минорных единицах, и записанная сюда «сотня» разошлась бы
    # с ней молча при первой же смене единиц.
    assert stroka.cost_minor == item["cost"], "проведение не записало себестоимость"

    stroka.cost_minor = 1
    with pytest.raises(errors.ForbiddenError) as otkaz:
        db.flush()
    assert otkaz.value.code == "waybill_immutable"
    db.rollback()


def test_storozh_ne_dayot_popravit_shapku(root_client, client_row, db):
    """Шапка проведённой не меняется вовсе — кроме статуса, и не через объект.

    Статус исключён не послаблением: законный путь у него один —
    `documents_repo.take_status`, условный `UPDATE` запросом. Он уезжает мимо
    мэппера, а значит мимо этого сторожа, и приёмка (`issued → closed`) им не
    задета. Проверяется тут же, второй половиной.
    """
    waybill, _ = provedennaya(root_client, client_row)
    bumaga = db.get(Document, waybill["id"])

    bumaga.client_id = None
    with pytest.raises(errors.ForbiddenError) as otkaz:
        db.flush()
    assert otkaz.value.code == "waybill_immutable"
    db.rollback()

    assert root_client.post(f"{WAYBILLS}/{waybill['id']}/confirm", json={}).status_code == 200
    assert root_client.get(f"{WAYBILLS}/{waybill['id']}").json()["status"] == "closed"


def test_massovuyu_pravku_strok_storozh_ne_propuskaet(root_client, client_row, db):
    """Три сторожа выше стоят на ОБЪЕКТЕ, массовый запрос объектов не трогает.

    Ровно эта дыра была у журнала действий: одна строка из соседней службы
    переписывала таблицу целиком, мимо мэппера и мимо всех запретов. Здесь она
    закрыта так же — на сессии.
    """
    waybill, _ = provedennaya(root_client, client_row)

    with pytest.raises(errors.ForbiddenError):
        db.execute(
            update(DocumentLine)
            .where(DocumentLine.document_id == waybill["id"])
            .values(quantity_milli=1)
        )
    db.rollback()

    with pytest.raises(errors.ForbiddenError):
        db.execute(delete(DocumentLine).where(DocumentLine.document_id == waybill["id"]))
    db.rollback()

    posle = root_client.get(f"{WAYBILLS}/{waybill['id']}").json()
    assert posle["lines"][0]["quantity_milli"] == 3_000


def test_storozh_ne_shire_chem_obyavlen(root_client, client_row, db):
    """Две половины одного вопроса: не задел ли сторож лишнего.

    Сторож, отбивающий заодно черновики и заказы, покраснел бы не сразу, а на
    первой же правке чужого бланка — и разбирались бы с ним как с поломкой
    заказов, а не как с перегнутым запретом. Поэтому обе законные правки
    проверяются здесь, рядом с запретами.
    """
    item = product(root_client, stock="10")

    # Черновик накладной правится и мимо службы: запрет про СТАТУС, а не про вид.
    waybill = chernovik(root_client, client_row, item, quantity="3")
    stroka = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == waybill["id"])
    ).one()
    stroka.quantity_milli = 4_000
    db.flush()
    db.rollback()

    # Строка заказа — тем более: заказ не накладная, у него своя жизнь.
    zakaz = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}
    ).json()
    root_client.post(
        f"{ORDERS}/{zakaz['id']}/lines", json={"product_id": item["id"], "quantity": "2"}
    )
    stroka_zakaza = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == zakaz["id"])
    ).one()
    stroka_zakaza.quantity_milli = 5_000
    db.flush()
    db.rollback()


# --- сторнирование ------------------------------------------------------------


def test_storno_vozvrashchaet_tovar_i_hranit_oba_dvizheniya(root_client, client_row):
    """Склад обязан помнить, что уходило и что вернулось.

    Сотри мы прежнее движение — остаток сошёлся бы, а вопрос «куда делись три
    штуки» остался бы без ответа. Проверка смотрит не только на остаток, но и на
    ЧИСЛО движений: сошедшийся остаток при одном движении означал бы правку.
    """
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert ostatok(root_client, item) == 7_000

    storno = root_client.post(f"{WAYBILLS}/{waybill['id']}/reverse")
    assert storno.status_code == 201, storno.text
    storno = storno.json()
    assert storno["kind"] == "waybill_in", "сторно расходной обязано быть приходным"
    assert storno["status"] == "draft", "сторно рождается черновиком: возврат бывает частичным"
    assert storno["basis_id"] == waybill["id"]

    assert root_client.post(f"{WAYBILLS}/{storno['id']}/post", json={}).status_code == 200
    assert ostatok(root_client, item) == 10_000

    dvizheniya = root_client.get(f"{STOCK}/moves", params={"product_id": item["id"]}).json()
    po_bumagam = [m for m in dvizheniya["items"] if m.get("document_id")]
    assert len(po_bumagam) == 2, (
        f"движений по бумагам должно быть два (отгрузка и возврат), а их "
        f"{len(po_bumagam)}: {po_bumagam}"
    )


def test_storno_chernovika_nelzya(root_client, client_row):
    """Сторнировать нечего: по черновику ничего не происходило."""
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/reverse")
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "waybill_not_posted"


def test_vtoroe_storno_otkazyvaet(root_client, client_row):
    """Второе сторно по той же бумаге — почти всегда двойное нажатие."""
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    assert root_client.post(f"{WAYBILLS}/{waybill['id']}/reverse").status_code == 201
    vtoroe = root_client.post(f"{WAYBILLS}/{waybill['id']}/reverse")
    assert vtoroe.status_code == 422
    assert vtoroe.json()["error"]["code"] == "waybill_already_reversed"


# --- один путь к остатку ------------------------------------------------------


def test_nakladnaya_po_zakrytomu_zakazu_ne_provoditsya(root_client, client_row):
    """Заказ уже двинул склад — накладная двинула бы его второй раз."""
    item = product(root_client, stock="10")
    zakaz = root_client.post(ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(
        f"{ORDERS}/{zakaz['id']}/lines", json={"product_id": item["id"], "quantity": "3"}
    )
    assert root_client.post(f"{ORDERS}/{zakaz['id']}/close", json={}).status_code == 200
    assert ostatok(root_client, item) == 7_000

    nakladnaya = root_client.post(f"{WAYBILLS}/from-order/{zakaz['id']}")
    assert nakladnaya.status_code == 201, nakladnaya.text
    otvet = root_client.post(f"{WAYBILLS}/{nakladnaya.json()['id']}/post", json={})
    assert otvet.status_code == 422, "товар уехал бы дважды"
    assert otvet.json()["error"]["code"] == "basis_already_shipped"
    assert ostatok(root_client, item) == 7_000


def test_zakaz_s_provedennoy_nakladnoy_ne_zakryvaetsya(root_client, client_row):
    """Вторая половина того же запрета, с другой стороны.

    Проверять надо обе: закрой мы одну сторону, и обход остался бы очевидным —
    просто сделать то же самое в другом порядке.
    """
    item = product(root_client, stock="10")
    zakaz = root_client.post(ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(
        f"{ORDERS}/{zakaz['id']}/lines", json={"product_id": item["id"], "quantity": "3"}
    )
    nakladnaya = root_client.post(f"{WAYBILLS}/from-order/{zakaz['id']}").json()
    assert root_client.post(f"{WAYBILLS}/{nakladnaya['id']}/post", json={}).status_code == 200
    assert ostatok(root_client, item) == 7_000

    otvet = root_client.post(f"{ORDERS}/{zakaz['id']}/close", json={})
    assert otvet.status_code == 422, "товар уехал бы дважды"
    assert otvet.json()["error"]["code"] == "already_shipped_by_waybill"
    assert ostatok(root_client, item) == 7_000


def test_perenos_pozitsiy_iz_zakaza_otbrasyvaet_uslugi(root_client, client_row):
    """Услугу нельзя отгрузить, и строкой в накладной ей делать нечего."""
    tovar = product(root_client, stock="10")
    usluga = product(root_client, service=True)
    zakaz = root_client.post(ORDERS, json={"kind": "sales_order", "client_id": client_row["id"]}).json()
    root_client.post(f"{ORDERS}/{zakaz['id']}/lines", json={"product_id": tovar["id"], "quantity": "2"})
    root_client.post(f"{ORDERS}/{zakaz['id']}/lines", json={"product_id": usluga["id"], "quantity": "1"})

    nakladnaya = root_client.post(f"{WAYBILLS}/from-order/{zakaz['id']}")
    assert nakladnaya.status_code == 201, nakladnaya.text
    stroki = nakladnaya.json()["lines"]
    assert len(stroki) == 1, f"услуга уехала в накладную: {stroki}"
    assert stroki[0]["product_id"] == tovar["id"]


# --- подтверждение приёмки ----------------------------------------------------


def test_podtverzhdenie_nichego_ne_dvigaet(root_client, client_row):
    """«Отгружено» и «принято» — разные факты, но склад двигает только первый."""
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/confirm", json={"note": "принял Иванов"})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "closed"
    assert ostatok(root_client, item) == 7_000


def test_podtverdit_chernovik_nelzya(root_client, client_row):
    item = product(root_client, stock="10")
    waybill = chernovik(root_client, client_row, item, quantity="3")
    otvet = root_client.post(f"{WAYBILLS}/{waybill['id']}/confirm", json={"note": ""})
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "waybill_not_posted"


# --- имя автора снимком -------------------------------------------------------


def test_imya_avtora_ostayotsya_posle_uvolneniya(root_client, client_row):
    """История обязана отвечать, кто отпустил товар, и через год тоже.

    `author_id` объявлен SET NULL — и правильно объявлен: удаление сотрудника не
    должно уносить историю бумаги. Но вместе со ссылкой пропадало и имя, причём
    задним числом, по всем прошлым записям сразу. Заметить это можно было только
    уволив человека — что здесь и делается.
    """
    # СВОЙ сотрудник, а не общий `manager_client`.
    #
    # Проверка увольняет автора, а `manager_client` — фикстура уровня сессии:
    # уволив его, мы оставляем без действующей сессии все последующие проверки
    # во всех файлах, которые к нему обращаются. Поймано ровно так — соседний
    # `tests/test_audit.py` посыпался `KeyError: 'id'` на своих фикстурах.
    from tests.conftest import make_manager

    kladovshchik = make_manager(root_client, f"kladovshchik-{uniq()}@test.local")
    ya = kladovshchik.get(f"{API}/auth/me").json()
    # Менеджеру права на накладные не выдаются по умолчанию: их даёт роль, а
    # роль настраивают под установку. Выдаём здесь, потому что проверка не про
    # права — она про то, переживает ли имя увольнение.
    rol = root_client.post(
        f"{API}/roles",
        json={
            "name": f"Кладовщик {uniq()}",
            "permissions": ["waybills.view", "waybills.create", "waybills.edit"],
        },
    )
    assert rol.status_code == 201, rol.text
    naznachena = root_client.post(
        f"{API}/roles/assign/{ya['id']}", json={"role_id": rol.json()["id"]}
    )
    assert naznachena.status_code == 200, naznachena.text

    created = kladovshchik.post(
        WAYBILLS, json={"kind": "waybill_out", "client_id": client_row["id"]}
    )
    assert created.status_code == 201, created.text
    waybill = created.json()

    udalyon = root_client.delete(f"{API}/staff/{ya['id']}")
    assert udalyon.status_code in (200, 204), udalyon.text

    istoriya = root_client.get(f"{API}/documents/{waybill['id']}").json()["events"]
    imena = [z.get("author_name") for z in istoriya]
    assert any(imya == ya["name"] for imya in imena), (
        f"имя автора пропало вместе с сотрудником: {istoriya}"
    )


# --- блок выключается целиком -------------------------------------------------


def test_vyklyuchennyy_blok_zakryvaet_adresa(root_client, client_row):
    """Выключенный блок исчезает целиком, включая прямые адреса."""
    root_client.post(f"{API}/modules/waybills", json={"enabled": False})
    try:
        otvet = root_client.get(WAYBILLS)
        assert otvet.status_code == 403
        assert otvet.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{API}/modules/waybills", json={"enabled": True})


def test_iz_zakaza_postavshchiku_vykhodit_prikhodnaya(root_client, client_row):
    """Вид накладной берётся у заказа, а не подставляется расходной.

    Прежде `po_zakazu` создавал расходную безусловно: из заказа ПОСТАВЩИКУ
    выходила бумага на отгрузку, и проведение снимало товар со склада вместо
    того, чтобы принять его. Остаток при этом сходился бы сам с собой —
    расхождение всплыло бы только на инвентаризации, когда концов уже не
    найти.

    Проверяется по ОСТАТКУ, а не по виду бумаги: вид можно поправить и
    ошибиться заново, а склад врать не станет.
    """
    item = product(root_client, stock="10")
    zakaz = root_client.post(
        ORDERS, json={"kind": "purchase_order", "client_id": client_row["id"]}
    ).json()
    root_client.post(
        f"{ORDERS}/{zakaz['id']}/lines", json={"product_id": item["id"], "quantity": "3"}
    )

    nakladnaya = root_client.post(f"{WAYBILLS}/from-order/{zakaz['id']}")
    assert nakladnaya.status_code == 201, nakladnaya.text
    assert nakladnaya.json()["kind"] == "waybill_in", (
        "из заказа поставщику вышла расходная накладная"
    )

    assert root_client.post(f"{WAYBILLS}/{nakladnaya.json()['id']}/post", json={}).status_code == 200
    assert ostatok(root_client, item) == 13_000, (
        "приёмка от поставщика списала товар вместо того, чтобы принять"
    )


def test_osnovaniem_nakladnoy_byvaet_tolko_zakaz(root_client, client_row):
    """Квитанция, акт и другая накладная основанием быть не могут.

    Прежде брался любой бланк, и выходила накладная «по основанию», которое
    основанием не является: двойная отгрузка по нему не сторожится, закрытие
    заказа его не видит, а на бумаге написано, что она по нему выписана.
    """
    # Другая накладная — самый близкий к правде случай: тот же вид бланка, те
    # же строки, а основанием быть не может.
    chuzhaya = root_client.post(
        WAYBILLS, json={"kind": "waybill_out", "client_id": client_row["id"]}
    )
    assert chuzhaya.status_code == 201, chuzhaya.text
    otvet = root_client.post(f"{WAYBILLS}/from-order/{chuzhaya.json()['id']}")
    assert otvet.status_code == 422, "накладная сошла за основание накладной"
    assert otvet.json()["error"]["code"] == "basis_is_not_order"


def test_bumaga_bez_sklada_ne_dvigaet_ostatok_ni_pri_kakom_bloke(root_client, client_row):
    """НАЙДЕНО ПРИЁМКОЙ: остаток надувался из ничего.

    Заказ закрыт при ВЫКЛЮЧЕННОМ складе — накладная выписана, склада у неё нет,
    движений не было. Склад включают обратно и жмут «отменить проведение».
    Сторно копировало пустой склад в `create`, а тот при включённом блоке
    подставлял ОСНОВНОЙ, и `provesti` писал приход: товар, который никуда не
    уезжал, возвращался на полку. Остаток рос на ровном месте.

    Правило теперь одно и самоописательное: склад у бумаги решается при
    заведении, и бумага без склада остатка не касается — ни при проведении, ни
    при сторнировании.
    """
    item = product(root_client, stock="10")
    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": False}).status_code == 200
    try:
        waybill = chernovik(root_client, client_row, item, quantity="3")
        provedena = root_client.post(f"{WAYBILLS}/{waybill['id']}/post", json={})
        assert provedena.status_code == 200, provedena.text
        assert provedena.json()["warehouse_id"] is None, "склад выключен, а у бумаги он есть"
    finally:
        assert root_client.post(f"{API}/modules/warehouse", json={"enabled": True}).status_code == 200

    assert ostatok(root_client, item) == 10_000, "выключенный склад всё-таки тронули"

    storno = root_client.post(f"{WAYBILLS}/{waybill['id']}/reverse")
    assert storno.status_code == 201, storno.text
    provedeno = root_client.post(f"{WAYBILLS}/{storno.json()['id']}/post", json={})
    assert provedeno.status_code == 200, provedeno.text

    assert ostatok(root_client, item) == 10_000, (
        "сторно бумаги, которая склада не касалась, вернуло товар на полку — "
        "остаток вырос из ничего"
    )
