"""Потолок проверки не выше вместимости колонки, куда значение ложится.

Правило одно и записано оно не здесь, а в `database/models/finance.py` — там,
где его однажды уже вывели правильно:

    «Число взято не с потолка и не из представлений о том, сколько бывает денег:
     это то, что помещается в колонку. `Integer` в MySQL — это INT, 4 байта, то
     есть 2 147 483 647 минорных единиц. Операция крупнее туда просто не влезет,
     и узнать об этом отказом вставки на боевом сервере было бы худшим из
     вариантов.»

Финансы этому следовали (`MAX_AMOUNT_MINOR = 2_000_000_000`), а заявки и склад —
нет: у обоих стояло `10**12`, то есть в 465 раз больше вместимости. Отказ,
названный там «худшим из вариантов», у них и происходил.

**Чем он плох именно.** Значение проходит СВОЮ проверку и упирается в чужую —
в MySQL. Тот отвечает 1264 «Out of range value», pymysql поднимает `DataError`,
а обработчика на этот класс нет: в `web/main.py` зарегистрированы только
`DomainError` и `RequestValidationError`. Человек получает пятисотку без
подсказки вместо «сумма слишком велика». Диапазон отказа — от 2 147 483 648 до
10^12, то есть 465 из каждых 466 сумм, которые собственная проверка пропускала.

Тот же силуэт, что у пароля длиннее 72 байт (`tests/test_auth.py`): своя
проверка пройдена, чужая — нет. Поэтому проверок здесь две: одна опытом через
настоящую ручку, вторая — механическая, на все потолки сразу.
"""

import ast
import pathlib
import re

import pytest

from tests.conftest import API

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Вместимость `Integer` в MySQL: signed INT, 4 байта.
INT_MAX = 2_147_483_647

DEALS = f"{API}/deals"
STOCK = f"{API}/warehouse"


def test_summa_vyshe_vmestimosti_kolonki_otvergaetsya_ponyatno(manager_client):
    """Сумма, не влезающая в колонку, получает 422 с кодом, а не 500.

    Вход не выдуманный: 3 000 000 000 минорных единиц — это 30 миллионов в
    валюте с двумя знаками. Для стройки, техники или валюты со слабым курсом
    сумма обычная, а до правки она отвечала пятисоткой.
    """
    klient = manager_client.post(f"{API}/clients", json={"name": "Крупный заказ"}).json()
    otvet = manager_client.post(
        DEALS,
        json={"title": "Стройка", "client_id": klient["id"], "amount": 3_000_000_000},
    )
    assert otvet.status_code == 422, f"ожидался понятный отказ, пришло {otvet.status_code}"
    assert otvet.json()["error"]["code"] == "money_too_large"


def test_predoplata_proveryaetsya_tem_zhe_potolkom(manager_client):
    """`prepaid` разбирается той же функцией и ложится в такую же колонку.

    Отдельно от суммы, потому что поправить одно поле и забыть соседнее — самый
    вероятный способ починить это наполовину.
    """
    klient = manager_client.post(f"{API}/clients", json={"name": "Предоплата"}).json()
    otvet = manager_client.post(
        DEALS,
        json={"title": "Аванс", "client_id": klient["id"], "prepaid": 3_000_000_000},
    )
    assert otvet.status_code == 422, f"ожидался понятный отказ, пришло {otvet.status_code}"
    assert otvet.json()["error"]["code"] == "money_too_large"


@pytest.fixture()
def sklad_vklyuchyon(root_client):
    """Блок склада по умолчанию выключен — включаем на время проверки."""
    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    yield
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})


def test_kolichestvo_vyshe_vmestimosti_kolonki_otvergaetsya_ponyatno(root_client, sklad_vklyuchyon):
    """То же у склада: количество хранится в тысячных, и предел тот же.

    2 147 483 647 тысячных — это 2 147 483 единицы товара. Приход на три
    миллиона штук в тысячных даёт три миллиарда и не влезает.
    """
    tovar = root_client.post(
        f"{STOCK}/products",
        json={"name": "Крепёж", "sku": "POTOLOK-1", "cost": "100", "price": "500"},
    ).json()
    assert "id" in tovar, f"товар не завёлся: {tovar}"
    otvet = root_client.post(
        f"{STOCK}/moves",
        json={"product_id": tovar["id"], "kind": "in", "quantity": "3000000"},
    )
    assert otvet.status_code == 422, f"ожидался понятный отказ, пришло {otvet.status_code}"
    assert otvet.json()["error"]["code"] == "quantity_too_large"


# --- механическая часть ------------------------------------------------------

#: Потолки, которым позволено быть выше вместимости INT, и почему.
#:
#: Пусто, и это осознанно: сегодня все такие потолки стерегут колонки `Integer`.
#: Запись сюда — это обещание, что значение ложится в BIGINT; появится она —
#: рядом обязана появиться и расширяющая миграция.
RAZRESHENO_VYSHE_INT: dict[str, str] = {}


def _chislo(uzel: ast.expr) -> int | None:
    """Значение узла, если это целое или простая арифметика над целыми.

    `ast.literal_eval` здесь НЕ годится, и это выяснилось покраснением: оба
    виновных потолка были записаны как `10**12`, а возведение в степень к
    литералам не относится — перебор их молча пропускал. Проверка при этом
    выглядела рабочей: она находила соседние константы и была зелёной, не видя
    ровно того, ради чего заведена.

    Считаем сами и только четыре действия над числами. Ни имён, ни вызовов:
    выражение, которое нельзя посчитать по дереву, не считается потолком вовсе —
    лучше не увидеть, чем выполнить чужой код в проверке.
    """
    if isinstance(uzel, ast.Constant):
        return uzel.value if isinstance(uzel.value, int) and not isinstance(uzel.value, bool) else None
    if isinstance(uzel, ast.UnaryOp) and isinstance(uzel.op, (ast.UAdd, ast.USub)):
        vnutri = _chislo(uzel.operand)
        return None if vnutri is None else (vnutri if isinstance(uzel.op, ast.UAdd) else -vnutri)
    if isinstance(uzel, ast.BinOp):
        levo, pravo = _chislo(uzel.left), _chislo(uzel.right)
        if levo is None or pravo is None:
            return None
        if isinstance(uzel.op, ast.Pow):
            # Степень считаем только скромную: `2**10000` в проверке — это
            # секунды счёта на пустом месте.
            return levo**pravo if 0 <= pravo <= 64 else None
        if isinstance(uzel.op, ast.Mult):
            return levo * pravo
        if isinstance(uzel.op, ast.Add):
            return levo + pravo
        if isinstance(uzel.op, ast.Sub):
            return levo - pravo
    return None


def _potolki_v_kode() -> list[tuple[str, str, int]]:
    """Все константы уровня модуля вида MAX_* с целым значением.

    Перебором по дереву, а не по списку: смысл проверки в том, чтобы её не
    пришлось дополнять руками при появлении шестого потолка.
    """
    nayd = []
    for put in sorted((ROOT / "core").rglob("*.py")):
        if "__pycache__" in put.parts:
            continue
        rel = put.relative_to(ROOT).as_posix()
        derevo = ast.parse(put.read_text(encoding="utf-8"))
        for uzel in derevo.body:
            if not isinstance(uzel, ast.Assign):
                continue
            for tsel in uzel.targets:
                if not (isinstance(tsel, ast.Name) and re.match(r"^MAX_[A-Z_]+$", tsel.id)):
                    continue
                znachenie = _chislo(uzel.value)
                if znachenie is not None:
                    nayd.append((rel, tsel.id, znachenie))
    return nayd


def test_ni_odin_potolok_ne_vyshe_vmestimosti_kolonki():
    """Ни один MAX_* не обещает больше, чем влезет в INT.

    Механически, потому что шестой такой потолок появится тем же путём, что и
    первые два: кто-то напишет «с запасом» число, не сверив его с колонкой.
    Проверка не знает, в какую именно колонку ложится значение, и знать не
    должна: в этом проекте все такие пределы стерегут `Integer`, а исключение
    обязано быть названным (`RAZRESHENO_VYSHE_INT`) и подкреплённым миграцией.
    """
    potolki = _potolki_v_kode()
    assert potolki, "перебор не нашёл ни одного потолка — смотрит не туда"

    vinovnye = [
        f"{rel}:{imya} = {znachenie:_} (в INT влезает {INT_MAX:_})"
        for rel, imya, znachenie in potolki
        if znachenie > INT_MAX and imya not in RAZRESHENO_VYSHE_INT
    ]
    assert vinovnye == [], (
        "потолок обещает больше, чем влезет в колонку — значит вместо понятного "
        "отказа человек получит пятисотку из MySQL:\n  " + "\n  ".join(vinovnye)
    )


def test_perebor_potolkov_vidit_izvestnye():
    """Перебор обязан находить те потолки, про которые известно, что они есть."""
    imena = {f"{rel}:{imya}" for rel, imya, _ in _potolki_v_kode()}
    for ozhidaemyy in (
        "core/services/deal_service.py:MAX_MONEY",
        "core/services/warehouse_service.py:MAX_QUANTITY",
    ):
        assert ozhidaemyy in imena, f"перебор потерял {ozhidaemyy}"


def test_v_spiske_isklyucheniy_net_lishnikh():
    """Названное исключение обязано существовать в коде.

    Иначе список становится памяткой о прошлом, а не действующим разрешением, и
    следующий читатель поверит, что где-то есть колонка BIGINT.
    """
    imena = {imya for _, imya, _ in _potolki_v_kode()}
    propavshie = sorted(set(RAZRESHENO_VYSHE_INT) - imena)
    assert propavshie == [], "в списке исключений записи без кода: " + ", ".join(propavshie)
