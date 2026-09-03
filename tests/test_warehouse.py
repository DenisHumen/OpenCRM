"""Склад: проверяем то, из-за чего он потом не сойдётся.

Склад ломается не там, где не нажимается кнопка, а там, где число на экране
перестаёт соответствовать полке. Поэтому здесь почти нет тестов «ответ 200» —
есть тесты на арифметику остатка, на неубиваемость истории и на то, что
выключенный блок закрыт целиком.
"""

import re

import pytest
from fastapi.testclient import TestClient

from core import exceptions as errors
from core.services import modules_service, warehouse_service
from tests.conftest import API

WH = f"{API}/warehouse"


@pytest.fixture(scope="module", autouse=True)
def warehouse_on(root_client: TestClient):
    """Склад по умолчанию выключен — для остальных тестов включаем.

    Заодно это проверка, что включение вообще работает: без него весь файл
    посыпался бы на 403, и было бы не видно, в чём дело.
    """
    response = root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert any(m["key"] == "warehouse" and m["enabled"] for m in response.json()["items"])
    yield
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    modules_service.invalidate()


@pytest.fixture(scope="module")
def deal_id(root_client: TestClient) -> int:
    """Заявка, под которую списывают товар."""
    client = root_client.post(f"{API}/clients", json={"name": "Заказчик со складом"}).json()
    deal = root_client.post(
        f"{API}/deals", json={"title": "Ремонт витрины", "client_id": client["id"]}
    )
    assert deal.status_code == 201, deal.text
    return deal.json()["id"]


def new_product(client: TestClient, **fields) -> dict:
    # Деньги — целыми в минимальных единицах, как во всём проекте: 120.00 → 12000
    payload = {"name": "Плёнка матовая", "unit": "kg", "cost": 12000, "price": 20000}
    payload.update(fields)
    response = client.post(f"{WH}/products", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def move(client: TestClient, product_id: int, kind: str, quantity, **fields) -> dict:
    payload = {"product_id": product_id, "kind": kind, "quantity": quantity}
    payload.update(fields)
    response = client.post(f"{WH}/moves", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_stock_is_the_sum_of_moves(root_client):
    """Остаток — это сумма движений, и никакого другого остатка не существует."""
    product = new_product(root_client, name="Гайка М8", unit="pcs", sku="N-M8")

    move(root_client, product["id"], "in", 100)
    move(root_client, product["id"], "out", 30)
    move(root_client, product["id"], "in", 12)
    move(root_client, product["id"], "writeoff", 2)
    move(root_client, product["id"], "return", 5)

    # 100 − 30 + 12 − 2 + 5 = 85
    expected = 85 * 1000
    card = root_client.get(f"{WH}/products/{product['id']}").json()
    assert card["stock_milli"] == expected

    # то же число в списке и рядом с историей: три ответа не имеют права
    # расходиться, иначе оператор поверит тому, который ему нравится
    listed = root_client.get(f"{WH}/products?search=Гайка М8").json()["items"]
    assert [row["stock_milli"] for row in listed] == [expected]
    history = root_client.get(f"{WH}/products/{product['id']}/moves").json()
    assert history["stock_milli"] == expected
    assert sum(m["quantity_milli"] for m in history["items"]) == expected


def test_stock_leaves_the_repository_as_a_whole_number(root_client):
    """Остаток выходит из репозитория ЦЕЛЫМ, а не `Decimal` — всеми тремя путями.

    `SUM()` в MySQL возвращает `Decimal`, а не целое. Пока
    приведение не стояло в репозитории, `Decimal` доезжал до показа: строка
    остатка собирается через `f"{frac:03d}"` и падала `ValueError`ом, а ответ
    API уходил дробным числом — `jsonable_encoder` переводит `Decimal` во float,
    то есть ровно в то, через что количество и деньги проходить не должны.

    Проверяем типом, а не значением: значение сходилось и до починки, потому
    `Decimal` и прожил на боевом сервере незамеченным.
    """
    from database.repositories import warehouse as warehouse_repo
    from database.session import SessionLocal

    product = new_product(root_client, name="Целое число", unit="kg")
    move(root_client, product["id"], "in", "1.5")

    with SessionLocal() as db:
        ostatok = warehouse_repo.stock_of(db, product["id"])
        assert ostatok == 1500
        assert type(ostatok) is int, f"остаток приехал {type(ostatok).__name__}"

        pachkoy = warehouse_repo.stock_by_product(db, [product["id"]])
        assert type(pachkoy[product["id"]]) is int
        po_skladam = warehouse_repo.stock_by_warehouse(db, [product["id"]])
        assert all(type(v) is int for v in po_skladam[product["id"]].values())


def test_fractional_quantities_survive_a_hundred_operations(root_client):
    """Дробные килограммы не должны копить ошибку: 1.0 − 0.1×10 = ровно 0.

    Это и есть защита от «списать дважды одно и то же»: останься на остатке
    хвостик в тысячную долю, кладовщик увидел бы товар, которого нет, и списал
    бы его ещё раз.
    """
    product = new_product(root_client, name="Кофе зерно", unit="kg")
    move(root_client, product["id"], "in", "1.0")
    for _ in range(10):
        move(root_client, product["id"], "out", "0.1")

    card = root_client.get(f"{WH}/products/{product['id']}").json()
    assert card["stock_milli"] == 0, "дробные списания разошлись с приходом"

    # одиннадцатое списание уже уходит в минус, а не проходит «бесплатно»
    result = move(root_client, product["id"], "out", "0.1")
    assert result["went_negative"] is True
    assert result["stock_milli"] == -100


def test_too_precise_quantity_is_rejected_instead_of_rounded(root_client):
    """Лишние знаки — отказ, а не тихое округление.

    Округли сервер 0.3335 до 0.334 — и остаток разойдётся с накладной на грамм,
    причём молча. Ошибку ввода исправляет человек, а не сервер за него.
    """
    product = new_product(root_client, name="Сахар", unit="kg")
    response = root_client.post(
        f"{WH}/moves", json={"product_id": product["id"], "kind": "in", "quantity": "0.3335"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "quantity_too_precise"
    assert root_client.get(f"{WH}/products/{product['id']}").json()["stock_milli"] == 0


def test_stock_can_go_negative_but_says_so(root_client):
    """Минус разрешён и помечен: товар отдали, приход занести забыли.

    Запрет заставил бы либо не записывать расход (склад разойдётся с реальностью
    навсегда), либо выдумать приход (себестоимость станет фальшивой).
    Обоснование — в warehouse_service.add_move.
    """
    product = new_product(root_client, name="Шуруп 3×20", unit="pcs")
    move(root_client, product["id"], "in", 10)

    result = move(root_client, product["id"], "out", 25)
    assert result["went_negative"] is True
    assert result["stock_milli"] == -15 * 1000

    # приход задним числом чинит остаток сам, без правки истории
    fixed = move(root_client, product["id"], "in", 40)
    assert fixed["went_negative"] is False
    assert fixed["stock_milli"] == 25 * 1000


def test_deleting_a_product_keeps_its_moves(root_client):
    """Удаление товара не стирает историю движений.

    История списаний — это себестоимость прошлых заявок. Пропади она вместе с
    карточкой, и прошлые заявки бесшумно подешевели бы.
    """
    product = new_product(root_client, name="Лента ПВХ", unit="m")
    move(root_client, product["id"], "in", 50)
    move(root_client, product["id"], "out", 20)

    assert root_client.delete(f"{WH}/products/{product['id']}").status_code == 200
    assert root_client.get(f"{WH}/products?search=Лента ПВХ").json()["items"] == []

    history = root_client.get(f"{WH}/products/{product['id']}/moves").json()
    assert history["total"] == 2
    assert history["stock_milli"] == 30 * 1000

    restored = root_client.post(f"{WH}/products/{product['id']}/restore").json()
    assert restored["stock_milli"] == 30 * 1000


def test_hard_delete_of_a_product_with_history_is_refused_by_the_database(root_client):
    """Последний рубеж: даже прямой DELETE в базе не унесёт историю с собой.

    Мягкое удаление — договорённость приложения, а внешний ключ RESTRICT — то,
    что остаётся, когда базу чистят руками или скриптом.
    """
    from sqlalchemy.exc import IntegrityError

    from database.models import Product
    from database.session import SessionLocal

    product = new_product(root_client, name="Штапик", unit="m")
    move(root_client, product["id"], "in", 7)

    db = SessionLocal()
    try:
        db.delete(db.get(Product, product["id"]))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()

    assert root_client.get(f"{WH}/products/{product['id']}/moves").json()["total"] == 1


def test_uslugu_v_vydannoy_bumage_baza_udalit_ne_dast(root_client):
    """Довод «прямого удаления не бывает» держался на движениях склада.

    У УСЛУГИ движений не бывает вовсе, и `RESTRICT` на `stock_moves` её не
    держит. Пока строка бумаги стояла `SET NULL`, прямой `DELETE` проходил и
    молча превращал товарную строку ВЫДАННОЙ бумаги в разовую позицию — мимо
    сторожа неизменяемости, потому что тот стоит событиями ORM, а `ON DELETE`
    исполняет сама база.
    """
    from sqlalchemy.exc import IntegrityError

    from database.models import Product
    from database.session import SessionLocal

    for klyuch in ("documents", "orders"):
        root_client.post(f"{API}/modules/{klyuch}", json={"enabled": True})
    usluga = new_product(
        root_client, name="Выезд мастера в бумаге", unit="hour", is_service=True, cost=None
    )
    klient = root_client.post(f"{API}/clients", json={"name": "Держатель услуги"}).json()
    zakaz = root_client.post(
        f"{API}/orders", json={"kind": "sales_order", "client_id": klient["id"]}
    )
    assert zakaz.status_code == 201, zakaz.text
    stroka = root_client.post(
        f"{API}/orders/{zakaz.json()['id']}/lines",
        json={"product_id": usluga["id"], "quantity": "1", "price": 10000},
    )
    assert stroka.status_code == 201, stroka.text

    db = SessionLocal()
    try:
        db.delete(db.get(Product, usluga["id"]))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()

def test_a_service_has_no_stock_at_all(root_client):
    """У услуги остаток не ноль, а его нет — и оприходовать её нельзя."""
    service = new_product(
        root_client, name="Выезд мастера", unit="hour", is_service=True, cost=None
    )
    assert service["stock_milli"] is None
    assert service["low_stock"] is False

    response = root_client.post(
        f"{WH}/moves", json={"product_id": service["id"], "kind": "in", "quantity": 1}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "service_has_no_stock"

    listed = root_client.get(f"{WH}/products?search=Выезд мастера").json()["items"]
    assert listed[0]["stock_milli"] is None


def test_aggregate_is_not_capped_by_the_page_size(root_client):
    """Остаток считается запросом, а не сложением загруженной страницы.

    Движений здесь больше, чем максимальный `per_page` (200). Сложи кто-нибудь
    выбранные объекты — остаток занизился бы ровно на хвост, и выглядело бы это
    совершенно правдоподобно.
    """
    product = new_product(root_client, name="Крепёж навалом", unit="pcs")
    count = 250
    for _ in range(count):
        move(root_client, product["id"], "in", 1)

    card = root_client.get(f"{WH}/products/{product['id']}").json()
    assert card["stock_milli"] == count * 1000

    page = root_client.get(f"{WH}/products/{product['id']}/moves?per_page=200").json()
    assert page["total"] == count
    assert len(page["items"]) == 200, "страница должна быть короче полной истории"
    assert page["stock_milli"] == count * 1000
    assert sum(m["quantity_milli"] for m in page["items"]) < page["stock_milli"]


def test_low_stock_warning_uses_the_threshold(root_client):
    product = new_product(root_client, name="Картридж", unit="pcs", min_stock=5)
    move(root_client, product["id"], "in", 10)
    assert root_client.get(f"{WH}/products/{product['id']}").json()["low_stock"] is False

    move(root_client, product["id"], "out", 6)
    card = root_client.get(f"{WH}/products/{product['id']}").json()
    assert card["stock_milli"] == 4 * 1000
    assert card["low_stock"] is True

    low = root_client.get(f"{WH}/products?low_only=true&per_page=200").json()["items"]
    assert product["id"] in [row["id"] for row in low]


def test_money_stays_in_minor_units(root_client):
    """Цены — целые копейки; «цену не назвали» это NULL, а не ноль."""
    product = new_product(root_client, name="Краска", unit="l", price=125050, cost=90000)
    assert product["price"] == 125050
    assert product["cost"] == 90000

    no_price = new_product(root_client, name="Образец", unit="pcs", price=None, cost=None)
    assert no_price["price"] is None
    assert no_price["cost"] is None


def test_writeoff_against_a_deal_carries_its_cost(root_client, deal_id):
    """Списание под заявку: движение с deal_id и снимком себестоимости."""
    product = new_product(root_client, name="Профиль алюм.", unit="m", cost=15000)
    move(root_client, product["id"], "in", 100)
    move(root_client, product["id"], "out", 4, deal_id=deal_id)

    # себестоимость снята на момент движения, а не подтянута сейчас
    root_client.patch(f"{WH}/products/{product['id']}", json={"cost": 99900})

    linked = root_client.get(f"{WH}/moves?deal_id={deal_id}").json()
    assert linked["total"] == 1
    assert linked["items"][0]["cost"] == 15000
    assert linked["items"][0]["quantity_milli"] == -4000
    assert linked["items"][0]["product_name"] == "Профиль алюм."
    # 4 м × 150.00 = 600.00
    assert linked["cost"] == 60000


def test_move_for_a_missing_deal_is_a_clear_404(root_client):
    """Несуществующая заявка — понятный отказ, а не 500 от внешнего ключа."""
    product = new_product(root_client, name="Уголок", unit="pcs")
    response = root_client.post(
        f"{WH}/moves",
        json={"product_id": product["id"], "kind": "out", "quantity": 1, "deal_id": 10_000_000},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "deal_not_found"


def test_deleting_a_deal_keeps_the_stock_history(root_client):
    """Заявку удалили — товар со склада не вернулся.

    Внешний ключ стоит на SET NULL именно поэтому: унеси удаление заявки
    движение с собой, остаток вырос бы сам собой и никто бы не понял почему.
    """
    client = root_client.post(f"{API}/clients", json={"name": "Передумал"}).json()
    deal = root_client.post(
        f"{API}/deals", json={"title": "Отменённая", "client_id": client["id"]}
    ).json()
    product = new_product(root_client, name="Саморез", unit="pcs")
    move(root_client, product["id"], "in", 20)
    move(root_client, product["id"], "out", 5, deal_id=deal["id"])

    assert root_client.delete(f"{API}/deals/{deal['id']}").status_code == 200

    card = root_client.get(f"{WH}/products/{product['id']}").json()
    assert card["stock_milli"] == 15 * 1000, "остаток изменился от удаления заявки"
    assert root_client.get(f"{WH}/products/{product['id']}/moves").json()["total"] == 2


def test_svoy_artikul_unikalen(root_client):
    new_product(root_client, name="Товар с артикулом", sku="A-100")
    clash = root_client.post(f"{WH}/products", json={"name": "Другой", "sku": "A-100"})
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "sku_taken"


def test_artikul_vydayotsya_sam(root_client):
    """Не назвали артикул — выдаём. Разбор: docs/19-sborka-zakaza.md §Р1."""
    pervyy = new_product(root_client, name="Без артикула 1", sku=None)
    vtoroy = new_product(root_client, name="Без артикула 2", sku="   ")

    for tovar in (pervyy, vtoroy):
        assert re.fullmatch(r"A-\d{6}", tovar["sku"]), tovar["sku"]
    assert int(vtoroy["sku"][2:]) == int(pervyy["sku"][2:]) + 1, "номер не сдвинулся"


def test_nash_diapazon_rukami_ne_nabirayut(root_client):
    """`A-999999` руками — это отказ в обслуживании всему складу.

    Счётчик выдачи берёт максимум по образцу `A-` плюс шесть цифр и упирается
    в потолок: один такой товар, заведённый правом `warehouse.create`, навсегда
    закрывает заведение БЕЗ артикула — всем, включая root, и молча.
    """
    otkaz = root_client.post(
        f"{WH}/products", json={"name": "Подделка у потолка", "sku": "A-999999"}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "sku_reserved"

    # А выдача после этого обязана работать.
    posle = new_product(root_client, name="После отказа", sku=None)
    assert re.fullmatch(r"A-\d{6}", posle["sku"]), posle["sku"]


def test_svoy_vydannyy_artikul_pravitsya_bez_otkaza(root_client):
    """Запрет выше не должен запирать правку собственной карточки."""
    tovar = new_product(root_client, name="Выданный", sku=None)
    otvet = root_client.patch(
        f"{WH}/products/{tovar['id']}",
        json={"name": "Переименован", "sku": tovar["sku"]},
    )
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["sku"] == tovar["sku"]

def test_chuzhoy_artikul_ne_dvigaet_schyotchik(root_client):
    """`A-100` похож на наш, но короче: считать его выданным нельзя.

    Иначе счётчик прыгнул бы на сто, а следом за ним — все будущие артикулы.
    """
    do = new_product(root_client, name="До чужого", sku=None)
    new_product(root_client, name="Чужой вид", sku="ZX-9")
    new_product(root_client, name="Похожий вид", sku="A-777")
    posle = new_product(root_client, name="После чужого", sku=None)

    assert int(posle["sku"][2:]) == int(do["sku"][2:]) + 1


def test_poddelki_ne_syedayut_poisk_maksimuma(root_client):
    """Похожий на наш, но не наш артикул не имеет права сбить счётчик.

    `A-0000420` — семь цифр вместо шести — при побайтном сравнении стоит ВЫШЕ
    любого нашего и остаётся в том же диапазоне. Пока максимум искали по верхним
    полусотне строк, полсотни таких заслонили бы настоящий: счёт пошёл бы с
    единицы, упёрся в занятое, и заведение товара отказало бы при полностью
    свободном диапазоне — а виноват был бы артикул, набранный руками год назад.
    """
    # Сперва уводим настоящий максимум подальше от единицы. Иначе проверка
    # проходит и на сломанном отборе: счётчик вернул бы ноль, а `insert_retrying`
    # своими тремя попытками дотянулся бы до нужного номера — и беда осталась бы
    # незамеченной ровно там, где её искали.
    nash = new_product(root_client, name="До подделок", sku=None)
    while int(nash["sku"][2:]) < 5:
        nash = new_product(root_client, name="До подделок", sku=None)
    for n in range(50):
        new_product(root_client, name=f"Подделка {n}", sku=f"{nash['sku']}{n:02d}")

    posle = root_client.post(f"{WH}/products", json={"name": "После подделок"})
    assert posle.status_code == 201, f"артикул не выдался вовсе: {posle.text}"
    assert int(posle.json()["sku"][2:]) == int(nash["sku"][2:]) + 1, (
        f"счётчик сбился подделками: {posle.json()['sku']} после {nash['sku']}"
    )


def test_artikul_udalyonnogo_ne_vydayotsya_zanovo(root_client):
    """Удалили товар — артикул остаётся занятым: этикетка уже на коробке."""
    ushedshiy = new_product(root_client, name="Товар на удаление", sku=None)
    assert root_client.delete(f"{WH}/products/{ushedshiy['id']}").status_code == 200

    novyy = new_product(root_client, name="Товар после удаления", sku=None)
    assert novyy["sku"] != ushedshiy["sku"]
    assert int(novyy["sku"][2:]) > int(ushedshiy["sku"][2:])


def test_artikul_nelzya_stereet(root_client):
    """Пустой артикул при правке — отказ, а не NULL: он уже на коробке."""
    tovar = new_product(root_client, name="Товар с выданным", sku=None)
    otvet = root_client.patch(f"{WH}/products/{tovar['id']}", json={"sku": "  "})
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "sku_required"
    assert root_client.get(f"{WH}/products/{tovar['id']}").json()["sku"] == tovar["sku"]


def test_zero_quantity_move_is_refused(root_client):
    product = new_product(root_client, name="Ничего", unit="pcs")
    response = root_client.post(
        f"{WH}/moves", json={"product_id": product["id"], "kind": "in", "quantity": 0}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "zero_quantity"


def test_disabled_module_closes_the_api(root_client):
    """Выключенный блок закрывает адреса целиком, а не отдаёт пустой список."""
    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": False}).status_code == 200
    try:
        for method, path in (
            ("get", f"{WH}/products"),
            ("get", f"{WH}/moves"),
            ("post", f"{WH}/products"),
        ):
            response = getattr(root_client, method)(
                path, **({} if method == "get" else {"json": {}})
            )
            assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
            assert response.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})


def test_switching_the_warehouse_off_does_not_touch_anything_else(root_client):
    """Ради этого блоки и разделены: выключили склад — работа не встала."""
    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": False}).status_code == 200
    try:
        for path in ("/clients", "/deals", "/documents", "/tasks", "/dashboard"):
            alive = root_client.get(f"{API}{path}")
            assert alive.status_code == 200, f"{path} слёг из-за выключенного склада: {alive.text}"
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})


def test_data_survives_switching_the_module_off_and_on(root_client):
    """Выключение — «убрать с глаз», а не «стереть»: остаток обязан вернуться."""
    product = new_product(root_client, name="Пережить выключение", unit="pcs")
    move(root_client, product["id"], "in", 9)

    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})

    back = root_client.get(f"{WH}/products/{product['id']}").json()
    assert back["stock_milli"] == 9 * 1000
    assert back["name"] == "Пережить выключение"


def test_absurd_quantity_is_refused_not_a_crash(root_client):
    """Число, которое не влезает в базу, — отказ, а не пятисотка.

    У количества, в отличие от денег, потолка не было вовсе. «10^30» в поле (то
    есть обычная опечатка или вставка мимо поля) доходило до вставки и роняло
    запрос переполнением в драйвере — пятисоткой на пользовательский ввод, без
    единого слова о том, что не так.
    """
    product = new_product(root_client, name="Гвозди", unit="kg")
    for absurd in ("9" * 30, "1e20", str(2**63)):
        response = root_client.post(
            f"{WH}/moves", json={"product_id": product["id"], "kind": "in", "quantity": absurd}
        )
        assert response.status_code == 422, f"{absurd}: {response.status_code}"
        assert response.json()["error"]["code"] == "quantity_too_large", absurd
    assert root_client.get(f"{WH}/products/{product['id']}").json()["stock_milli"] == 0

    # тот же разбор стоит на пороге предупреждения о нехватке
    threshold = root_client.post(
        f"{API}/warehouse/products", json={"name": "Порог до неба", "min_stock": "9" * 30}
    )
    assert threshold.status_code == 422
    assert threshold.json()["error"]["code"] == "quantity_too_large"


def test_huge_exponent_does_not_hang_the_parser():
    """`1e100000` не должно уходить в построение числа из ста тысяч цифр.

    `int()` от такого Decimal упирается в предел разбора Python и падает
    ValueError'ом мимо нашей обработки — то есть снова пятисоткой.
    """
    for absurd in ("1e1000", "1e100000", "-1e100000"):
        with pytest.raises(errors.ValidationError) as caught:
            warehouse_service.parse_quantity(absurd)
        assert caught.value.code == "quantity_too_large", absurd


def test_quantity_parsing_never_touches_float():
    """Разбор количества — Decimal, а не float: 0.1 × 3 обязано быть ровно 0.3."""
    assert warehouse_service.parse_quantity("0.1") == 100
    assert sum(warehouse_service.parse_quantity("0.1") for _ in range(3)) == 300
    assert warehouse_service.parse_quantity("0.1") * 3 == warehouse_service.parse_quantity("0.3")
    assert warehouse_service.parse_quantity("1 250,5") == 1_250_500
    assert warehouse_service.parse_quantity(None) is None
    assert warehouse_service.parse_quantity("") is None
    assert warehouse_service.format_quantity(1500) == "1.5"
    assert warehouse_service.format_quantity(2000) == "2"
    assert warehouse_service.format_quantity(-100) == "-0.1"


def test_cost_of_many_deals_matches_the_cost_of_each(root_client, warehouse_on):
    """Себестоимость пачкой считается так же, как поштучно.

    Пачечный запрос написан для финансового блока и отчётов, где заявок сотни и
    спрашивать про каждую отдельно нельзя. Пока его никто не звал, он оставался
    непроверенным — а «похоже на правильное» в деньгах не считается: расхождение
    в округлении на копейку заметят в отчёте, а объяснить его будет нечем.
    """
    from database.repositories import warehouse as warehouse_repo
    from database.session import SessionLocal

    product = new_product(root_client, name="Профиль алюминиевый", unit="m", cost=33_33)
    clients = root_client.post(f"{API}/clients", json={"name": "Три заявки"}).json()

    deals = []
    for title, quantity in (("Первая", "3"), ("Вторая", "7"), ("Третья", "0")):
        deal = root_client.post(
            f"{API}/deals", json={"title": title, "client_id": clients["id"]}
        ).json()
        if quantity != "0":
            move(root_client, product["id"], "in", quantity, deal_id=None)
            move(root_client, product["id"], "out", quantity, deal_id=deal["id"])
        deals.append(deal["id"])

    with SessionLocal() as db:
        one_by_one = {deal: warehouse_repo.deal_cost_minor(db, deal) for deal in deals}
        in_batch = warehouse_repo.deal_cost_by_deal(db, deals)

        # Заявка без списаний в пачке отсутствует, а поштучно даёт ноль: это не
        # расхождение, а разные вопросы — «сколько ушло» и «ушло ли вообще».
        assert in_batch == {deal: cost for deal, cost in one_by_one.items() if cost}
        assert one_by_one[deals[0]] == 3 * 33_33
        assert one_by_one[deals[1]] == 7 * 33_33
        assert one_by_one[deals[2]] == 0

        # Пустой список не должен превращаться в «спроси про все».
        assert warehouse_repo.deal_cost_by_deal(db, []) == {}


def test_the_kind_of_move_decides_the_sign_not_the_typed_number(root_client, warehouse_on):
    """Приход всегда прибавляет, расход всегда убавляет — как ни набери.

    Приход с минусом раньше проходил как есть и уменьшал остаток. В сумме
    движений это то же самое, что расход, а в журнале склада — строка
    «приход −5», то есть запись, врущая о том, что произошло. Разбираться в
    такой истории через полгода нечем: цифры сходятся, слова не те.

    Свободный знак остаётся у корректировки: она и существует ради «по факту на
    полке меньше, чем в базе».
    """
    product = new_product(root_client, name="Знаковый товар", unit="pcs")
    stock = lambda: root_client.get(f"{WH}/products/{product['id']}").json()["stock_milli"]

    move(root_client, product["id"], "in", "-5")          # набрали с минусом
    assert stock() == 5_000, "приход с минусом убавил остаток"

    move(root_client, product["id"], "out", "2")
    assert stock() == 3_000

    move(root_client, product["id"], "out", "-1")         # расход с минусом
    assert stock() == 2_000, "расход с минусом прибавил остаток"

    move(root_client, product["id"], "return", "-3")      # возврат с минусом
    assert stock() == 5_000, "возврат с минусом убавил остаток"

    # А корректировка минус принимает: ради неё исключение и сделано.
    move(root_client, product["id"], "adjust", "-1")
    assert stock() == 4_000


def test_a_search_string_longer_than_the_cap_is_refused(root_client, warehouse_on):
    """Двести знаков — потолок поиска, дальше это не поиск, а вставленный абзац."""
    from web.api.deps import MAX_SEARCH

    fine = root_client.get(f"{WH}/products", params={"search": "я" * MAX_SEARCH})
    assert fine.status_code == 200, fine.text

    too_long = root_client.get(f"{WH}/products", params={"search": "я" * (MAX_SEARCH + 1)})
    assert too_long.status_code == 422, too_long.text


# --- снимки товара -------------------------------------------------------------
#
# Название опознаёт вещь плохо: «шлейф 40-pin» и «шлейф 40-pin (узкий)» —
# отличить их на полке можно только глазами. Проверки ниже про то, что снимок
# доезжает целым, не пускает к себе чужого и уносит за собой файлы.


def _snimok(client: TestClient, product_id: int, name: str = "detal.png", **kw):
    from tests.conftest import png_bytes

    return client.post(
        f"{WH}/products/{product_id}/photos",
        files={"file": (name, kw.pop("content", None) or png_bytes(), "image/png")},
    )


def test_snimok_dobavlyaetsya_i_otdayotsya_dvumya_razmerami(root_client):
    """Два размера делаются сразу: плитку показывают, снимок открывают.

    Один размер не годится ни в ту, ни в другую сторону: оригинал с телефона на
    полсотни позиций — это мегабайты на каждое открытие списка, а плитка в 320
    точек не годится, чтобы рассмотреть маркировку.
    """
    item = new_product(root_client, name="Шлейф 40-pin")
    sozdan = _snimok(root_client, item["id"])
    assert sozdan.status_code == 201, sozdan.text
    photo = sozdan.json()
    assert photo["size_bytes"] > 0, "вес обоих файлов не посчитан"

    spisok = root_client.get(f"{WH}/products/{item['id']}/photos")
    assert spisok.status_code == 200, spisok.text
    assert [p["id"] for p in spisok.json()["items"]] == [photo["id"]]

    for razmer in ("view", "thumb"):
        otdan = root_client.get(
            f"{WH}/products/{item['id']}/photos/{photo['id']}", params={"size": razmer}
        )
        assert otdan.status_code == 200, f"{razmer}: {otdan.text}"
        # Оба размера лежат в webp: хранить исходный jpeg рядом незачем, а
        # разный тип у двух файлов одного снимка означал бы два пути раздачи.
        assert otdan.headers["content-type"] == "image/webp"
        assert otdan.content[:4] == b"RIFF", f"{razmer} — не webp"


def test_pervyy_snimok_tot_chto_pokazyvayut(root_client):
    """Порядок задаёт человек, и первый — тот, что идёт в список.

    Признака «главная» нет намеренно: он завёл бы инвариант «ровно одна», а
    такие в проекте держатся запросами. Порядок разъехаться сам не может.
    """
    item = new_product(root_client, name="Корпус в сборе")
    pervyy = _snimok(root_client, item["id"], "a.png").json()
    vtoroy = _snimok(root_client, item["id"], "b.png").json()

    poryadok = root_client.put(
        f"{WH}/products/{item['id']}/photos/order",
        json={"order": [vtoroy["id"], pervyy["id"]]},
    )
    assert poryadok.status_code == 200, poryadok.text
    assert [p["id"] for p in poryadok.json()["items"]] == [vtoroy["id"], pervyy["id"]]

    # Неполный порядок отвергается: он оставил бы снимки, места которых никто не
    # назвал, и выдача зависела бы от плана запроса.
    krivoy = root_client.put(
        f"{WH}/products/{item['id']}/photos/order", json={"order": [vtoroy["id"]]}
    )
    assert krivoy.status_code == 422, krivoy.text
    assert krivoy.json()["error"]["code"] == "bad_photo_order"


def test_chuzhoy_snimok_ne_otdayotsya_po_pryamomu_adresu(root_client):
    """Номер товара в адресе — не украшение, а условие отбора.

    Без него номер снимка от соседнего товара отдавал бы чужую картинку тому,
    кто его угадал или запомнил.
    """
    odin = new_product(root_client, name="Товар первый")
    drugoy = new_product(root_client, name="Товар второй")
    photo = _snimok(root_client, odin["id"]).json()

    chuzhoy = root_client.get(f"{WH}/products/{drugoy['id']}/photos/{photo['id']}")
    assert chuzhoy.status_code == 404, chuzhoy.text
    assert chuzhoy.json()["error"]["code"] == "photo_not_found"


def test_ne_kartinku_ne_prinimayem(root_client):
    """Тип определяется подписью файла, а не расширением в имени.

    `.png` в имени не гарантирует ничего, а SVG — это документ со скриптом
    внутри, а не растр: приняв его, склад стал бы носителем чужого кода.
    """
    item = new_product(root_client, name="Не картинка")
    otkaz = _snimok(root_client, item["id"], "virus.png", content=b"%PDF-1.4 not an image")
    assert otkaz.status_code == 422, otkaz.text

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    otkaz_svg = _snimok(root_client, item["id"], "kartinka.svg", content=svg)
    assert otkaz_svg.status_code == 422, otkaz_svg.text
    assert otkaz_svg.json()["error"]["code"] == "bad_photo_type"


def test_udalenie_snimka_unosit_oba_fayla(root_client):
    """Строка ушла — файлов на диске не осталось.

    Снимаются они ПОСЛЕ фиксации: откат вернул бы строку, а файла уже не было
    бы, и карточка обещала бы снимок, которого физически нет.
    """
    from core.services import product_photo_service
    from database.session import SessionLocal

    item = new_product(root_client, name="Под удаление")
    photo = _snimok(root_client, item["id"]).json()

    with SessionLocal() as db:
        row = product_photo_service.poluchit(db, item["id"], photo["id"])
        puti = [
            product_photo_service.put_na_diske(row, "view"),
            product_photo_service.put_na_diske(row, "thumb"),
        ]
    assert all(p.exists() for p in puti), "файлы снимка не легли на диск"

    ubran = root_client.delete(f"{WH}/products/{item['id']}/photos/{photo['id']}")
    assert ubran.status_code == 200, ubran.text
    assert root_client.get(f"{WH}/products/{item['id']}/photos").json()["items"] == []
    assert not any(p.exists() for p in puti), "файлы снимка остались на диске"


def test_bez_bloka_snimkov_net(root_client):
    """Выключенный блок уносит и снимки — целиком, а не «пустым списком»."""
    item = new_product(root_client, name="Со снимком")
    _snimok(root_client, item["id"])

    vykl = root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    assert vykl.status_code == 200, vykl.text
    try:
        zakryto = root_client.get(f"{WH}/products/{item['id']}/photos")
        assert zakryto.status_code == 403, zakryto.text
        assert zakryto.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
        modules_service.invalidate()
