"""Порядок и категории в списках бумаг: отбор по виду, сортировка, счёт.

Заказ владельца 02.09.2026: «удобные сортировки и возможность скрыть
определённые категории». Разбор — `docs/dizayn/20-udobstvo-i-spravka.md`, работа 2.

Три беды, которые здесь стерегутся, и все три выглядят как исправная работа:

- **сортировка после страницы.** Разложить сто приехавших строк — не то же
  самое, что разложить тысячу и взять сто: ответ выглядит отсортированным и
  неверен. Проверяется сравнением второй страницы с истинным порядком;
- **счёт категорий, посчитанный С отбором по категории.** Свернул человек
  квитанции — и число рядом с ними пропало вместе с ними, то есть пропал и
  способ их вернуть. Счёт обязан считаться без той оси, по которой отбирают;
- **незнакомый ключ, принятый молча.** Попросил «по номеру», получил по дате и
  уверен, что так и надо. Незнакомый вид и незнакомый порядок — отказ.
"""

import itertools

from tests.conftest import API

DOCS = f"{API}/documents"
ORDERS = f"{API}/orders"


#: Своя метка на каждый заход проверки.
#:
#: **База у набора общая, и окно страницы обрезано.** Проверка порядка,
#: сравнивающая «двести свежих» с «двумястами старых», на пустой базе зелёная, а
#: в наборе — нет: при более чем двухстах бумагах это два РАЗНЫХ окна, и
#: обратными друг другу они не будут никогда. Поймано воротами, локально было
#: зелено. Поэтому каждая проверка порядка работает только со своими бумагами и
#: отбирает их поиском по метке.
_schyot = itertools.count(1)


def metka() -> str:
    return f"poryadok-{next(_schyot):04d}"


def zavesti(manager_client, item: str, **extra):
    """Квитанция приёмки с приметным предметом: список у набора общий."""
    client = manager_client.post(
        f"{API}/clients", json={"name": f"Порядок {item}"}
    ).json()
    body = {"client_id": client["id"], "item": item}
    body.update(extra)
    otvet = manager_client.post(DOCS, json=body)
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def spisok(client, **params):
    zapros = "&".join(f"{k}={v}" for k, v in params.items())
    otvet = client.get(f"{DOCS}?{zapros}" if zapros else DOCS)
    assert otvet.status_code == 200, otvet.text
    return otvet.json()


def test_neizvestnyy_vid_i_poryadok_otkaz_a_ne_molchanie(manager_client):
    plohoy_vid = manager_client.get(f"{DOCS}?kind=vydumannyy")
    assert plohoy_vid.status_code == 422
    assert plohoy_vid.json()["error"]["code"] == "unknown_kind"

    plohoy_poryadok = manager_client.get(f"{DOCS}?sort=po_nastroeniyu")
    assert plohoy_poryadok.status_code == 422
    assert plohoy_poryadok.json()["error"]["code"] == "unknown_sort"
    # В отказе названо, что бывает: без перечня человеку остаётся угадывать.
    assert "new" in plohoy_poryadok.json()["error"]["message"]


def test_otbor_po_vidu_suzhaet_spisok(manager_client):
    zavesti(manager_client, "Отбор по виду")
    tolko_kvitantsii = spisok(manager_client, kind="intake", per_page=200)
    vidy = {row["kind"] for row in tolko_kvitantsii["items"]}
    assert vidy <= {"intake"}, f"в отборе по квитанциям приехало лишнее: {vidy}"
    assert tolko_kvitantsii["total"] >= 1


def test_schyot_kategoriy_ne_ischezaet_vmeste_s_otborom(manager_client):
    """Свернул категорию — её число обязано остаться. Иначе не вернуть.

    Счёт по видам считается БЕЗ отбора по виду. Если считать с ним, ответ на
    `?kind=intake` содержал бы одну строку счёта, и остальные категории пропали
    бы с экрана вместе со своими числами — то есть вместе со способом их
    развернуть обратно.
    """
    zavesti(manager_client, "Счёт категорий")
    vse = spisok(manager_client, per_page=1)
    assert vse["counts"].get("intake", 0) >= 1, "квитанции не сосчитались"

    suzheno = spisok(manager_client, kind="intake", per_page=1)
    assert suzheno["counts"] == vse["counts"], (
        "счёт категорий поехал вслед за отбором по категории: "
        f"было {vse['counts']}, при отборе {suzheno['counts']}"
    )


def test_schyot_kategoriy_slushaetsya_sosednikh_otborov(manager_client):
    """Отбор по ДРУГОЙ оси счёт менять обязан: иначе он врёт про экран."""
    moya = metka()
    zavesti(manager_client, f"Счёт слушается поиска {moya}")
    s_poiskom = spisok(manager_client, search=moya, per_page=1)
    assert s_poiskom["counts"].get("intake") == 1, (
        f"поиск по «{moya}» дал счёт {s_poiskom['counts']}, а бумага одна"
    )
    assert sum(s_poiskom["counts"].values()) == s_poiskom["total"], (
        "сумма по категориям обязана сойтись с «всего»: "
        f"{s_poiskom['counts']} против {s_poiskom['total']}"
    )


def test_poryadok_prikladyvaetsya_k_zaprosu_a_ne_k_stranitse(manager_client):
    """Вторая страница обязана быть второй страницей ОБЩЕГО порядка.

    Беда, ради которой проверка написана: разложить приехавшую сотню строк
    выглядит точно так же, как разложить всю тысячу и взять из неё сотню, — и
    отличается только ответом. Ловится сравнением страниц с истинным порядком:
    берём список целиком, режем сами и сверяем.
    """
    moya = metka()
    for nomer in range(5):
        zavesti(manager_client, f"Страничный порядок {moya} {nomer}")

    tselikom = spisok(manager_client, search=moya, sort="old", per_page=200)["items"]
    assert len(tselikom) == 5, f"поиск по метке нашёл не свои бумаги: {len(tselikom)}"
    nomera = [row["number"] for row in tselikom]

    vtoraya = spisok(manager_client, search=moya, sort="old", per_page=2, page=2)["items"]
    assert [row["number"] for row in vtoraya] == nomera[2:4], (
        "вторая страница разошлась с общим порядком — похоже, сортируют уже "
        "приехавшую страницу"
    )


def test_starye_sverkhu_eto_obratnyy_poryadok(manager_client):
    moya = metka()
    for nomer in range(3):
        zavesti(manager_client, f"Обратный порядок {moya} {nomer}")
    svezhie = [r["number"] for r in spisok(manager_client, search=moya, sort="new", per_page=200)["items"]]
    starye = [r["number"] for r in spisok(manager_client, search=moya, sort="old", per_page=200)["items"]]
    assert len(svezhie) == 3, f"поиск по метке нашёл не свои бумаги: {svezhie}"
    assert starye == list(reversed(svezhie)), (
        "«старые сверху» обязаны быть тем же списком наоборот"
    )


def test_poryadok_po_nomeru_polnyy(manager_client):
    """Номер уникален, значит порядок по нему определён без разрешителя ничьей."""
    moya = metka()
    for nomer in range(3):
        zavesti(manager_client, f"Порядок по номеру {moya} {nomer}")
    nomera = [
        r["number"]
        for r in spisok(manager_client, search=moya, sort="number", per_page=200)["items"]
    ]
    assert len(nomera) == 3, f"поиск по метке нашёл не свои бумаги: {nomera}"
    assert nomera == sorted(nomera, reverse=True), "по номеру разложено не по номеру"


def test_zakazy_schitayut_kategorii_po_sostoyaniyu(root_client):
    """У заказов категория — состояние: вид там выбран чипами, и их два."""
    assert root_client.post(f"{API}/modules/documents", json={"enabled": True}).status_code == 200
    assert root_client.post(f"{API}/modules/orders", json={"enabled": True}).status_code == 200
    from core.services import modules_service

    modules_service.invalidate()

    otvet = root_client.get(f"{ORDERS}?per_page=1")
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    assert "counts" in telo, "список заказов не назвал категорий вовсе"
    assert sum(telo["counts"].values()) == telo["total"], (
        f"сумма по состояниям {telo['counts']} разошлась с «всего» {telo['total']}"
    )

    plohoy = root_client.get(f"{ORDERS}?sort=po_nastroeniyu")
    assert plohoy.status_code == 422
    assert plohoy.json()["error"]["code"] == "unknown_sort"
