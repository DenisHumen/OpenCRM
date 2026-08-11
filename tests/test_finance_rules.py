"""Правила начисления: налог с оборота, стандартные расходы, деньги по оплате.

Проверяется не «работают ли ручки», а то, чего нельзя нарушить:

- **прошлые отчёты не едут.** Ставку поменяли — март остался мартом, потому что
  ставка лежит СНИМКОМ на операции, а не подтягивается из справочника;
- **округление живёт в одном месте** и уходит к ближайшей минорной единице,
  ровная половина — вверх по модулю, симметрично для прихода и возврата;
- **производное не хранится.** Получено, остаток, «оплачен» и сумма начислений с
  учётом поправок считаются запросом; ни одной колонки под них нет;
- **правка суммы не переписывает прошлое** — заводится разностная операция, и
  экран показывает сумму цепочки, а не последнее записанное число;
- **выключённый блок денег не ломает отгрузку**: заказ закрывается, склад
  списывается, а денежных операций просто нет.

Своя песочница во времени (2032 год) — по той же причине, что у соседнего
файла: тесты набора гоняются в обоих порядках и заводят свои записи «сегодня».
"""

import ast
import itertools
import pathlib

import pytest
from sqlalchemy import event

from tests.conftest import API

FINANCE = f"{API}/finance"
ORDERS = f"{API}/orders"
STOCK = f"{API}/warehouse"

#: Месяц, в котором живут платежи этого файла.
MARCH_FROM = "2032-03-01"
MARCH_TO = "2032-03-31"
MARCH_DAY = "2032-03-15T12:00:00"
#: Другой месяц — для проверки, что доплата ложится своим днём.
APRIL_FROM = "2032-04-01"
APRIL_TO = "2032-04-30"
APRIL_DAY = "2032-04-05T10:00:00"

ROOT = pathlib.Path(__file__).resolve().parent.parent

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


# --- обстановка ---------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def blocks_on(root_client):
    """Деньги, бланки, склад и заказы включены на время файла.

    Возвращаем всё как было: блоки по умолчанию выключены, и оставить их
    включёнными значило бы менять поведение соседних наборов — например, состав
    меню в дашборде.
    """
    keys = ("documents", "warehouse", "orders", "finance")
    for key in keys:
        assert root_client.post(f"{API}/modules/{key}", json={"enabled": True}).status_code == 200
    yield
    for key in ("orders", "warehouse", "finance"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": False})


@pytest.fixture(autouse=True)
def rules_of_this_test_only(root_client):
    """Правила глобальны, а проверки — нет: закрываем свои после каждого теста.

    Без этого налог, заведённый одним тестом, начисляется на платежи всех
    следующих, а стандартный расход — на каждый закрытый ими заказ. Набор при
    этом гоняется в обоих порядках, так что «повезло» здесь не бывает.
    """
    yield
    for rule in root_client.get(f"{FINANCE}/rules").json()["items"]:
        root_client.delete(f"{FINANCE}/rules/{rule['id']}")


@pytest.fixture
def income_category(root_client):
    created = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Выручка правил {uniq()}", "direction": "income"},
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture
def tax_category(root_client):
    created = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Налог правил {uniq()}", "direction": "expense", "purpose": "tax"},
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture
def cost_category(root_client):
    created = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Упаковка правил {uniq()}", "direction": "expense"},
    )
    assert created.status_code == 201, created.text
    return created.json()


def make_rule(client, **fields):
    body = {"name": f"Правило {uniq()}", **fields}
    created = client.post(f"{FINANCE}/rules", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def tax_rule(client, tax_category, income_category=None, rate_bp=500):
    return make_rule(
        client,
        base="income_percent",
        category_id=tax_category["id"],
        source_category_id=income_category["id"] if income_category else None,
        rate_bp=rate_bp,
    )


def pay(client, category_id, amount, when=MARCH_DAY, **extra):
    response = client.post(
        f"{FINANCE}/payments",
        json={"category_id": category_id, "amount": amount, "happened_at": when, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def operations(client, date_from=MARCH_FROM, date_to=MARCH_TO, **params):
    return client.get(
        f"{FINANCE}/operations",
        params={"from": date_from, "to": date_to, "per_page": 200, **params},
    ).json()["items"]


def profit_of(client, date_from=MARCH_FROM, date_to=MARCH_TO) -> dict:
    response = client.get(f"{FINANCE}/profit", params={"from": date_from, "to": date_to})
    assert response.status_code == 200, response.text
    return response.json()


def by_category(reported: dict, category_id: int) -> int:
    """Сумма по СВОЕЙ статье, а не итог периода.

    Таблица общая, и соседние тесты сыплют в тот же месяц свои платежи: итог
    периода зависел бы от порядка прогона, а сумма по приметной статье — нет.
    Это то же правило, по которому в наборе ищут карточку по приметному имени.
    """
    mine = [row for row in reported["items"] if row["category_id"] == category_id]
    return mine[0]["amount"] if mine else 0


def product(root_client, stock="10", price="50000"):
    item = root_client.post(
        f"{STOCK}/products",
        json={"name": f"Товар правил {uniq()}", "sku": f"FR-{uniq()}", "price": price},
    ).json()
    root_client.post(
        f"{STOCK}/moves", json={"product_id": item["id"], "kind": "in", "quantity": stock}
    )
    return item


def order_with(root_client, item, quantity="2", **fields):
    created = root_client.post(ORDERS, json={"kind": "sales_order", **fields})
    assert created.status_code == 201, created.text
    order = created.json()
    added = root_client.post(
        f"{ORDERS}/{order['id']}/lines",
        json={"product_id": item["id"], "quantity": quantity},
    )
    assert added.status_code == 201, added.text
    return order


def stock_of(root_client, product_id) -> int:
    return root_client.get(f"{STOCK}/products/{product_id}").json()["stock_milli"]


def money_of(root_client, order_id) -> dict:
    response = root_client.get(f"{FINANCE}/documents/{order_id}/money")
    assert response.status_code == 200, response.text
    return response.json()


def operation(client, category_id, amount, when=MARCH_DAY, **extra):
    """Обычная операция — вторым входом в правила, помимо `/finance/payments`.

    Вход через `/finance/operations` проверяется отдельно нарочно: заслон
    «правила смотрят только на приход» живёт именно здесь, а весь остальной набор
    ходит через оплату и события заказа и до этой строки не доезжает.
    """
    response = client.post(
        f"{FINANCE}/operations",
        json={"category_id": category_id, "amount": amount, "happened_at": when, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def accrual_by_category(money: dict, category_id: int) -> dict | None:
    mine = [row for row in money["accruals"] if row["category_id"] == category_id]
    return mine[0] if mine else None


# --- округление ---------------------------------------------------------------


@pytest.mark.parametrize(
    "base_minor,rate_bp,expected,why",
    [
        (12_345, 500, 617, "617,25 — ближе к 617"),
        (12_350, 500, 618, "ровная половина уходит вверх"),
        (-12_350, 500, -618, "и вниз по модулю тоже — симметрия"),
        (10, 500, 1, "половина минорной единицы не теряется"),
        (1, 500, 0, "и не выдумывается"),
    ],
)
def test_okruglenie_k_blizhayshemu_polovina_vverkh_po_modulyu(
    base_minor, rate_bp, expected, why
):
    """Правило названо вслух и закреплено таблицей.

    Симметрия здесь не украшение: несимметричное округление оставляет копейку
    налога на каждом отменённом платеже, и через год «отложено налога»
    перестанет сходиться с нулём по полностью возвращённым заказам.
    """
    from core.services.finance_service import accrue_minor

    assert accrue_minor(base_minor, rate_bp) == expected, why


def test_okruglenie_zhivyot_v_odnom_meste(root_client, income_category, tax_category):
    """Налог по десяти платежам равен десяти начислениям поодиночке.

    Если бы округление повторялось где-то ещё — например, в отчёте поверх суммы,
    — эти два числа разошлись бы, и объяснить расхождение было бы нечем.
    """
    from core.services.finance_service import accrue_minor

    tax_rule(root_client, tax_category, income_category)
    for _ in range(10):
        pay(root_client, income_category["id"], 12_345)

    reported = profit_of(root_client)
    mine = [row for row in reported["items"] if row["category_id"] == tax_category["id"]][0]
    assert mine["amount"] == accrue_minor(12_345, 500) * 10 == 6_170


# --- снимок ставки: прошлые отчёты не едут ------------------------------------


def test_snimok_stavki_lezhit_na_operatsii():
    """Ссылки на правило без снимка не хватает: сумму правила правят на месте.

    Проверка структурная нарочно: поведенческая поймала бы потерю снимка только
    после того, как кто-то поменяет ставку, а колонка, которую забыли заполнить,
    молчит до этого дня.
    """
    from database.models import FinanceOperation

    columns = FinanceOperation.__table__.columns
    for name in ("rule_id", "rate_bp", "base_amount_minor"):
        assert name in columns, f"у операции нет снимковой колонки {name}"
    assert columns["rate_bp"].nullable and columns["base_amount_minor"].nullable, (
        "снимок обязан быть пустым у платежа: платёж ничем не порождён"
    )


def test_smena_stavki_ne_perepisyvaet_proshlyy_mesyats(
    root_client, income_category, tax_category
):
    """Начислили в марте по 5%, сменили на 7% — март не изменился.

    Единственная проверка, доказывающая, что прошлые отчёты не поедут. Считайся
    налог «на лету по действующей ставке», смена ставки переписала бы март задним
    числом — и это была бы не ошибка реализации, а неизбежное следствие
    устройства.
    """
    rule = tax_rule(root_client, tax_category, income_category, rate_bp=500)
    pay(root_client, income_category["id"], 12_345)

    was = profit_of(root_client)
    was_tax = [row for row in was["items"] if row["category_id"] == tax_category["id"]][0]
    assert was_tax["amount"] == 617

    changed = root_client.patch(f"{FINANCE}/rules/{rule['id']}", json={"rate_bp": 700})
    assert changed.status_code == 200, changed.text

    now = profit_of(root_client)
    now_tax = [row for row in now["items"] if row["category_id"] == tax_category["id"]][0]
    assert now_tax["amount"] == 617, "смена ставки переписала прошлый месяц"
    assert now["profit"] == was["profit"], "прибыль за март изменилась задним числом"

    # А новое начисление идёт уже по новой ставке — правило работает.
    pay(root_client, income_category["id"], 12_345, when=APRIL_DAY)
    april = profit_of(root_client, APRIL_FROM, APRIL_TO)
    april_tax = [row for row in april["items"] if row["category_id"] == tax_category["id"]][0]
    assert april_tax["amount"] == 864, "новая ставка не применилась"


def test_nalog_lozhitsya_v_ten_zhe_den_chto_platyozh(
    root_client, income_category, tax_category
):
    """«Пришли деньги — сразу отложился налог, видно в тот же день.»

    Дата берётся у платежа, а не «сегодня»: финансы заносят пачкой в конце
    недели, и налог с пятничной выручки не должен уезжать в следующий месяц
    отдельно от неё.
    """
    tax_rule(root_client, tax_category, income_category)
    pay(root_client, income_category["id"], 100_000, when=MARCH_DAY)

    mine = [
        row
        for row in operations(root_client, category_id=tax_category["id"])
        if row["category_id"] == tax_category["id"]
    ]
    assert mine, "налог не начислился"
    assert mine[0]["happened_at"].startswith("2032-03-15")
    # И в апреле его нет: он весь в марте.
    assert not operations(root_client, APRIL_FROM, APRIL_TO, category_id=tax_category["id"])


def test_stavka_i_baza_lezhat_na_nachislenii(root_client, income_category, tax_category, db):
    """На вопрос «почему тут 5%, а там 7%» отвечает сама операция.

    `base_amount_minor` не выводится обратно из суммы и ставки: округление
    необратимо, 617 могло получиться из целого диапазона баз.
    """
    from database.repositories import finance as finance_repo

    rule = tax_rule(root_client, tax_category, income_category)
    pay(root_client, income_category["id"], 12_345)

    accrued = [
        row
        for row in finance_repo.list_operations(db, category_id=tax_category["id"])[0]
    ]
    assert accrued, "начисление не найдено"
    assert accrued[0].rule_id == rule["id"]
    assert accrued[0].rate_bp == 500
    assert accrued[0].base_amount_minor == 12_345


# --- деньги считаются по оплате -----------------------------------------------


def test_predoplata_i_doplata_lozhatsya_svoimi_dnyami(
    root_client, income_category, tax_category
):
    """Предоплата — в день предоплаты, остаток — в день доплаты.

    Налог начисляется с каждой отдельно, с той суммы, что пришла: иначе отчёт за
    март показал бы налог с денег, которых в марте не было.
    """
    tax_rule(root_client, tax_category, income_category)
    pay(root_client, income_category["id"], 60_000, when=MARCH_DAY)
    pay(root_client, income_category["id"], 40_000, when=APRIL_DAY)

    march = profit_of(root_client)
    april = profit_of(root_client, APRIL_FROM, APRIL_TO)
    assert by_category(march, income_category["id"]) == 60_000
    assert by_category(april, income_category["id"]) == 40_000
    assert by_category(march, tax_category["id"]) == 3_000, (
        "налог марта посчитан не с мартовской суммы"
    )
    assert by_category(april, tax_category["id"]) == 2_000


def test_poluchennoe_i_ostatok_schitayutsya_zaprosom(root_client, income_category):
    """Получено, остаток и «оплачен» — три числа, и ни одно не хранится."""
    item = product(root_client, price="50000")
    order = order_with(root_client, item, quantity="2")  # 2 × 500,00 = 1000,00

    empty = money_of(root_client, order["id"])
    assert (empty["total"], empty["received"], empty["due"], empty["paid"]) == (
        100_000,
        0,
        100_000,
        False,
    )

    pay(root_client, income_category["id"], 40_000, document_id=order["id"])
    part = money_of(root_client, order["id"])
    assert (part["received"], part["due"], part["paid"]) == (40_000, 60_000, False)

    pay(root_client, income_category["id"], 60_000, when=APRIL_DAY, document_id=order["id"])
    full = money_of(root_client, order["id"])
    assert (full["received"], full["due"], full["paid"]) == (100_000, 0, True)


def test_v_baze_net_kolonok_pod_oplatu():
    """Ни `paid_at`, ни `is_paid`, ни `payments_total` — «оплачен» считается.

    Структурная проверка: хранимое число разойдётся с историей на первом же
    возврате, а поведенческая заметит это только после того, как кто-то забудет
    его обновить.
    """
    from database.session import Base

    forbidden = ("paid_at", "is_paid", "payments_total", "paid_total")
    guilty = [
        f"{name}.{column.name}"
        for name, table in Base.metadata.tables.items()
        for column in table.columns
        if column.name in forbidden
    ]
    assert guilty == [], "появилась хранимая оплата: " + ", ".join(guilty)


def test_vozvrat_storniruet_nalog(root_client, income_category, tax_category):
    """Вернули деньги — налог ушёл в минус сам, без отдельной ветки в коде.

    Ровно то, ради чего округление симметрично по модулю: полностью возвращённый
    платёж обязан оставлять по налогу ноль.
    """
    tax_rule(root_client, tax_category, income_category)
    pay(root_client, income_category["id"], 12_345)
    pay(root_client, income_category["id"], -12_345, when=MARCH_DAY)

    reported = profit_of(root_client)
    assert by_category(reported, income_category["id"]) == 0
    assert by_category(reported, tax_category["id"]) == 0, "налог не сторнировался в ноль"


def test_nachislennaya_operatsiya_ne_porozhdaet_vtoruyu(root_client, tax_category):
    """Правило, кладущее начисленное в ДОХОДНУЮ статью, не закручивает себя.

    Такая статья законна: возврат переплаченного налога — доход с налоговым
    признаком. Без явной проверки `rule_id IS NOT NULL` правило начислило бы на
    собственное начисление, и так до упора.
    """
    doubtful = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Возврат налога {uniq()}", "direction": "income", "purpose": "tax"},
    ).json()
    make_rule(
        root_client,
        base="income_percent",
        category_id=doubtful["id"],
        source_category_id=None,
        rate_bp=500,
    )

    pay(root_client, doubtful["id"], 100_000)
    mine = [
        row
        for row in operations(root_client, category_id=doubtful["id"])
        if row["category_id"] == doubtful["id"]
    ]
    # Платёж и ровно одно начисление с него. Третьей строки быть не должно.
    assert len(mine) == 2, f"правило сработало на собственном начислении: {mine}"


# --- стандартные расходы при закрытии заказа ---------------------------------


def test_zakrytie_zakaza_spisyvaet_standartnye_rashody(root_client, cost_category):
    """«Списываются при закрытии любого заказа» — дословно из решения заказчика."""
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)

    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200

    money = money_of(root_client, order["id"])
    packing = [row for row in money["accruals"] if row["category_id"] == cost_category["id"]]
    assert packing and packing[0]["amount"] == 8_000, money["accruals"]


def test_dvoynoe_zakrytie_ne_zadvaivaet_rashody(root_client, cost_category):
    """Двое нажали разом — расходы записались один раз.

    Защита не в проверке «уже закрыт», а в условной смене статуса: событие
    поднимается ПОСЛЕ неё, и проигравший гонку до подписчиков не доходит.
    """
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)

    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    again = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert again.status_code in (409, 422), again.text

    money = money_of(root_client, order["id"])
    packing = [row for row in money["accruals"] if row["category_id"] == cost_category["id"]]
    assert len(packing) == 1 and packing[0]["amount"] == 8_000, "расход записался дважды"


def test_bez_finansov_otgruzka_prohodit_i_sklad_spisyvaetsya(root_client, cost_category):
    """Блок денег выключен — заказ закрывается целиком и без ошибок.

    Это не половина операции, а правда о системе без финансов: статус, движения
    склада и журнал на месте, а денежных операций не существует как явления.
    """
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client, stock="10")
    order = order_with(root_client, item, quantity="3")

    assert root_client.post(f"{API}/modules/finance", json={"enabled": False}).status_code == 200
    try:
        shipped = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
        assert shipped.status_code == 200, shipped.text
        assert shipped.json()["status"] == "closed"
        assert stock_of(root_client, item["id"]) == 7_000, "склад не списался"
    finally:
        root_client.post(f"{API}/modules/finance", json={"enabled": True})

    # Денег по заказу нет — и это не поломка, а отсутствие блока в тот момент.
    assert money_of(root_client, order["id"])["accruals"] == []


def test_polomannoe_pravilo_otvechaet_422_a_ne_500_i_sklad_ne_tronut(
    root_client, cost_category
):
    """Отказ участника — доменная ошибка с кодом, и откатывается ВСЁ.

    Отгрузка не должна падать пятисоткой из-за справочника. И раз участник
    возразил, движений склада остаться не должно ни одного: транзакция одна.
    """
    from database.models import FinanceRule
    from database.session import SessionLocal

    rule = make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client, stock="10")
    order = order_with(root_client, item, quantity="3")
    before = stock_of(root_client, item["id"])

    # Ломаем правило мимо сервиса — так же, как это сделала бы правка в базе
    # руками: через API такая сумма не проходит.
    session = SessionLocal()
    broken = session.get(FinanceRule, rule["id"])
    broken.amount_minor = 2_100_000_000
    session.commit()
    session.close()

    refused = root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "money_too_large"
    assert stock_of(root_client, item["id"]) == before, "склад тронут при отказе"
    assert root_client.get(f"{ORDERS}/{order['id']}").json()["status"] == "issued"


# --- правка суммы на месте ----------------------------------------------------


def test_pravka_80_na_140_zavodit_raznostnuyu_operatsiyu(root_client, cost_category):
    """В карточке меняют 80 на 140 — цифра сразу верная, история сохраняется.

    Исходная операция не правится и не удаляется: заводится корректирующая на
    разницу. Экран показывает верную сумму не потому, что кто-то переписал число,
    а потому что он СУММИРУЕТ цепочку.
    """
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})

    accrual = money_of(root_client, order["id"])["accruals"][0]
    assert accrual["amount"] == 8_000

    fixed = root_client.patch(f"{FINANCE}/accruals/{accrual['id']}", json={"amount": 14_000})
    assert fixed.status_code == 200, fixed.text

    assert money_of(root_client, order["id"])["accruals"][0]["amount"] == 14_000
    # Третья правка добавляет ещё строку — и сумма снова верна без уборки.
    assert (
        root_client.patch(
            f"{FINANCE}/accruals/{accrual['id']}", json={"amount": 20_000}
        ).status_code
        == 200
    )
    assert money_of(root_client, order["id"])["accruals"][0]["amount"] == 20_000


def test_pravka_ne_trogaet_ni_ishodnuyu_operatsiyu_ni_pravilo(root_client, cost_category):
    """Журнал операций показывает ОБЕ строки, а правило остаётся прежним.

    Молча слить их значило бы спрятать факт правки от того, кто сводит кассу; а
    поправив правило, мы взяли бы 140 и со следующего заказа — чего никто не
    просил.
    """
    from database.repositories import finance as finance_repo
    from database.session import SessionLocal

    rule = make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    accrual = money_of(root_client, order["id"])["accruals"][0]
    root_client.patch(f"{FINANCE}/accruals/{accrual['id']}", json={"amount": 14_000})

    session = SessionLocal()
    try:
        rows = finance_repo.operations_of_document(session, order["id"])
        # Две строки, а не одна переписанная: исходное начисление и разница.
        assert sorted(row.amount_minor for row in rows) == [-8_000, -6_000]
        head = [row for row in rows if row.id == accrual["id"]][0]
        assert head.amount_minor == -8_000, "исходную операцию переписали"
        assert head.correction is None and head.corrects_id is None
    finally:
        session.close()

    still = [
        row
        for row in root_client.get(f"{FINANCE}/rules").json()["items"]
        if row["id"] == rule["id"]
    ]
    assert still and still[0]["amount"] == 8_000, "правило поехало вслед за поправкой"


def test_pravka_ostavlyaet_zapis_v_zhurnale(root_client, cost_category):
    """«Было 80, стало 140, кто и когда» — заказанное дословно."""
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    accrual = money_of(root_client, order["id"])["accruals"][0]
    root_client.patch(f"{FINANCE}/accruals/{accrual['id']}", json={"amount": 14_000})

    entries = root_client.get(
        f"{API}/audit", params={"entity_type": "finance_operation"}
    ).json()["items"]
    mine = [row for row in entries if row["action"] == "finance.accrual_adjusted"]
    assert mine, "поправка прошла мимо журнала"
    assert (mine[0]["value_before"], mine[0]["value_after"]) == ("8000", "14000")
    assert mine[0]["actor_name"], "поправка осталась ничьей"


def test_pravka_na_tu_zhe_summu_otvergaetsya(root_client, cost_category):
    """Ноль разницы — отказ, а не пустая операция."""
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    accrual = money_of(root_client, order["id"])["accruals"][0]

    refused = root_client.patch(f"{FINANCE}/accruals/{accrual['id']}", json={"amount": 8_000})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "nothing_to_adjust"


def test_platyozh_ne_pravitsya_kak_nachislenie(root_client, income_category):
    """Поправляют начисление, а не платёж: деньги исправляют возвратом."""
    payment = pay(root_client, income_category["id"], 50_000)
    refused = root_client.patch(f"{FINANCE}/accruals/{payment['id']}", json={"amount": 60_000})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "not_an_accrual"


# --- отмена проведения --------------------------------------------------------


def test_otmena_zakaza_zavodit_obratnye_operatsii_a_platezhi_ne_trogaet(
    root_client, cost_category, income_category
):
    """Отмена сторнирует НАЧИСЛЕННОЕ и не отменяет полученных денег.

    Товар вернули, а деньги ещё нет: возврат клиенту — отдельное осознанное
    действие человека. И сторнируется итог цепочки, а не первая записанная
    сумма: упаковку могли поправить с 80 на 140.
    """
    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    pay(root_client, income_category["id"], 50_000, document_id=order["id"])

    accrual = money_of(root_client, order["id"])["accruals"][0]
    root_client.patch(f"{FINANCE}/accruals/{accrual['id']}", json={"amount": 14_000})

    reverted = root_client.post(f"{ORDERS}/{order['id']}/revert")
    assert reverted.status_code == 200, reverted.text

    after = money_of(root_client, order["id"])
    assert after["accruals"][0]["amount"] == 0, "сторнировали не итог цепочки"
    assert after["accruals"][0]["reverted"] is True
    assert after["received"] == 50_000, "отмена проведения забрала полученные деньги"


def test_otmena_ne_stiraet_prezhnie_operatsii(root_client, cost_category):
    """Обратной операцией, а не стиранием: «куда делись эти восемьдесят» обязано
    иметь ответ."""
    from database.repositories import finance as finance_repo
    from database.session import SessionLocal

    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    root_client.post(f"{ORDERS}/{order['id']}/revert")

    session = SessionLocal()
    try:
        rows = finance_repo.operations_of_document(session, order["id"])
        assert sorted(row.amount_minor for row in rows) == [-8_000, 8_000]
        assert {row.correction for row in rows} == {None, "reversal"}
    finally:
        session.close()


# --- справочник ---------------------------------------------------------------


def test_zakrytie_stati_pod_zhivym_pravilom_otvergaetsya(root_client, cost_category):
    """Отказ переносится туда, где человек его понимает.

    Закрой мы статью, на которую смотрит правило, — следующая отгрузка падала бы
    посреди работы у стойки с непонятной ошибкой.
    """
    rule = make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    refused = root_client.delete(f"{FINANCE}/categories/{cost_category['id']}")
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "category_in_use_by_rule"

    # Закрыли правило — статья закрывается.
    assert root_client.delete(f"{FINANCE}/rules/{rule['id']}").status_code == 200
    assert root_client.delete(f"{FINANCE}/categories/{cost_category['id']}").status_code == 200


def test_zakrytoe_pravilo_ne_nachislyaet_a_vyklyuchennoe_vklyuchaetsya_obratno(
    root_client, cost_category
):
    """Выключатель и закрытие — разные операции, и путать их нельзя."""
    rule = make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    assert root_client.patch(
        f"{FINANCE}/rules/{rule['id']}", json={"is_active": False}
    ).status_code == 200

    item = product(root_client)
    quiet = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{quiet['id']}/close", json={})
    assert money_of(root_client, quiet["id"])["accruals"] == [], "выключенное правило начислило"

    root_client.patch(f"{FINANCE}/rules/{rule['id']}", json={"is_active": True})
    loud = order_with(root_client, product(root_client))
    root_client.post(f"{ORDERS}/{loud['id']}/close", json={})
    assert money_of(root_client, loud["id"])["accruals"][0]["amount"] == 8_000


def test_migratsiya_ne_seet_ni_odnoy_stavki(root_client):
    """Чужая ставка, появившаяся молча, начнёт откладывать деньги без спроса.

    Проверяем не «список пуст» (соседние тесты успели завести свои), а то, что
    среди правил нет ни одного, которого никто в этом наборе не создавал: посев
    ставил бы их с приметными именами вроде «Налог» и «Упаковка».
    """
    items = root_client.get(f"{FINANCE}/rules", params={"include_closed": True}).json()["items"]
    seeded = [row for row in items if not row["name"].startswith("Правило ")]
    assert seeded == [], f"миграция засеяла ставки: {seeded}"


@pytest.mark.parametrize("bad", ["5.5", 0, -1, None, "", True, 10_001])
def test_drobnaya_stavka_ne_okruglyaetsya_a_otvergaetsya(bad):
    """Ставка — целое число базисных пунктов, и дробь сюда не доезжает.

    «5.5» здесь означало бы, что кто-то по дороге посчитал ставку через `float`,
    а округлить её значило бы разойтись с налоговой молча.
    """
    from core import exceptions as errors
    from core.services import finance_service

    with pytest.raises(errors.ValidationError):
        finance_service.parse_rate_bp(bad)


@pytest.mark.parametrize(
    "body,code",
    [
        ({"base": "income_percent", "rate_bp": 0}, "bad_rate"),
        ({"base": "income_percent", "rate_bp": 10_001}, "rate_too_large"),
        ({"base": "per_order", "amount": 0}, "zero_money"),
        ({"base": "per_order", "amount": -100}, "negative_money"),
        ({"base": "wat", "amount": 100}, "unknown_base"),
    ],
)
def test_nesummy_i_nestavki_otvergayutsya_a_ne_okruglyayutsya(
    root_client, cost_category, body, code
):
    """Округлить «5.5» значило бы разойтись с налоговой на копейку молча."""
    response = root_client.post(
        f"{FINANCE}/rules",
        json={"name": f"Кривое {uniq()}", "category_id": cost_category["id"], **body},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code


def test_pravilo_smotrit_tolko_na_dohodnuyu_statyu(root_client, tax_category, cost_category):
    """«С какого вида дохода» — вопрос о приходе, и расходная статья на него не
    отвечает."""
    response = root_client.post(
        f"{FINANCE}/rules",
        json={
            "name": f"Странное {uniq()}",
            "base": "income_percent",
            "category_id": tax_category["id"],
            "source_category_id": cost_category["id"],
            "rate_bp": 500,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_must_be_income"


def test_pravilo_schitaet_tolko_so_svoego_vida_dohoda(root_client, tax_category):
    """«Ставок можно завести несколько, под разные виды дохода» — дословно."""
    mine = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Свой доход {uniq()}", "direction": "income"},
    ).json()
    other = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Чужой доход {uniq()}", "direction": "income"},
    ).json()
    tax_rule(root_client, tax_category, mine, rate_bp=500)

    pay(root_client, other["id"], 100_000)
    assert not [
        row
        for row in operations(root_client, category_id=tax_category["id"])
        if row["category_id"] == tax_category["id"]
    ], "правило сработало на чужом виде дохода"

    pay(root_client, mine["id"], 100_000)
    assert by_category(profit_of(root_client), tax_category["id"]) == 5_000


# --- считается запросом, а не в Python ---------------------------------------


def test_summa_nachisleniy_po_zakazu_iz_bazy_a_ne_iz_pyti(root_client, cost_category, db):
    """Цепочка поправок складывается SUM'ом, а не обходом списка в Python.

    Проверка механическая нарочно: «считается запросом» — свойство, которое легко
    потерять при первой переделке, и остальные тесты остаются зелёными.
    """
    from core.services import finance_service
    from database.session import engine

    make_rule(
        root_client, base="per_order", category_id=cost_category["id"], amount=8_000
    )
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})

    seen: list[str] = []

    def remember(_conn, _cursor, statement, *_rest):
        seen.append(statement.lower())

    event.listen(engine, "before_cursor_execute", remember)
    try:
        finance_service.money_of_document(db, order["id"])
    finally:
        event.remove(engine, "before_cursor_execute", remember)

    assert any("sum(" in statement for statement in seen), (
        "начисления сложили в Python"
    )
    assert any("coalesce" in statement for statement in seen), (
        "цепочка поправок сгруппирована не по COALESCE(corrects_id, id)"
    )


def test_v_bloke_po_prezhnemu_net_ni_deleniya_ni_float():
    """Новый код блока обязан жить по тому же правилу, что и старый.

    Отдельная проверка рядом с соседней в `test_finance.py`: та перебирает файлы
    списком, и новый файл в неё надо не забыть добавить. Здесь названы те, где
    появилось округление.
    """
    for rel in ("core/services/finance_service.py", "database/repositories/finance.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        guilty = []
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                guilty.append(f"{rel}:{node.lineno} деление даёт float")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"float", "round"}:
                    guilty.append(f"{rel}:{node.lineno} {node.func.id}(...)")
        assert guilty == [], "деньги прошли через дробное:\n" + "\n".join(guilty)


# --- клиент заказа ------------------------------------------------------------


def test_zakaz_nahodit_klienta_po_nomeru(root_client):
    """«Указать клиента можно по номеру или имени, остальное подтягивается.»"""
    phone = f"+380 67 {uniq()} 11"
    existing = root_client.post(
        f"{API}/clients", json={"name": f"Постоянный {uniq()}", "phone": phone}
    ).json()

    created = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_phone": phone}
    )
    assert created.status_code == 201, created.text
    assert created.json()["client_id"] == existing["id"]
    assert created.json()["client_created"] is False


def test_zakaz_zavodit_kartochku_kogda_klienta_ne_bylo(root_client):
    """«Если клиента не было — он создаётся», и экран об этом говорит."""
    name = f"Новичок {uniq()}"
    created = root_client.post(
        ORDERS,
        json={"kind": "sales_order", "client_name": name, "client_phone": f"+380 50 {uniq()} 22"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["client_created"] is True

    client_id = created.json()["client_id"]
    assert root_client.get(f"{API}/clients/{client_id}").json()["name"] == name


def test_dva_sovpadeniya_dayut_otkaz_so_spiskom_kandidatov(root_client):
    """Молча взять первого — значит увезти деньги и товар на чужую карточку."""
    phone = f"+380 63 {uniq()} 33"
    first = root_client.post(
        f"{API}/clients", json={"name": f"Семья А {uniq()}", "phone": phone}
    ).json()
    second = root_client.post(
        f"{API}/clients", json={"name": f"Семья Б {uniq()}", "phone": phone}
    ).json()

    refused = root_client.post(ORDERS, json={"kind": "sales_order", "client_phone": phone})
    assert refused.status_code == 409, refused.text
    body = refused.json()["error"]
    assert body["code"] == "client_ambiguous"
    candidates = {row["id"] for row in body["details"]["candidates"]}
    assert {first["id"], second["id"]} <= candidates
    # Номер в списке замаскирован: человек назвал один номер, а не просил
    # справочник телефонов.
    assert all(row["phone_masked"].startswith("…") for row in body["details"]["candidates"])

    # Второй запрос приходит уже с выбором — и поиск не делается вовсе.
    chosen = root_client.post(
        ORDERS, json={"kind": "sales_order", "client_id": second["id"]}
    )
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["client_id"] == second["id"]


def test_naydennaya_kartochka_ne_perezapisyvaetsya(root_client):
    """«Подтянулись данные» и «затёрлись данные» — разные действия."""
    phone = f"+380 68 {uniq()} 44"
    existing = root_client.post(
        f"{API}/clients",
        json={"name": f"Старый {uniq()}", "phone": phone, "email": "old@example.com"},
    ).json()

    root_client.post(
        ORDERS,
        json={
            "kind": "sales_order",
            "client_phone": phone,
            "client_name": "Совсем другое имя",
            "client_email": "new@example.com",
        },
    )
    card = root_client.get(f"{API}/clients/{existing['id']}").json()
    assert card["name"] == existing["name"], "имя в карточке затёрли"
    assert card["email"] == "old@example.com", "адрес в карточке затёрли"


def test_zakaz_postavshchiku_kartochku_klienta_ne_zavodit(root_client):
    """Поставщик — не клиент: заводить под него карточку значит засорять справочник."""
    created = root_client.post(
        ORDERS, json={"kind": "purchase_order", "client_name": f"ООО Поставщик {uniq()}"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["client_id"] is None
    assert created.json()["client_created"] is False


# --- налог живёт с платежом, а не с бланком -----------------------------------
#
# Отмена проведения заказа и возврат денег — два разных действия человека, и
# сторнируют они разное. Проверки ниже — про границу между ними: снять налог
# отменой заказа значит снять его с оборота, который никуда не делся.


def test_otmena_zakaza_ne_snimaet_nalog_nachislennyy_s_platezha(
    root_client, income_category, tax_category, cost_category
):
    """Отмена заказа сторнирует своё — упаковку, а не налог с живого платежа.

    Налог начислен ПЛАТЕЖОМ и висит на том же бланке только потому, что платёж
    назвал бланк. Сняв его отменой заказа, мы получаем оборот 100 000 при налоге
    ноль: деньги в кассе есть, отложенного налога нет.

    Хуже второй шаг, и он штатный: следом человек возвращает деньги отдельным
    действием, правило снимает налог ВТОРОЙ раз — с отрицательной базы, — и
    прибыль вырастает из воздуха.
    """
    tax_rule(root_client, tax_category, income_category)
    make_rule(root_client, base="per_order", category_id=cost_category["id"], amount=8_000)

    item = product(root_client)
    order = order_with(root_client, item)
    assert root_client.post(f"{ORDERS}/{order['id']}/close", json={}).status_code == 200
    pay(root_client, income_category["id"], 100_000, document_id=order["id"])

    assert by_category(profit_of(root_client), tax_category["id"]) == 5_000

    reverted = root_client.post(f"{ORDERS}/{order['id']}/revert")
    assert reverted.status_code == 200, reverted.text

    money = money_of(root_client, order["id"])
    packing = accrual_by_category(money, cost_category["id"])
    assert packing["amount"] == 0 and packing["reverted"] is True, "упаковка не сторнирована"
    tax = accrual_by_category(money, tax_category["id"])
    assert tax["reverted"] is False, "налог сторнирован отменой заказа"
    assert tax["amount"] == 5_000
    assert money["received"] == 100_000, "отмена проведения забрала полученные деньги"
    assert by_category(profit_of(root_client), tax_category["id"]) == 5_000, (
        "налог снят при живом обороте"
    )

    # Второй шаг штатной пары: деньги вернули. Налог обязан уйти в ноль — ровно
    # один раз, а не второй.
    pay(root_client, income_category["id"], -100_000, document_id=order["id"])
    after = profit_of(root_client)
    assert by_category(after, income_category["id"]) == 0
    assert by_category(after, tax_category["id"]) == 0, (
        "налог снят дважды: отменой заказа и возвратом денег"
    )


def test_otmena_zakaza_bez_pravil_zakrytiya_nichego_ne_storniruet(
    root_client, income_category, tax_category
):
    """Заказ без единого правила «на заказ» отменяется, не тронув налог.

    Отдельно от соседней проверки: там упаковка есть, и её сторнирование могло бы
    скрыть, что заодно сторнировали и налог. Здесь сторнировать нечего вовсе, и
    обратных операций не должно появиться ни одной.
    """
    from database.repositories import finance as finance_repo
    from database.session import SessionLocal

    tax_rule(root_client, tax_category, income_category)
    item = product(root_client)
    order = order_with(root_client, item)
    root_client.post(f"{ORDERS}/{order['id']}/close", json={})
    pay(root_client, income_category["id"], 100_000, document_id=order["id"])

    assert root_client.post(f"{ORDERS}/{order['id']}/revert").status_code == 200

    session = SessionLocal()
    try:
        rows = finance_repo.operations_of_document(session, order["id"])
        assert [row for row in rows if row.correction == "reversal"] == [], (
            "отмена заказа завела обратную операцию по чужому начислению"
        )
    finally:
        session.close()


# --- расходов на отгрузку не бывает у приёмки ---------------------------------


def test_priyomka_zakaza_postavshchiku_ne_spisyvaet_upakovku(root_client, cost_category):
    """«Расходы на ОТГРУЗКУ покупателю» — при закупке их не бывает.

    Событие одно на оба вида заказа, и это правильно: складу интересны оба.
    Решать, к какому из них относится стандартный расход, — вопрос блока денег.
    """
    make_rule(root_client, base="per_order", category_id=cost_category["id"], amount=8_000)
    item = product(root_client)
    purchase = order_with(root_client, item, kind="purchase_order")

    assert root_client.post(f"{ORDERS}/{purchase['id']}/close", json={}).status_code == 200
    assert money_of(root_client, purchase["id"])["accruals"] == [], (
        "приёмка от поставщика списала расходы на отгрузку"
    )

    # А отгрузка покупателю — списывает: правило не сломано, оно просто не про
    # закупку.
    sale = order_with(root_client, product(root_client))
    root_client.post(f"{ORDERS}/{sale['id']}/close", json={})
    assert (
        accrual_by_category(money_of(root_client, sale["id"]), cost_category["id"])["amount"]
        == 8_000
    )


# --- заслоны, снятие которых не роняло ни одного теста ------------------------


def test_zanesyonnyy_rashod_ne_porozhdaet_naloga_s_oborota(
    root_client, income_category, tax_category, cost_category
):
    """Правило смотрит на ПРИХОД, и занесённый расход его не запускает.

    Правило без `source_category_id` считает «со всякого прихода», и без явной
    проверки направления статьи каждая занесённая аренда порождала бы «налог с
    оборота» — с самой себя.

    Заодно это единственная проверка входа через `/finance/operations`: весь
    остальной набор ходит через оплату и события заказа.
    """
    make_rule(
        root_client,
        base="income_percent",
        category_id=tax_category["id"],
        source_category_id=None,
        rate_bp=500,
    )

    operation(root_client, cost_category["id"], 100_000)
    assert by_category(profit_of(root_client), tax_category["id"]) == 0, (
        "расход породил налог с оборота"
    )

    # А приход через ту же ручку — порождает: правило работает, вход тот же.
    operation(root_client, income_category["id"], 100_000)
    assert by_category(profit_of(root_client), tax_category["id"]) == 5_000, (
        "правила не работают через /finance/operations"
    )


def test_platyozh_po_rashodnoy_state_otvergaetsya(root_client, cost_category):
    """Оплата — это приход, и расходная статья здесь означала бы налог не с того.

    Отказ стоит на входе оплаты, а не полагается на «человек не ошибётся»:
    платёж клиенту, проведённый как выручка, посчитал бы налог с чужой суммы и
    попал бы в «получено по заказу».
    """
    refused = root_client.post(
        f"{FINANCE}/payments",
        json={"category_id": cost_category["id"], "amount": 50_000, "happened_at": MARCH_DAY},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "payment_needs_income"


def test_nachislenie_v_dohodnuyu_statyu_ne_stanovitsya_poluchennymi_dengami(root_client):
    """Начисленное — не деньги в кассе, даже когда статья доходная.

    Статья «возврат переплаченного налога» законно доходная, и потому отбора по
    направлению статьи самого по себе не хватает. Без отдельного условия
    `rule_id IS NULL` начисление показалось бы полученными деньгами, и заказ стал
    бы «оплачен» суммой, которой в кассе не было.
    """
    back = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Возврат налога {uniq()}", "direction": "income", "purpose": "tax"},
    ).json()
    revenue = root_client.post(
        f"{FINANCE}/categories",
        json={"name": f"Выручка кассы {uniq()}", "direction": "income"},
    ).json()
    make_rule(
        root_client,
        base="income_percent",
        category_id=back["id"],
        source_category_id=revenue["id"],
        rate_bp=500,
    )

    item = product(root_client, price="50000")
    order = order_with(root_client, item, quantity="2")  # 2 × 500,00 = 1000,00
    pay(root_client, revenue["id"], 96_000, document_id=order["id"])

    money = money_of(root_client, order["id"])
    assert money["received"] == 96_000, "начисление посчиталось полученными деньгами"
    assert money["due"] == 4_000
    assert money["paid"] is False, "заказ оплачен деньгами, которых в кассе не было"


# --- клиент заказа: молчаливых исходов не бывает ------------------------------


def test_odno_sovpadenie_po_imeni_ne_uvozit_zakaz_na_chuzhuyu_kartochku(root_client):
    """Совпало ИМЯ подстрокой — это не примета, а догадка.

    «Иванов» находит Петра Иванова, у которого совсем другой телефон. Молчаливая
    привязка означает, что деньги и товар уедут на чужую карточку, а названный
    номер пропадёт без слова.
    """
    tail = uniq()
    stranger = root_client.post(
        f"{API}/clients",
        json={"name": f"Пётр Иванов {tail}", "phone": f"+380 67 {tail} 01"},
    ).json()

    refused = root_client.post(
        ORDERS,
        json={
            "kind": "sales_order",
            "client_name": f"Иванов {tail}",
            "client_phone": f"+380 50 {tail} 77",
        },
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()["error"]
    assert body["code"] == "client_name_only"
    assert stranger["id"] in {row["id"] for row in body["details"]["candidates"]}

    # Названный номер не потерян: он доезжает до новой карточки вторым запросом.
    created = root_client.post(
        ORDERS,
        json={
            "kind": "sales_order",
            "client_name": f"Иванов {tail}",
            "client_phone": f"+380 50 {tail} 77",
            "client_create_new": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["client_created"] is True
    assert created.json()["client_id"] != stranger["id"]
    card = root_client.get(f"{API}/clients/{created.json()['client_id']}").json()
    assert card["phone"] == f"+380 50 {tail} 77", "названный номер потерян"


def test_tochnaya_primeta_ne_vytesnyaetsya_odnofamiltsami(root_client):
    """Карточка с совпавшим номером обязана попасть в выдачу, а не в её хвост.

    Выдача кандидатов обрезана пределом, а имя ищется подстрокой: десяток
    однофамильцев, правленых сегодня, вытеснил бы из окна ту единственную
    карточку, у которой сошёлся номер. Заказ завёл бы ей дубль вместо того, чтобы
    её найти, — и вся защита из соседних проверок обошлась бы сама собой.
    """
    tail = uniq()
    phone = f"+380 44 {tail} 00"
    # Своя — ПЕРВОЙ: по свежести правки она в самом конце очереди.
    mine = root_client.post(
        f"{API}/clients", json={"name": f"Однофамилец {tail} 0", "phone": phone}
    ).json()
    for number in range(1, 8):
        root_client.post(
            f"{API}/clients", json={"name": f"Однофамилец {tail} {number}"}
        )

    found = root_client.post(
        ORDERS,
        json={
            "kind": "sales_order",
            "client_name": f"Однофамилец {tail}",
            "client_phone": phone,
        },
    )
    assert found.status_code == 201, found.text
    assert found.json()["client_id"] == mine["id"], "точную примету вытеснили однофамильцы"
    assert found.json()["client_created"] is False


def test_nazvannyy_nomer_protivorechit_kartochke_i_ostanavlivaet(root_client):
    """Карточку выбирают ВСЕ названные приметы разом, а не первая совпавшая.

    Почта сошлась, номер — нет. Привязать по почте значит выбросить номер, о
    котором человек сказал вслух; а номер, противоречащий карточке, — это либо
    другой человек, либо смена номера, и решает это человек, а не сервер.
    """
    tail = uniq()
    existing = root_client.post(
        f"{API}/clients",
        json={
            "name": f"Почтовый {tail}",
            "phone": f"+380 67 {tail} 05",
            "email": f"same-{tail}@example.com",
        },
    ).json()

    refused = root_client.post(
        ORDERS,
        json={
            "kind": "sales_order",
            "client_email": f"same-{tail}@example.com",
            "client_phone": f"+380 50 {tail} 99",
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "client_phone_mismatch"
    assert existing["id"] in {
        row["id"] for row in refused.json()["error"]["details"]["candidates"]
    }

    # Карточка при этом не тронута: «подтянулись данные» и «затёрлись данные» —
    # разные действия.
    card = root_client.get(f"{API}/clients/{existing['id']}").json()
    assert card["phone"] == f"+380 67 {tail} 05"


def test_odin_nomer_bez_imeni_zavodit_kartochku(root_client):
    """«Указать клиента можно по номеру ИЛИ имени, если клиента не было — он
    создаётся» — дословно из решения заказчика.

    Молчаливое 201 с ничейным заказом недопустимо: введённый номер исчезал без
    следа и без отказа, а заказ оставался без карточки — то есть без ленты, без
    истории и без денег на клиенте.
    """
    tail = uniq()
    phone = f"+380 99 {tail} 55"
    created = root_client.post(ORDERS, json={"kind": "sales_order", "client_phone": phone})
    assert created.status_code == 201, created.text
    assert created.json()["client_created"] is True
    assert created.json()["client_id"], "заказ остался ничейным"

    card = root_client.get(f"{API}/clients/{created.json()['client_id']}").json()
    assert card["phone"] == phone, "номер не сохранился"
    assert tail in card["name"], "карточку нечем назвать в списке"

    # Второй заказ по тому же номеру находит ту же карточку, а не заводит вторую.
    again = root_client.post(ORDERS, json={"kind": "sales_order", "client_phone": phone})
    assert again.status_code == 201, again.text
    assert again.json()["client_id"] == created.json()["client_id"]
    assert again.json()["client_created"] is False


def test_mestnyy_nomer_nahodit_kartochku_zavedyonnuyu_mezhdunarodnym(root_client):
    """«067…» и «+380 67…» — один человек, и решает это `phone_norm`.

    Тот же приём, что у телефонии, и та же единственная функция приведения: без
    неё заказ не находил бы карточку, которую по тому же номеру находит звонок.
    """
    tail = uniq()
    root_client.patch(f"{API}/settings", json={"values": {"default_country_code": "380"}})
    try:
        existing = root_client.post(
            f"{API}/clients",
            json={"name": f"Международный {tail}", "phone": f"+38067{tail}12"},
        ).json()
        found = root_client.post(
            ORDERS, json={"kind": "sales_order", "client_phone": f"067{tail}12"}
        )
        assert found.status_code == 201, found.text
        assert found.json()["client_id"] == existing["id"], "местный набор не нашёл карточку"
        assert found.json()["client_created"] is False
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"default_country_code": ""}})
