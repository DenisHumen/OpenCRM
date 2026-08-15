"""Несколько складов: места, переезды, раскладка остатков, история.

Главное, что здесь проверяется, — **инвариант остатка цел**. Остаток
по-прежнему не хранится и по-прежнему считается запросом; склад лишь добавил
группировку. Поэтому почти каждая проверка ниже сводится к одному вопросу:
сходится ли сумма после того, как товар куда-то переехал.
"""

import itertools

import pytest

from core.services import warehouse_service
from tests.conftest import API

WAREHOUSES = f"{API}/warehouses"
STOCK = f"{API}/warehouse"

# База одна на весь набор, а код склада и артикул товара уникальны по всей
# таблице — значит два теста с одинаковым числом столкнулись бы, и падал бы не
# тот, кто виноват, а тот, кто пришёл вторым.
_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(autouse=True)
def warehouse_on(root_client):
    """Блок склада по умолчанию выключен — включаем на время проверок."""
    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    yield


@pytest.fixture
def main_warehouse(root_client):
    """Основной склад. Он обязан существовать до первого прихода."""
    listed = root_client.get(WAREHOUSES)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items, "система осталась без единого склада"
    return next(w for w in items if w["is_default"])


@pytest.fixture
def second(root_client):
    """Второй склад, закрываемый за собой: включённый выбор склада меняет
    условия у соседних проверок, а набор гоняется в обоих порядках."""
    created = root_client.post(
        WAREHOUSES, json={"name": f"Подсобка {uniq()}", "code": f"WH{uniq()}"}
    )
    assert created.status_code == 201, created.text
    warehouse = created.json()
    yield warehouse
    root_client.delete(f"{WAREHOUSES}/{warehouse['id']}")


def product(client, name="Матрица", stock=None, warehouse_id=None, cost="100"):
    item = client.post(
        f"{STOCK}/products",
        json={"name": f"{name} {uniq()}", "sku": f"WH-{uniq()}", "cost": cost, "price": "500"},
    ).json()
    if stock:
        body = {"product_id": item["id"], "kind": "in", "quantity": stock}
        if warehouse_id:
            body["warehouse_id"] = warehouse_id
        assert client.post(f"{STOCK}/moves", json=body).status_code == 201
    return item


def stock_on(client, product_id, warehouse_id=None) -> int:
    params = {"warehouse_id": warehouse_id} if warehouse_id else {}
    listed = client.get(f"{STOCK}/products", params={"per_page": 200, **params}).json()
    row = next(r for r in listed["items"] if r["id"] == product_id)
    return row["stock_milli"]


# --- склад всегда есть, и он ровно один основной ------------------------------


def test_sklad_est_do_pervogo_prihoda(root_client, main_warehouse):
    """Система без места не примет ни одного прихода.

    На боевой базе склад кладёт миграция, на свежей — засев при старте. Не будь
    ни того, ни другого, первый же приход упёрся бы в «склад не выбран», причём
    выбрать было бы не из чего.
    """
    assert main_warehouse["is_default"] is True

    item = product(root_client, stock="10")
    moves = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    assert moves[0]["warehouse_id"] == main_warehouse["id"], "приход лёг мимо основного склада"


def test_osnovnoy_sklad_odin(root_client, second):
    """Иначе приход поехал бы то туда, то сюда — и заметить это нечем."""
    root_client.patch(f"{WAREHOUSES}/{second['id']}", json={"is_default": True})
    items = root_client.get(WAREHOUSES).json()["items"]
    assert [w["id"] for w in items if w["is_default"]] == [second["id"]]


def test_vybor_sklada_poyavlyaetsya_kogda_ih_bolshe_odnogo(root_client, second):
    """Мастерской с одной подсобкой выбор склада в каждой форме — помеха.

    Правило выведено из данных, а не из настройки: завёл второй склад — выбор
    появился везде сам, закрыл — исчез. Считает его сервер, потому что
    посчитанное на фронте стало бы вторым экземпляром того же правила.
    """
    assert root_client.get(WAREHOUSES).json()["many"] is True

    root_client.delete(f"{WAREHOUSES}/{second['id']}")
    assert root_client.get(WAREHOUSES).json()["many"] is False


def test_kod_sklada_ne_povtoryaetsya(root_client, second):
    """Код печатается на наклейке: два склада с одним кодом — это коробка,
    про которую не сказать, откуда она."""
    clash = root_client.post(WAREHOUSES, json={"name": "Ещё одна", "code": second["code"]})
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "warehouse_code_taken"


# --- закрытие склада ----------------------------------------------------------


def test_posledniy_sklad_ne_zakryt(root_client, main_warehouse):
    """Без склада система не примет ни одного прихода."""
    denied = root_client.delete(f"{WAREHOUSES}/{main_warehouse['id']}")
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "last_warehouse"


def test_sklad_s_ostatkom_ne_zakryt(root_client, second):
    """Иначе товар физически лежит, а в системе его нет нигде."""
    product(root_client, stock="5", warehouse_id=second["id"])

    denied = root_client.delete(f"{WAREHOUSES}/{second['id']}")
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "warehouse_not_empty"


def test_pustoy_sklad_s_istoriey_zakryvaetsya_a_istoriya_ostayotsya(
    root_client, main_warehouse, second
):
    """Склад закрывают, а не удаляют: движения старше закрытия."""
    item = product(root_client, stock="4", warehouse_id=second["id"])
    root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": second["id"],
            "to_warehouse_id": main_warehouse["id"],
            "quantity": "4",
        },
    )

    closed = root_client.delete(f"{WAREHOUSES}/{second['id']}")
    assert closed.status_code == 200, closed.text
    assert second["id"] not in [w["id"] for w in root_client.get(WAREHOUSES).json()["items"]]

    history = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    assert any(m["warehouse_id"] == second["id"] for m in history), "история закрытого пропала"


# --- переезд ------------------------------------------------------------------


def test_pereezd_ne_menyaet_obshchiy_ostatok(root_client, main_warehouse, second):
    """Товар не появился и не пропал — он переехал.

    Проверяется главное: на источнике минус, на приёмнике плюс, а сумма по
    товару та же. Разойдись здесь хоть на единицу — и весь блок врёт.
    """
    item = product(root_client, stock="10")
    before_total = stock_on(root_client, item["id"])

    moved = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "3",
        },
    )
    assert moved.status_code == 201, moved.text

    assert stock_on(root_client, item["id"], main_warehouse["id"]) == 7000
    assert stock_on(root_client, item["id"], second["id"]) == 3000
    assert stock_on(root_client, item["id"]) == before_total


def test_pereezd_eto_dve_stroki_i_odna_zapis_v_zhurnale(root_client, main_warehouse, second):
    """Одна строка с двумя складами сломала бы `SUM GROUP BY warehouse_id`.

    Поэтому в движениях две строки со ссылкой на шапку, а в журнале — одна
    запись: в общем списке движений переезд читается плохо (две строки подряд с
    разными знаками), и разбираться, одно это событие или два, пришлось бы по
    времени и по памяти.
    """
    item = product(root_client, stock="10")
    header = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "2",
        },
    ).json()

    moves = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    pair = [m for m in moves if m["transfer_id"] == header["id"]]
    assert len(pair) == 2, "переезд записан не двумя строками"
    assert sorted(m["quantity_milli"] for m in pair) == [-2000, 2000]
    assert {m["warehouse_id"] for m in pair} == {main_warehouse["id"], second["id"]}

    journal = root_client.get(f"{STOCK}/transfers").json()["items"]
    entry = next(t for t in journal if t["id"] == header["id"])
    assert entry["from_warehouse_name"] and entry["to_warehouse_name"]
    assert entry["items"] == [{"product_id": item["id"], "quantity_milli": 2000}]


def test_pereezd_na_tot_zhe_sklad_otvergaetsya(root_client, main_warehouse):
    """Это не операция, а опечатка — но в истории выглядит как настоящий переезд."""
    item = product(root_client, stock="5")
    denied = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": main_warehouse["id"],
            "quantity": "1",
        },
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "same_warehouse"


def test_uslugu_perevezti_nelzya(root_client, main_warehouse, second):
    """У услуги остатка нет и быть не может."""
    service = root_client.post(
        f"{STOCK}/products", json={"name": f"Выезд {uniq()}", "is_service": True}
    ).json()
    denied = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": service["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "1",
        },
    )
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "service_has_no_stock"


def test_uvezti_bolshe_chem_lezhit_ostanavlivaet(root_client, main_warehouse, second):
    """У обычного движения минус разрешён, у переезда — нет.

    Деталь могли поставить в машину до того, как занесли накладную, — это
    честный минус. А увезти коробку с пустого склада нельзя физически: значит
    остановка и явное подтверждение, которое записывается.
    """
    item = product(root_client, stock="2")
    body = {
        "product_id": item["id"],
        "from_warehouse_id": main_warehouse["id"],
        "to_warehouse_id": second["id"],
        "quantity": "5",
    }

    denied = root_client.post(f"{STOCK}/transfers", json=body)
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "not_enough_on_source"
    assert stock_on(root_client, item["id"], main_warehouse["id"]) == 2000

    forced = root_client.post(f"{STOCK}/transfers", json={**body, "confirm_negative": True})
    assert forced.status_code == 201, forced.text
    # Подтверждение записано: через месяц вопрос «почему минус» задаст не тот,
    # кто нажимал, и ответить должна запись, а не память.
    assert warehouse_service.OVERDRAFT_NOTE in forced.json()["comment"]
    assert stock_on(root_client, item["id"], main_warehouse["id"]) == -3000


def test_pereezd_ne_menyaet_sebestoimost_zayavki(root_client, main_warehouse, second):
    """Иначе заявка обошлась бы дороже оттого, что коробку переставили с полки."""
    client = root_client.post(f"{API}/clients", json={"name": f"Заказчик {uniq()}"}).json()
    deal = root_client.post(
        f"{API}/deals", json={"title": f"Работа {uniq()}", "client_id": client["id"]}
    ).json()

    item = product(root_client, stock="10", cost="250")
    root_client.post(
        f"{STOCK}/moves",
        json={"product_id": item["id"], "kind": "out", "quantity": "2", "deal_id": deal["id"]},
    )
    cost_before = root_client.get(f"{STOCK}/moves", params={"deal_id": deal["id"]}).json()["cost"]

    root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "5",
        },
    )
    cost_after = root_client.get(f"{STOCK}/moves", params={"deal_id": deal["id"]}).json()["cost"]

    assert cost_after == cost_before, "переезд подвинул себестоимость заявки"


# --- отмена переезда ----------------------------------------------------------


def test_otmena_eto_obratnyy_pereezd_a_ne_udalenie(root_client, main_warehouse, second):
    """Удали мы две строки — остаток сошёлся бы, а «куда делись две матрицы»
    осталось бы без ответа. То есть ошибка стала бы невидимой."""
    item = product(root_client, stock="10")
    header = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "4",
        },
    ).json()

    back = root_client.post(f"{STOCK}/transfers/{header['id']}/revert")
    assert back.status_code == 201, back.text
    assert back.json()["reverses_id"] == header["id"]

    assert stock_on(root_client, item["id"], main_warehouse["id"]) == 10000
    assert stock_on(root_client, item["id"], second["id"]) == 0

    moves = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    assert len([m for m in moves if m["transfer_id"] == header["id"]]) == 2, "прежние строки стёрты"
    assert len([m for m in moves if m["transfer_id"] == back.json()["id"]]) == 2


def test_dvazhdy_odin_pereezd_ne_otmenyayetsya(root_client, main_warehouse, second):
    """Иначе двойное нажатие увезло бы товар обратно дважды."""
    item = product(root_client, stock="6")
    header = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "6",
        },
    ).json()

    assert root_client.post(f"{STOCK}/transfers/{header['id']}/revert").status_code == 201
    again = root_client.post(f"{STOCK}/transfers/{header['id']}/revert")
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "transfer_already_reverted"
    assert stock_on(root_client, item["id"], second["id"]) == 0


# --- раскладка и история ------------------------------------------------------


def test_poisk_pokazyvaet_gde_i_skolko(root_client, main_warehouse, second):
    """То, ради чего половина задачи: «а на точке-то оно есть?»."""
    item = product(root_client, stock="10")
    root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "4",
        },
    )

    listed = root_client.get(f"{STOCK}/products", params={"search": item["name"]}).json()
    row = next(r for r in listed["items"] if r["id"] == item["id"])
    assert row["by_warehouse"][str(main_warehouse["id"])] == 6000
    assert row["by_warehouse"][str(second["id"])] == 4000
    assert row["stock_milli"] == 10000


def test_raskladka_stoit_odin_zapros_na_stranitsu(root_client, main_warehouse, second):
    """Запрос на строку превратил бы поиск из 500 позиций в 500 обращений.

    Эта ошибка в блоке уже разбиралась и закрывалась (`stock_by_product`);
    повторить её раскладкой по складам — значит вернуть ту же беду с другого
    конца. Считаем сами запросы, а не время: время зависит от диска и соседей,
    число обращений — только от кода.
    """
    from sqlalchemy import event

    from database.session import engine

    # Состав блоков и настройки живут в кэше две секунды: попадёт ли обновление
    # в замер, решает секундомер, а не код. Считаем то, о чём тест спрашивает.
    CACHE_TABLES = ("module_states", "site_settings")

    def cost_of(count: int) -> int:
        made = [product(root_client, stock="5") for _ in range(count)]
        for item in made:
            root_client.post(
                f"{STOCK}/transfers",
                json={
                    "product_id": item["id"],
                    "from_warehouse_id": main_warehouse["id"],
                    "to_warehouse_id": second["id"],
                    "quantity": "1",
                },
            )
        queries: list[str] = []
        listener = lambda conn, cursor, statement, *rest: queries.append(statement)  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            answer = root_client.get(f"{STOCK}/products", params={"per_page": 200})
            assert answer.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len([q for q in queries if not any(t in q for t in CACHE_TABLES)])

    few = cost_of(2)
    many = cost_of(8)
    assert few == many, f"страница подорожала с ростом числа товаров: {few} → {many}"


def test_ostatok_na_datu(root_client, main_warehouse):
    """Побочная выгода того, что остаток не хранится.

    При хранимом числе вопрос «сколько было на первое число» не имел бы ответа
    вовсе, а он нужен и для сверки с бумажной инвентаризацией, и для разговора
    с бухгалтером.
    """
    from database.repositories import warehouse as warehouse_repo
    from database.session import SessionLocal

    item = product(root_client, stock="7")
    db = SessionLocal()
    try:
        assert warehouse_repo.stock_of(db, item["id"]) == 7000
        # До первого движения — ноль, а не «неизвестно».
        assert warehouse_repo.stock_of(db, item["id"], on_date="2000-01-01 00:00:00") == 0
    finally:
        db.close()


def test_istoriya_odnogo_sklada(root_client, main_warehouse, second):
    """Приёмщик открывает свой склад и видит, что на нём происходило."""
    item = product(root_client, stock="9")
    root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "3",
        },
    )

    here = root_client.get(
        f"{STOCK}/moves", params={"warehouse_id": second["id"], "per_page": 200}
    ).json()
    assert here["items"], "история склада пуста"
    assert all(m["warehouse_id"] == second["id"] for m in here["items"])


# --- блок выключается ---------------------------------------------------------


def test_vyklyuchennyy_blok_ne_stiraet_sklady(root_client, second):
    """Выключили раздел — не значит отказались от складов и их истории."""
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    assert root_client.get(WAREHOUSES).status_code == 403
    assert root_client.get(f"{STOCK}/transfers").status_code == 403

    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    assert second["id"] in [w["id"] for w in root_client.get(WAREHOUSES).json()["items"]]


def test_zavesti_sklad_pravo_otdelnoye(manager_client, root_client):
    """Завести склад — решение структурное, как завести юрлицо.

    Приход, расход и перемещение остаются на `create`: кладовщик двигает товар
    каждый день, а склады заводят раз в год, и это разные полномочия.
    """
    denied = manager_client.post(WAREHOUSES, json={"name": "Чужой склад"})
    assert denied.status_code == 403, denied.text
    # Смотреть при этом можно: выбрать склад в форме прихода нужно всякому.
    assert manager_client.get(WAREHOUSES).status_code == 200


def test_zhurnal_v_kartochke_pro_etot_tovar(root_client, main_warehouse, second):
    """Иначе карточка одной позиции показывала бы переезды всех сразу.

    Фильтр идёт подзапросом по строкам, а не соединением: соединение вернуло бы
    переезд дважды (у него две строки на позицию), и в журнале появились бы
    близнецы — их-то и стережёт проверка на длину.
    """
    mine = product(root_client, stock="5")
    other = product(root_client, stock="5")
    for item in (mine, other):
        root_client.post(
            f"{STOCK}/transfers",
            json={
                "product_id": item["id"],
                "from_warehouse_id": main_warehouse["id"],
                "to_warehouse_id": second["id"],
                "quantity": "1",
            },
        )

    journal = root_client.get(f"{STOCK}/transfers", params={"product_id": mine["id"]}).json()
    assert journal["total"] == 1, "в журнале товара оказались чужие переезды или близнецы"
    assert journal["items"][0]["items"][0]["product_id"] == mine["id"]


def test_otmenennyy_pereezd_pomechen(root_client, main_warehouse, second):
    """Кнопка «отменить» на уже отменённом — это приглашение на отказ."""
    item = product(root_client, stock="4")
    header = root_client.post(
        f"{STOCK}/transfers",
        json={
            "product_id": item["id"],
            "from_warehouse_id": main_warehouse["id"],
            "to_warehouse_id": second["id"],
            "quantity": "4",
        },
    ).json()
    assert root_client.post(f"{STOCK}/transfers/{header['id']}/revert").status_code == 201

    journal = root_client.get(f"{STOCK}/transfers", params={"product_id": item["id"]}).json()
    original = next(t for t in journal["items"] if t["id"] == header["id"])
    reversal = next(t for t in journal["items"] if t["reverses_id"] == header["id"])
    assert original["reverted"] is True
    assert reversal["reverted"] is False, "отмену отмены предлагать не нужно"
