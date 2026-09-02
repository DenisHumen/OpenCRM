"""Блок «Финансы»: операции, статьи, бюджеты, прибыль.

Проверяется не «работают ли ручки», а то, чего нельзя нарушить: прибыль
считается запросом и нигде не хранится, деньги не превращаются во float,
направление статьи и знак операции остаются разными сведениями, а выключенный
блок исчезает целиком, не трогая данные.

**Про регистрацию блока.** Ключ `finance` вносится в реестр (`core/modules.py`)
отдельно и не этим набором. Пока его там нет, охранники `require_module` и
`require_perm` не дают даже импортировать роутер — и правильно делают: опечатка
в имени блока обязана ронять запуск, а не тихо открывать раздел. Поэтому фикция
ниже вносит блок в реестр на время файла и подключает роутер сама. Как только
блок появится в реестре и в `web/main.py` по-настоящему, фикция это увидит и
ничего подменять не станет — обе проверки написаны так, чтобы файл пережил
регистрацию и не завёл вторую копию маршрутов.
"""

import ast
import pathlib
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event

from tests.conftest import API, make_manager

MODULE_KEY = "finance"

#: Период, в котором живут операции этого файла.
#:
#: Год выбран заведомо не тот, в котором гоняются соседние тесты: они заводят
#: свои записи «сегодня», и общий счёт по таблице попадал бы то на них, то нет.
#: Своя песочница во времени — то же самое, что приметное имя у карточки.
PERIOD_FROM = "2031-03-01"
PERIOD_TO = "2031-03-31"
INSIDE = "2031-03-15T12:00:00"
#: Тот же месяц, но другой день — для проверки, что период берёт обе даты.
INSIDE_LATER = "2031-03-28T09:30:00"
#: За границей месяца: в отчёт попасть не должно.
OUTSIDE = "2031-04-02T12:00:00"

#: Отдельный месяц для проверки возврата.
#:
#: Ему нужен ИТОГ ПО ПЕРИОДУ, а не по своей статье: утверждение «доход равен
#: нулю» проверяет как раз то, что возврат по расходу никуда не перетёк.
#: Соседние проверки этого файла сыплют доходы в март, и общий итог марта от
#: них зависел бы — а тесты набора гоняются в обоих порядках.
RETURN_FROM = "2031-05-01"
RETURN_TO = "2031-05-31"
RETURN_INSIDE = "2031-05-10T12:00:00"
RETURN_LATER = "2031-05-20T12:00:00"

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Исходники блока — их обходит проверка «деньги не проходят через float».
SOURCES = (
    "database/models/finance.py",
    "database/repositories/finance.py",
    "core/services/finance_service.py",
    "web/api/routes/finance.py",
)


@pytest.fixture(scope="module", autouse=True)
def finance_registered():
    """Внести блок в реестр и подключить роутер — на время этого файла.

    Всё возвращается на место в конце: реестр блоков, реестр прав и список
    маршрутов приложения. Иначе соседние файлы, гоняющиеся после этого, видели
    бы блок, которого в коде реестра нет, — а проверка «каждый блок назван в
    настройках» сверяется как раз с реестром.
    """
    from core import modules as core_modules
    from core import permissions as core_permissions
    from core.services import modules_service
    from web.main import app

    already_registered = core_modules.get(MODULE_KEY) is not None
    was_modules, was_keys = core_modules.MODULES, core_modules.KEYS
    was_by_key = dict(core_modules.BY_KEY)
    was_areas, was_area_by_key = core_permissions.AREAS, dict(core_permissions.BY_KEY)

    if not already_registered:
        block = core_modules.Module(key=MODULE_KEY, default=False)
        core_modules.MODULES = (*was_modules, block)
        core_modules.KEYS = (*was_keys, MODULE_KEY)
        core_modules.BY_KEY[MODULE_KEY] = block
        # Матрица прав строится по реестру блоков и один раз, при импорте.
        # Добавив блок, обязаны пересобрать её — иначе `require_perm` не найдёт
        # области и уронит импорт роутера.
        core_permissions.AREAS = core_permissions._build()
        core_permissions.BY_KEY.clear()
        core_permissions.BY_KEY.update({area.key: area for area in core_permissions.AREAS})

    from web.api.routes import finance as finance_routes

    if _finance_is_mounted(app):
        # Роутер уже подключён по-настоящему — второй раз подключать нельзя:
        # маршруты задвоились бы, и перебор в `test_route_guards` считал бы их
        # дважды.
        added = []
    else:
        tail = len(app.routes)
        app.include_router(finance_routes.router, prefix=API)
        added = list(app.routes[tail:])
        # Подключённый роутер уезжает в конец списка, а в конце уже стоит
        # «ловец» SPA: `GET /{full_path:path}` перехватывает всё подряд и на
        # адреса, начинающиеся с `api/`, честно отвечает 404. Первый прогон на
        # этом и попался: POST работал, GET отвечал «not_found». В `web/main.py`
        # порядок соблюдён сам собой — роутеры подключаются до ловца; здесь его
        # приходится восстанавливать руками.
        del app.routes[tail:]
        at = _spa_index(app)
        app.routes[at:at] = added

    modules_service.invalidate()
    yield
    for route in added:
        app.routes.remove(route)
    core_modules.MODULES, core_modules.KEYS = was_modules, was_keys
    core_modules.BY_KEY.clear()
    core_modules.BY_KEY.update(was_by_key)
    core_permissions.AREAS = was_areas
    core_permissions.BY_KEY.clear()
    core_permissions.BY_KEY.update(was_area_by_key)
    modules_service.invalidate()


def _finance_is_mounted(app) -> bool:
    """Подключён ли роутер блока по-настоящему (то есть из `web/main.py`)."""
    return any(_paths_of(route) and _paths_of(route)[0].startswith(f"{API}/finance") for route in app.routes)


def _paths_of(route) -> list[str]:
    """Адреса маршрута. Подключённый роутер лежит в `app.routes` одним объектом,
    а настоящие адреса живут внутри него — плоский перебор их не видит."""
    original = getattr(route, "original_router", None)
    if original is not None:
        prefix = getattr(route, "prefix", "") or ""
        return [prefix + str(getattr(inner, "path", "")) for inner in original.routes]
    path = getattr(route, "path", None)
    return [str(path)] if path else []


def _spa_index(app) -> int:
    """Где стоит «ловец» SPA. Всё, что после него, до API уже не доходит."""
    for index, route in enumerate(app.routes):
        if any("{full_path" in path for path in _paths_of(route)):
            return index
    return len(app.routes)


@pytest.fixture(scope="module")
def finance_on(root_client):
    """Блок включён на время файла и выключен после.

    Выключаем обратно нарочно: блок по умолчанию выключен, и оставить его
    включённым значило бы менять поведение соседних наборов — например, состав
    меню в дашборде.
    """
    assert root_client.post(f"{API}/modules/{MODULE_KEY}", json={"enabled": True}).status_code == 200
    yield
    root_client.post(f"{API}/modules/{MODULE_KEY}", json={"enabled": False})


@pytest.fixture
def money(root_client, finance_on):
    """Свои статьи дохода и расхода. Имена приметные — таблица общая."""
    income = root_client.post(
        f"{API}/finance/categories",
        json={"name": "Выручка теста 2031", "direction": "income"},
    )
    assert income.status_code == 201, income.text
    expense = root_client.post(
        f"{API}/finance/categories",
        json={"name": "Аренда теста 2031", "direction": "expense"},
    )
    assert expense.status_code == 201, expense.text
    return income.json(), expense.json()


def add(client, category_id: int, amount: int, when: str = INSIDE, **extra):
    response = client.post(
        f"{API}/finance/operations",
        json={"category_id": category_id, "amount": amount, "happened_at": when, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def profit_of(client, date_from: str = PERIOD_FROM, date_to: str = PERIOD_TO) -> dict:
    response = client.get(f"{API}/finance/profit", params={"from": date_from, "to": date_to})
    assert response.status_code == 200, response.text
    return response.json()


# --- прибыль считается запросом и нигде не хранится -------------------------


def test_pribyli_net_ni_v_odnoy_kolonke():
    """Хранимый итог однажды разойдётся с историей — и узнать, какое из двух
    чисел верное, будет неоткуда.

    Проверка структурная нарочно: поведенческая поймала бы расхождение только
    после того, как кто-то заведёт колонку И забудет её обновить. А колонка,
    которую обновляют аккуратно, всё равно ошибка — она просто ещё не выстрелила.
    """
    from database.session import Base

    forbidden = ("profit", "balance", "total", "fact", "spent", "income", "expense")
    guilty = []
    for name, table in Base.metadata.tables.items():
        if not name.startswith("finance_"):
            continue
        for column in table.columns:
            if any(word in column.name.lower() for word in forbidden):
                guilty.append(f"{name}.{column.name}")
    assert guilty == [], (
        "в блоке появилась колонка с производным итогом: " + ", ".join(guilty)
    )


def test_pribyl_ne_zavisit_ot_stranitsy_spiska(root_client, money):
    """Шестьдесят операций, страница из пятидесяти — прибыль обязана быть полной.

    Ровно та беда, ради которой итог считает база: сложи мы операции в Python по
    выдаче списка, десять последних потерялись бы молча, а число выглядело бы
    правдоподобно.
    """
    income, _expense = money
    for _ in range(60):
        add(root_client, income["id"], 1_000)

    listing = root_client.get(
        f"{API}/finance/operations",
        params={"from": PERIOD_FROM, "to": PERIOD_TO, "category_id": income["id"], "per_page": 50},
    ).json()
    assert len(listing["items"]) == 50, "страница должна быть неполной — иначе проверять нечего"

    reported = profit_of(root_client)
    mine = [row for row in reported["items"] if row["category_id"] == income["id"]][0]
    assert mine["count"] == 60
    assert mine["amount"] == 60_000


def test_pribyl_prihodit_iz_bazy_a_ne_iz_pyti(db, money):
    """Считает SUM, а не Python: смотрим, какой SQL ушёл в базу.

    Проверка механическая нарочно. «Считается запросом» — свойство, которое
    легко потерять при первой же переделке: агрегат заменяют обходом списка,
    все прочие тесты остаются зелёными, а отчёт начинает врать только на
    объёмах, которых в наборе тестов нет.
    """
    from core.services import finance_service
    from database.session import engine

    seen: list[str] = []

    def remember(_conn, _cursor, statement, *_rest):
        seen.append(statement.lower())

    event.listen(engine, "before_cursor_execute", remember)
    try:
        finance_service.profit(
            db, datetime(2031, 3, 1), datetime(2031, 4, 1)
        )
    finally:
        event.remove(engine, "before_cursor_execute", remember)

    assert any("sum(" in statement for statement in seen), (
        "прибыль посчиталась без SUM — значит, её сложили в Python"
    )
    # Три запроса: итоги, разбивка по статьям, названия статей. Потолок здесь
    # не про скорость, а про «на строку отчёта не ходят в базу».
    assert len(seen) <= 4, f"на прибыль ушло {len(seen)} запросов: {seen}"


def test_summa_razbivki_shoditsya_s_itogom(root_client, money):
    """Две цифры на одном экране обязаны сходиться, иначе не верят обеим."""
    income, expense = money
    add(root_client, income["id"], 500_00)
    add(root_client, expense["id"], 120_00, when=INSIDE_LATER)

    reported = profit_of(root_client)
    mine = {row["category_id"]: row for row in reported["items"]}
    assert mine[income["id"]]["amount"] == 500_00
    assert mine[expense["id"]]["amount"] == 120_00
    # Разбивка знает про направление, поэтому итог собирается вычитанием.
    assert (
        mine[income["id"]]["amount"] - mine[expense["id"]]["amount"]
        == 500_00 - 120_00
    )
    assert reported["profit"] == reported["income"] - reported["expense"]


def test_operatsiya_za_granitsey_perioda_v_otchyot_ne_popadaet(root_client, money):
    income, _expense = money
    add(root_client, income["id"], 777_00, when=OUTSIDE)

    reported = profit_of(root_client)
    assert not [row for row in reported["items"] if row["category_id"] == income["id"]]


# --- деньги не превращаются во float ----------------------------------------


def test_dengi_v_kolonkakh_tselye():
    """Дробная колонка ломает соглашение для всех отчётов, куда попадёт."""
    from sqlalchemy import Float, Numeric

    from database.session import Base

    guilty = []
    for name, table in Base.metadata.tables.items():
        if not name.startswith("finance_"):
            continue
        for column in table.columns:
            if "minor" in column.name and isinstance(column.type, (Float, Numeric)):
                guilty.append(f"{name}.{column.name}")
    assert guilty == [], "деньги дробным типом: " + ", ".join(guilty)


@pytest.mark.parametrize("relative_path", SOURCES)
def test_v_bloke_net_ni_deleniya_ni_float(relative_path):
    """Ни `/`, ни `float()`, ни `round()` над деньгами — даже промежуточно.

    Деление в Python даёт float, а `0.1 + 0.2 != 0.3`: одна такая строка, и
    итог по колонке перестаёт совпадать с суммой карточек. Ищем по дереву, а не
    по тексту: `/` встречается в комментариях и путях, а `ast.Div` — только там,
    где действительно делят.
    """
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    guilty = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            guilty.append(f"{relative_path}:{node.lineno} деление даёт float")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"float", "round"}:
                guilty.append(f"{relative_path}:{node.lineno} {node.func.id}(...)")
    assert guilty == [], "деньги прошли через дробное:\n" + "\n".join(guilty)


def test_summy_v_otvete_tselye(root_client, money):
    """JSON отдаёт целые: `500.0` в ответе означает, что float был по дороге."""
    income, expense = money
    add(root_client, income["id"], 333_33)
    add(root_client, expense["id"], 111_11)

    reported = profit_of(root_client)
    numbers = [reported["income"], reported["expense"], reported["profit"]]
    numbers += [row["amount"] for row in reported["items"]]
    for value in numbers:
        assert isinstance(value, int) and not isinstance(value, bool), f"{value!r} не целое"


@pytest.mark.parametrize("bad", [0, "12.5", True, 10**13, None, ""])
def test_nesummy_otvergayutsya_a_ne_okruglyayutsya(bad):
    """Округлить «12.5» значило бы разойтись с накладной на копейку молча."""
    from core import exceptions as errors
    from core.services import finance_service

    with pytest.raises(errors.ValidationError):
        finance_service.parse_amount_minor(bad)


def test_potolok_summy_uderzhivaet_kolonku_mysql():
    """Потолок стоит там, где кончается INT в MySQL, а не «сколько бывает денег»."""
    from core.services import finance_service
    from database.models.finance import MAX_AMOUNT_MINOR

    assert MAX_AMOUNT_MINOR < 2**31 - 1
    assert finance_service.parse_amount_minor(MAX_AMOUNT_MINOR) == MAX_AMOUNT_MINOR


# --- знак и направление — разные сведения -----------------------------------


def test_vozvrat_po_rashodnoy_state_ne_uezzhaet_v_dohody(root_client, money):
    """Минус по расходу — это возврат, а не выручка.

    Считай мы направление по знаку суммы, первый же возврат аренды увеличил бы
    доход фирмы, и сумма выручки перестала бы означать выручку.
    """
    _income, expense = money
    add(root_client, expense["id"], 200_00, when=RETURN_INSIDE)
    add(root_client, expense["id"], -50_00, when=RETURN_LATER)

    reported = profit_of(root_client, RETURN_FROM, RETURN_TO)
    assert reported["income"] == 0, "возврат по расходу попал в доход"
    assert reported["expense"] == 150_00
    assert reported["profit"] == -150_00

    only_expenses = root_client.get(
        f"{API}/finance/operations",
        params={"from": RETURN_FROM, "to": RETURN_TO, "direction": "expense"},
    ).json()
    mine = [row for row in only_expenses["items"] if row["category_id"] == expense["id"]]
    assert sorted(row["amount"] for row in mine) == [-50_00, 200_00], (
        "фильтр по направлению отобрал по знаку, а не по статье"
    )


def test_summa_naruzhu_edet_v_terminakh_stati(root_client, money):
    """Расход отдаётся положительным: «потрачено −40 000» не читается."""
    _income, expense = money
    created = add(root_client, expense["id"], 400_00)
    assert created["amount"] == 400_00
    assert created["direction"] == "expense"


def test_napravlenie_stati_ne_menyaetsya(root_client, money):
    """Перевернуть направление значит объявить весь прошлый расход доходом."""
    _income, expense = money
    response = root_client.patch(
        f"{API}/finance/categories/{expense['id']}",
        json={"name": expense["name"], "direction": "income"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "direction_is_fixed"


# --- статьи ------------------------------------------------------------------


def test_migratsiya_zasevaet_spravochnik(root_client, finance_on):
    """Операцию без статьи завести нельзя, а пустой справочник — конструктор
    вместо работы. Налог и зарплата обязаны быть помечены: ради этого признака
    он и живёт в справочнике."""
    items = root_client.get(f"{API}/finance/categories").json()["items"]
    purposes = {row["purpose"] for row in items}
    assert "tax" in purposes and "salary" in purposes
    assert {"income", "expense"} <= {row["direction"] for row in items}


def test_zakrytaya_statya_uhodit_iz_vybora_no_ne_iz_proshlogo(root_client, money):
    """Закрытие статьи не переписывает прибыль за прошлый период."""
    income, _expense = money
    add(root_client, income["id"], 900_00)
    was = profit_of(root_client)["income"]

    assert root_client.delete(f"{API}/finance/categories/{income['id']}").status_code == 200

    open_ones = root_client.get(f"{API}/finance/categories").json()["items"]
    assert income["id"] not in {row["id"] for row in open_ones}

    with_closed = root_client.get(
        f"{API}/finance/categories", params={"include_closed": True}
    ).json()["items"]
    assert income["id"] in {row["id"] for row in with_closed}

    assert profit_of(root_client)["income"] == was, "закрытие статьи изменило прибыль"
    # Строка журнала осталась подписанной: имя закрытой статьи по-прежнему
    # находится, иначе операция висела бы без объяснения, что это за деньги.
    listing = root_client.get(
        f"{API}/finance/operations",
        params={"from": PERIOD_FROM, "to": PERIOD_TO, "category_id": income["id"]},
    ).json()
    assert listing["items"][0]["category_name"] == income["name"]


def test_statya_iz_chuzhogo_spravochnika_ne_prohodit(root_client, finance_on):
    response = root_client.post(
        f"{API}/finance/operations",
        json={"category_id": 10**7, "amount": 100, "happened_at": INSIDE},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "category_not_found"


# --- бюджеты -----------------------------------------------------------------


def test_plan_protiv_fakta(root_client, money):
    """План лежит в базе, факт — нет: он приходит из того же агрегата, что и прибыль."""
    _income, expense = money
    created = root_client.post(
        f"{API}/finance/budgets",
        json={
            "category_id": expense["id"],
            "period_start": PERIOD_FROM,
            "period_end": PERIOD_TO,
            "planned": 500_00,
        },
    )
    assert created.status_code == 201, created.text

    add(root_client, expense["id"], 180_00)
    add(root_client, expense["id"], 20_00, when=INSIDE_LATER)

    items = root_client.get(
        f"{API}/finance/budgets", params={"from": PERIOD_FROM, "to": PERIOD_TO}
    ).json()["items"]
    mine = [row for row in items if row["category_id"] == expense["id"]][0]
    assert (mine["planned"], mine["fact"], mine["left"]) == (500_00, 200_00, 300_00)


def test_fakt_schitaetsya_po_periodu_samogo_byudzheta(root_client, money):
    """Квартальный план на экране августа обязан показывать квартальный факт.

    Считай мы факт по периоду экрана, «план 300 000, факт 90 000» читалось бы
    как провал там, где идёт третий месяц из трёх.
    """
    _income, expense = money
    created = root_client.post(
        f"{API}/finance/budgets",
        json={
            "category_id": expense["id"],
            "period_start": "2031-01-01",
            "period_end": "2031-06-30",
            "planned": 900_00,
        },
    )
    assert created.status_code == 201, created.text

    add(root_client, expense["id"], 100_00, when="2031-01-20T10:00:00")
    add(root_client, expense["id"], 300_00, when=INSIDE)

    # Смотрим ОДИН март, а факт обязан быть за всё полугодие.
    items = root_client.get(
        f"{API}/finance/budgets", params={"from": PERIOD_FROM, "to": PERIOD_TO}
    ).json()["items"]
    half_year = [row for row in items if row["period_start"] == "2031-01-01"][0]
    assert half_year["fact"] == 400_00


def test_dva_plana_na_odin_period_ne_zavodyatsya(root_client, money):
    _income, expense = money
    body = {
        "category_id": expense["id"],
        "period_start": PERIOD_FROM,
        "period_end": PERIOD_TO,
        "planned": 100_00,
    }
    assert root_client.post(f"{API}/finance/budgets", json=body).status_code == 201
    again = root_client.post(f"{API}/finance/budgets", json=body)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "budget_exists"


def test_plan_ne_byvaet_otritsatelnym(root_client, money):
    """Знак принадлежит операции, а не намерению."""
    _income, expense = money
    response = root_client.post(
        f"{API}/finance/budgets",
        json={
            "category_id": expense["id"],
            "period_start": PERIOD_FROM,
            "period_end": PERIOD_TO,
            "planned": -100_00,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "negative_money"


def test_period_naiznanku_otvergaetsya(root_client, money):
    _income, expense = money
    response = root_client.post(
        f"{API}/finance/budgets",
        json={
            "category_id": expense["id"],
            "period_start": PERIOD_TO,
            "period_end": PERIOD_FROM,
            "planned": 100_00,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_period"


# --- блок выключается без вреда для остальных -------------------------------


def test_vyklyuchennyy_blok_zakryt_tselikom(root_client, money):
    """Прятать пункт в меню недостаточно: адрес остаётся рабочим.

    Перебираем ВСЕ маршруты блока, а не один: пропущенный остался бы открытым, и
    выключенные финансы продолжали бы отдавать деньги фирмы тому, кто помнит
    адрес.
    """
    income, _expense = money
    add(root_client, income["id"], 42_00)

    assert root_client.post(f"{API}/modules/{MODULE_KEY}", json={"enabled": False}).status_code == 200
    try:
        addresses = (
            ("get", "/finance/categories"),
            ("post", "/finance/categories"),
            ("get", "/finance/operations"),
            ("post", "/finance/operations"),
            ("get", "/finance/profit"),
            ("get", "/finance/budgets"),
            ("post", "/finance/budgets"),
        )
        for method, path in addresses:
            # `request`, а не `client.get(json=...)`: у httpx тело в GET не
            # передаётся именованным аргументом, и вызов упал бы TypeError.
            response = root_client.request(method.upper(), f"{API}{path}", json={})
            assert response.status_code == 403, f"{method} {path} остался открытым"
            assert response.json()["error"]["code"] == "module_disabled", path
    finally:
        root_client.post(f"{API}/modules/{MODULE_KEY}", json={"enabled": True})

    # Данные на месте: выключение прячет раздел, а не стирает историю денег.
    assert profit_of(root_client)["income"] >= 42_00


def test_sosedniy_blok_ne_zamechaet_vyklyucheniya_finansov(root_client, money):
    """Выключенный блок обязан гаснуть один. Клиенты — несущий блок, и они
    отвечают одинаково до и после."""
    before = root_client.get(f"{API}/clients").status_code
    root_client.post(f"{API}/modules/{MODULE_KEY}", json={"enabled": False})
    try:
        assert root_client.get(f"{API}/clients").status_code == before == 200
    finally:
        root_client.post(f"{API}/modules/{MODULE_KEY}", json={"enabled": True})


def test_bez_prava_razdel_zakryt(root_client, finance_on):
    """Спрятать кнопку недостаточно — адрес продолжает работать.

    У «Менеджера» прав на финансы нет: набор собирается перечислением, и деньги
    фирмы в него не входят.
    """
    stranger = make_manager(root_client, "finance-stranger@test.local")
    response = stranger.get(f"{API}/finance/profit")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_zavodit_operatsii_i_pravit_spravochnik_raznye_prava():
    """Расходы заносят каждый день, а статьи заводят раз в год.

    Проверяем сам реестр, а не маршрут: набор действий блока — это решение, и
    расширить его молча (например, вернув `edit`, который нечего править)
    не должно выйти.
    """
    from core import permissions

    area = permissions.get(MODULE_KEY)
    assert area is not None, "блок не внесён в реестр — фикция файла не сработала"
    assert set(area.actions) == {"view", "create", "manage"}


def test_kazhdyy_marshrut_bloka_zakryt_pravom():
    """Тот же перебор, что в `test_route_guards`, но по своему роутеру.

    Общий перебор собирает маршруты при импорте своего файла, то есть может не
    увидеть роутер, подключённый фикцией. Пусть у блока будет своя проверка — до
    того дня, когда он подключится в `web/main.py` по-настоящему.
    """
    from fastapi.routing import APIRoute

    from web.api.routes import finance as finance_routes

    for route in finance_routes.router.routes:
        if not isinstance(route, APIRoute):
            continue
        guards = [
            getattr(dependency.call, "opencrm_permission", None)
            for dependency in route.dependant.dependencies
        ]
        found = [guard for guard in guards if guard]
        assert found, f"{sorted(route.methods)} {route.path} не закрыт правом"
        if route.methods & {"POST", "PATCH", "PUT", "DELETE"}:
            assert any(action != "view" for _area, action in found), (
                f"{sorted(route.methods)} {route.path} закрыт только правом на просмотр"
            )


# --- журнал действий ---------------------------------------------------------


def test_operatsiya_ostavlyaet_sled_v_zhurnale(root_client, money):
    """«Кто завёл этот расход» — тот же вопрос, что «кто списал две матрицы»."""
    _income, expense = money
    operatsiya = add(root_client, expense["id"], 640_00)

    # Спрашиваем про СВОЮ операцию по её номеру, а не «самую свежую с таким
    # названием статьи»: имена статей у фикстуры постоянные, и записи соседних
    # тестов ложатся под тот же ярлык. Какая из них окажется первой, решает
    # порядок файлов в прогоне, — и проверка краснела на неизменном коде.
    entries = root_client.get(
        f"{API}/audit",
        params={"entity_type": "finance_operation", "entity_id": operatsiya["id"]},
    ).json()["items"]
    mine = [row for row in entries if row["action"] == "finance.operation_added"]
    assert mine, "операция прошла мимо журнала"
    assert mine[0]["action"] == "finance.operation_added"
    # Сумма в журнал уезжает целыми минорными единицами — без валюты и формата.
    assert mine[0]["value_after"] == str(-640_00)
    assert mine[0]["actor_name"]


def test_zakrytie_stati_zapisano(root_client, money):
    income, _expense = money
    assert root_client.delete(f"{API}/finance/categories/{income['id']}").status_code == 200

    entries = root_client.get(
        f"{API}/audit", params={"entity_type": "finance_category"}
    ).json()["items"]
    mine = [row for row in entries if row["entity_label"] == income["name"]]
    assert mine and mine[0]["action"] == "finance_category.deleted"


# --- вспомогательное ---------------------------------------------------------


def test_granitsy_perioda_poluotkrytye():
    """Конец периода — начало следующего дня, а не 23:59:59.

    Иначе операция в 23:59:59.6 выпадала бы из отчёта, а в 23:59:59.4 попадала,
    и объяснить это было бы нечем.
    """
    from core.services.finance_service import _period_bounds

    start, end = _period_bounds(date(2031, 3, 1), date(2031, 3, 31), 0)
    assert start == datetime(2031, 3, 1)
    assert end == datetime(2031, 4, 1)
    assert end - start == timedelta(days=31)


def test_plan_do_kontsa_kalendarya_ne_lozhit_ekran(root_client, finance_on):
    """План с концом `9999-12-31` сохраняется — и экран планов после этого жив.

    «План до конца проекта» — законное намерение, и форма даты не ограничивает
    (`period_end: str`). Собственная проверка такую дату пропускает целиком:
    `date.fromisoformat` её разбирает, MySQL DATE хранит. А дальше счёт границ
    периода делал `period_end + timedelta(days=1)` и падал `OverflowError`-ом —
    не `ValueError`, который ловится этажом выше, и не доменной ошибкой, а
    исключением, на которое обработчика нет вовсе.

    Хуже всего то, ЧТО именно ложилось. Выдача планов отбирает всё, что
    ПЕРЕСЕКАЕТСЯ с периодом экрана, поэтому такой план попадал в каждый месяц
    начиная со своего начала. То есть экран планов переставал открываться
    навсегда — и починить его из интерфейса было нельзя, потому что интерфейс не
    открывался.
    """
    sozdana = root_client.post(
        f"{API}/finance/categories",
        json={"name": "Долгий проект 2031", "direction": "expense"},
    )
    assert sozdana.status_code == 201, sozdana.text
    statya = sozdana.json()

    sozdan = root_client.post(
        f"{API}/finance/budgets",
        json={
            "category_id": statya["id"],
            "period_start": "2026-08-01",
            "period_end": "9999-12-31",
            "planned": 50000,
        },
    )
    assert sozdan.status_code == 201, sozdan.text

    spisok = root_client.get(f"{API}/finance/budgets", params={"month": "2026-08"})
    assert spisok.status_code == 200, (
        f"экран планов лёг на плане до конца календаря: {spisok.status_code}"
    )
    assert any(p["id"] == sozdan.json()["id"] for p in spisok.json()["items"]), (
        "план сохранился, но в выдачу не попал — значит проверка смотрит не туда"
    )


def test_otchyot_s_dalyokim_kontsom_perioda_ne_lozhitsya(root_client, finance_on):
    """То же у отчётов, где конец периода приходит ПАРАМЕТРОМ ЗАПРОСА.

    Отдельно от бюджета, потому что поверхность другая и шире: план заводит
    root, а отчёт открывает любой, у кого есть право смотреть. Счёт границ там
    был тот же самый, скопированный.
    """
    otvet = root_client.get(
        f"{API}/reports/funnel", params={"from": "2026-01-01", "to": "9999-12-31"}
    )
    assert otvet.status_code == 200, (
        f"отчёт лёг на дате у края календаря: {otvet.status_code} {otvet.text[:200]}"
    )


def test_statyu_pravyat_po_odnomu_polyu(root_client, money):
    """Правка одного поля не требует пересылать всю статью целиком.

    Раньше `PATCH /finance/categories/{id}` отвечал `422 name Field required` на
    попытку поменять один `purpose`: схема правки была той же, что и схема
    заведения, а там название обязательное. У правил начисления частичная правка
    была с самого начала — рассогласование замечал тот, кто ходит в API руками.

    Сервис к частичному телу был готов всегда: он смотрит на каждое поле
    отдельно. Не хватало ровно схемы.
    """
    _income, expense = money

    odno_pole = root_client.patch(
        f"{API}/finance/categories/{expense['id']}", json={"purpose": "tax"}
    )
    assert odno_pole.status_code == 200, odno_pole.text
    assert odno_pole.json()["purpose"] == "tax"
    # Название не тронуто: незаполненное поле означает «не меняй», а не «сотри».
    assert odno_pole.json()["name"] == expense["name"]

    tolko_imya = root_client.patch(
        f"{API}/finance/categories/{expense['id']}", json={"name": "Аренда и связь"}
    )
    assert tolko_imya.status_code == 200, tolko_imya.text
    assert tolko_imya.json()["name"] == "Аренда и связь"
    assert tolko_imya.json()["purpose"] == "tax", "правка имени сбросила назначение"

    # Направление по-прежнему не меняется, и отказ по-прежнему внятный, а не
    # «лишнее поле»: ради этого `direction` в схеме правки объявлен.
    perevernut = root_client.patch(
        f"{API}/finance/categories/{expense['id']}", json={"direction": "income"}
    )
    assert perevernut.status_code == 422, perevernut.text
    assert perevernut.json()["error"]["code"] == "direction_is_fixed"
