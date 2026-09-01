"""Строки заявки: из чего складывается её сумма.

Здесь проверяется не «ответ 201», а арифметика итога и то, ради чего строки
заводились: сумма перестала быть числом, набранным на глаз. Отдельно стоит
сторож кэша `deals.amount` — сознательного отступления от правила «производное
не хранится» (`docs/19-sborka-zakaza.md` §Р5).
"""

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database.models import Deal, DealLine
from tests.conftest import API

WH = f"{API}/warehouse"
KOREN = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def sklad_vklyuchen(root_client: TestClient):
    """Раздел строк закрыт блоком склада целиком — без него его нет."""
    from core.services import modules_service

    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    modules_service.invalidate()
    yield
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    modules_service.invalidate()


#: Заявки, которым этот файл заводил строки. Записываем при рождении, а не
#: ищем потом по таблице: база у набора одна на весь прогон, и сводная проверка
#: по всей таблице краснела бы от чужой правки — то есть указывала бы не туда.
NASHI_ZAYAVKI: set[int] = set()


@pytest.fixture
def zayavka(root_client: TestClient) -> int:
    klient = root_client.post(f"{API}/clients", json={"name": "Заказчик строк"}).json()
    otvet = root_client.post(
        f"{API}/deals", json={"title": "Поставка серверов", "client_id": klient["id"]}
    )
    assert otvet.status_code == 201, otvet.text
    NASHI_ZAYAVKI.add(otvet.json()["id"])
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


def test_stroka_nabiraetsya_skanom(root_client, zayavka):
    """У стойки коробка в руках, и набирать название никто не станет."""
    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    server = tovar(root_client, name="Товар со штрихкодом")
    kod = "4600000000208"
    assert (
        root_client.post(
            f"{API}/labels/products/{server['id']}/barcodes", json={"code": kod}
        ).status_code
        == 201
    )

    dobavlena = stroka(root_client, zayavka, code=kod, quantity="1")
    assert dobavlena["product_id"] == server["id"]
    assert dobavlena["kind"] == "product"


def test_neizvestnyy_kod_govorit_chto_iskali(root_client, zayavka):
    """Пустой ответ после писка сканера читается как «сканер сломался»."""
    otkaz = root_client.post(
        f"{API}/deals/{zayavka}/lines", json={"code": "4600000000901", "quantity": "1"}
    )
    assert otkaz.status_code == 404, otkaz.text
    assert otkaz.json()["error"]["code"] == "barcode_unknown"
    assert "4600000000901" in otkaz.json()["error"]["message"]


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

    Сверяются только заявки ЭТОГО файла. Проверка по всей таблице зависела бы
    от порядка файлов — прямой и обратный проходы видели бы разное множество, —
    и краснела бы от чужой правки, уводя разбор в чужой файл.
    """
    assert NASHI_ZAYAVKI, "ни одной заявки не заведено — сверять нечего"
    po_strokam = dict(
        db.execute(
            select(
                DealLine.deal_id,
                func.sum(
                    (DealLine.price_minor * DealLine.quantity_milli).op("DIV")(1000)
                ),
            )
            .where(DealLine.price_minor.is_not(None), DealLine.deal_id.in_(NASHI_ZAYAVKI))
            .group_by(DealLine.deal_id)
        ).all()
    )
    so_strokami = set(
        db.scalars(
            select(DealLine.deal_id).where(DealLine.deal_id.in_(NASHI_ZAYAVKI)).distinct()
        )
    )

    for deal in db.scalars(select(Deal).where(Deal.id.in_(so_strokami))):
        dolzhno = int(po_strokam.get(deal.id, 0))
        assert deal.amount == dolzhno, f"заявка {deal.id}: {deal.amount} вместо {dolzhno}"


def test_summu_zayavki_so_strokami_rukami_ne_perepisat(root_client, zayavka):
    """Сумма заявки со строками — итог, а не поле для ввода.

    Прими её здесь — и кэш разойдётся с истиной: следующая правка строки затрёт
    введённое, а в журнале правки суммы через строки не будет вовсе. На боевой
    базе узнать, какое из двух чисел верное, будет неоткуда.
    """
    stroka(root_client, zayavka, name="Работа", quantity="1", price=100_000)

    otkaz = root_client.patch(f"{API}/deals/{zayavka}", json={"amount": 999_000})
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "amount_from_lines"
    assert root_client.get(f"{API}/deals/{zayavka}").json()["amount"] == 100_000


def test_ekran_zapiraet_summu_i_perechityvaet_eyo(root_client, zayavka):
    """Отказ сервера должен быть виден на карточке ДО нажатия, а сумма — свежей.

    Найдено живой пробой (CLAUDE.md §2), обе половины. Поле суммы оставалось
    обычным полем ввода: человек правил его, жал сохранить и получал отказ,
    которого ничто не предвещало. А карточка держала СВОЮ копию суммы и после
    правки строк показывала $408 у заявки, у которой суммы уже не было вовсе.

    Браузера в наборе нет, поэтому правило держится чтением исходника — тот же
    приём, что в `tests/test_screens.py`. Проверять на настоящем экране дороже
    самого правила, а разойтись эти два места могут только правкой вот здесь.
    """
    karta = (KOREN / "web/frontend/crm/src/screens/DealCard.tsx").read_text(encoding="utf-8")
    stroki_tsx = (KOREN / "web/frontend/crm/src/components/DealLines.tsx").read_text(encoding="utf-8")

    assert "readOnly={strok > 0}" in karta, "поле суммы правится руками при наборе строк"
    assert 't("amountFromLines")' in karta, "поле заперто молча — непонятно почему"
    assert "onSostav" in stroki_tsx and "onSostav" in karta, "карточка не знает про строки"
    assert "if (deal && deal.amount !== itog) void load();" in karta, (
        "карточка не перечитывает себя — покажет устаревшую сумму"
    )
    assert "key={`amount-${deal.amount}`}" in karta, (
        "поле не пересоздаётся: у неуправляемого `defaultValue` не перечитывается"
    )


def test_itog_vyshe_vmestimosti_kolonki_otvergaetsya_ponyatno(root_client, zayavka):
    """Оба сомножителя в своём потолке, а произведение — уже нет.

    `deals.amount` — INT, а считается он как цена × количество ÷ 1000. Два
    миллиарда за штуку и две штуки проходят каждый свою проверку и дают четыре
    миллиарда: MySQL отвечает 1264, обработчика на этот класс нет, и человек
    получает пятисотку без подсказки.
    """
    otvet = root_client.post(
        f"{API}/deals/{zayavka}/lines",
        json={"name": "Слишком дорого", "quantity": "2", "price": 2_000_000_000},
    )
    assert otvet.status_code != 500, f"пятисотка вместо отказа: {otvet.text}"
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"], "отказ без кода — человеку нечего прочесть"


def test_udalyonnyy_tovar_v_stroku_ne_stavitsya(root_client, zayavka):
    """Удалённый товар не продают: он ушёл из списков не случайно."""
    server = tovar(root_client, name="Товар на удаление в строку")
    assert root_client.delete(f"{WH}/products/{server['id']}").status_code == 200

    otkaz = root_client.post(
        f"{API}/deals/{zayavka}/lines", json={"product_id": server["id"], "quantity": "1"}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "product_deleted"


def test_nesushchestvuyushchiy_artikul_nazvan_svoim_kodom(root_client, zayavka):
    """По коду отказа магазин решает, повторять ли запрос, а экран — что
    подсветить. Общий 422 без кода не отвечает ни на один из вопросов."""
    otkaz = root_client.post(
        f"{API}/deals/{zayavka}/lines", json={"sku": "НЕТ-ТАКОГО", "quantity": "1"}
    )
    assert otkaz.status_code == 404, otkaz.text
    assert otkaz.json()["error"]["code"] == "product_not_found"


def test_slishkom_dlinnoe_nazvanie_otkaz_a_ne_obrezka(root_client, zayavka):
    """Обрезка увезла бы урезанное название в счёт клиенту."""
    otkaz = root_client.post(
        f"{API}/deals/{zayavka}/lines",
        json={"name": "я" * 201, "quantity": "1", "price": 100},
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "name_too_long"


def test_skan_perebivaet_ostavsheesya_v_forme(root_client, zayavka):
    """Код ищется ПЕРВЫМ, и это не мелочь.

    У стойки коробка в руках, а в форме мог остаться товар от прошлой строки.
    Победи `product_id` — в заявку легло бы то, чего сканер не видел, и
    заметили бы это при отгрузке.
    """
    root_client.post(f"{API}/modules/labels", json={"enabled": True})
    chuzhoy = tovar(root_client, name="Остался в форме")
    nuzhnyy = tovar(root_client, name="Тот, что в руках")
    kod = "4600000000505"
    assert (
        root_client.post(
            f"{API}/labels/products/{nuzhnyy['id']}/barcodes", json={"code": kod}
        ).status_code
        == 201
    )

    dobavlena = stroka(root_client, zayavka, code=kod, product_id=chuzhoy["id"], quantity="1")
    assert dobavlena["product_id"] == nuzhnyy["id"], "в строку лёг товар из формы, а не из скана"


def test_bez_bloka_sklada_razdel_strok_ischezaet_tselikom(root_client, zayavka):
    """Пустой список вместо отказа — это «выключено» на словах.

    Общий обход адресов сюда не доходит: он отбрасывает пути с параметром, а
    сторож маршрутов про блоки не знает вовсе. Значит единственный сторож этих
    четырёх ручек — вот этот тест. Адрес остаётся в закладках, и раздел,
    отдающий пустой список, выглядит работающим и пустым, то есть врёт дважды.
    """
    from core.services import modules_service

    dobavlena = stroka(root_client, zayavka, name="Работа", quantity="1", price=1000)
    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": False}).status_code == 200
    modules_service.invalidate()
    try:
        otvety = {
            "GET": root_client.get(f"{API}/deals/{zayavka}/lines"),
            "POST": root_client.post(
                f"{API}/deals/{zayavka}/lines", json={"name": "Ещё", "quantity": "1"}
            ),
            "PATCH": root_client.patch(
                f"{API}/deals/{zayavka}/lines/{dobavlena['id']}", json={"quantity": "2"}
            ),
            "DELETE": root_client.delete(f"{API}/deals/{zayavka}/lines/{dobavlena['id']}"),
        }
        for metod, otvet in otvety.items():
            assert otvet.status_code == 403, f"{metod} ответил {otvet.status_code}: {otvet.text}"
            assert otvet.json()["error"]["code"] == "module_disabled", metod
        # Сама заявка при этом жива: закрылся склад, а не заявки.
        assert root_client.get(f"{API}/deals/{zayavka}").status_code == 200
    finally:
        assert root_client.post(
            f"{API}/modules/warehouse", json={"enabled": True}
        ).status_code == 200
        modules_service.invalidate()

    # Включили обратно — строка на месте: выключение убирает с глаз, а не стирает.
    posle = root_client.get(f"{API}/deals/{zayavka}/lines").json()
    assert [s["id"] for s in posle["items"]] == [dobavlena["id"]]


def test_pribyl_schitaetsya_tolko_pri_polnoy_sebestoimosti(root_client, zayavka):
    """Неполная себестоимость завысила бы прибыль там, где решают о скидке.

    У своей траты себестоимости нет по существу, у товара её могли не назвать.
    Сложить то, что есть, и назвать это себестоимостью — значит показать
    прибыль ВЫШЕ настоящей и в самый неподходящий момент.
    """
    server = tovar(root_client, price=4_500_000, cost=3_900_000)
    stroka(root_client, zayavka, product_id=server["id"], quantity="2")

    est = root_client.get(f"{API}/deals/{zayavka}/lines").json()
    assert est["cost_minor"] == 2 * 3_900_000
    assert est["profit_minor"] == 2 * (4_500_000 - 3_900_000)

    # Дописали свою трату — себестоимости у неё нет, и прибыль пропадает.
    stroka(root_client, zayavka, name="Упаковка", quantity="1", price=250_000)
    posle = root_client.get(f"{API}/deals/{zayavka}/lines").json()
    assert posle["profit_minor"] is None, "прибыль посчитана по неполной себестоимости"
    assert posle["cost_minor"] is None


def test_pribyl_zakryta_pravom_na_summy(root_client, zayavka):
    """Прибыль — это маржа, и закрывается тем же правом, что цена и итог."""
    server = tovar(root_client, price=4_500_000, cost=3_900_000)
    stroka(root_client, zayavka, product_id=server["id"], quantity="1")
    est = root_client.get(f"{API}/deals/{zayavka}/lines").json()
    assert est["profit_minor"] is not None
    # Сам отказ по праву проверяется в test_roles.py — здесь важно, что поле
    # есть в ответе и что оно того же рода, что цена и итог.
    assert set(("total_minor", "cost_minor", "profit_minor")) <= set(est)


#: Маршруты роутера заявок и блок, которым каждый закрыт. Пусто — своё, заявочное.
#:
#: Список выписан НАРОЧНО. Заявки — блок несущий, поэтому общего охранника у
#: роутера нет, и он единственный в проекте вешает блоки ПО МАРШРУТУ. Все
#: соседи закрыты целиком роутером и прямо объясняют почему: «пропущенный
#: маршрут остался бы открытым». Здесь пропустить его можно, и заметить это
#: чтением нельзя — поэтому перебор.
#:
#: Ключ — МЕТОД и путь, а не путь. Пока ключом был путь, четыре метода одного
#: адреса складывались в одну запись, последний затирал первого, и снятый
#: охранник с `GET /deals/{id}/lines` перебор не замечал вовсе. Поймано
#: подрывом: проверка была зелёной ровно там, где нужна.
BLOKI_MARSHRUTOV_ZAYAVOK = {
    ("GET", "/deals"): None,
    ("POST", "/deals"): None,
    ("GET", "/deals/board"): None,
    ("GET", "/deals/{deal_id}"): None,
    ("PATCH", "/deals/{deal_id}"): None,
    ("DELETE", "/deals/{deal_id}"): None,
    ("POST", "/deals/{deal_id}/move"): None,
    ("GET", "/deals/{deal_id}/feed"): None,
    ("POST", "/deals/{deal_id}/feed"): None,
    ("GET", "/deals/{deal_id}/lines"): "warehouse",
    ("POST", "/deals/{deal_id}/lines"): "warehouse",
    ("PATCH", "/deals/{deal_id}/lines/{line_id}"): "warehouse",
    ("DELETE", "/deals/{deal_id}/lines/{line_id}"): "warehouse",
    ("POST", "/deals/{deal_id}/order"): "orders",
}


def test_v_routere_zayavok_net_neobyavlennykh_marshrutov():
    """Новый подраздел заявки обязан объявить свой блок — или сказать, что свой.

    Проверка не судит, ВЕРНЫЙ ли блок: она требует, чтобы решение было принято.
    Незаявленный маршрут — это либо забытый охранник, либо забытая строка тут,
    и оба случая разбираются одинаково: открыть роутер и посмотреть.
    """
    import re

    tekst = (KOREN / "web/api/routes/deals.py").read_text(encoding="utf-8")
    obrazets = r'@router\.(get|post|patch|put|delete)\(\s*"([^"]*)"(.*?)\)\s*\ndef '
    najdeno = {}
    for metod, put, hvost in re.findall(obrazets, tekst, re.S):
        blok = re.search(r'require_module\("([^"]+)"\)', hvost)
        najdeno[(metod.upper(), "/deals" + put)] = blok.group(1) if blok else None

    assert najdeno == BLOKI_MARSHRUTOV_ZAYAVOK, (
        "роутер заявок разошёлся со списком блоков.\n"
        f"  в коде:   {sorted(najdeno.items())}\n"
        f"  в списке: {sorted(BLOKI_MARSHRUTOV_ZAYAVOK.items())}"
    )


def test_itog_zayavki_ravem_summe_pokazannykh_strok(root_client, zayavka):
    """Итог обязан сойтись с тем, что человек складывает глазами.

    Дробное количество делает произведение дробным в тысячных: цена 3,33 на
    1,5 метра — это 4,995, а копейки дробной не бывает. Строка показывает 4,99;
    две такие строки на экране дают 9,98. Итог же считался ОДНИМ делением по
    сумме произведений и давал 9,99.

    Одна копейка — но она разводит счёт клиента с нашим, и разводит молча:
    столбец в накладной складывается в одно число, а внизу стоит другое. На
    двадцати строках расхождение становится двадцатью копейками, и объяснить
    его нельзя ничем, кроме «у нас так считается».

    Поэтому итог складывает ПОКАЗАННЫЕ строки, а не точные произведения.
    Точность при этом ниже на доли копейки в нашу сторону — это осознанный
    размен: сойтись с видимым важнее, чем не потерять полкопейки.
    """
    for _ in range(2):
        otvet = root_client.post(
            f"{API}/deals/{zayavka}/lines",
            json={"name": "Кабель", "quantity": "1.5", "price": 333},
        )
        assert otvet.status_code == 201, otvet.text

    spisok = root_client.get(f"{API}/deals/{zayavka}/lines").json()
    stroki = [s["total_minor"] for s in spisok["items"]]
    assert stroki == [499, 499], f"строка округлена не вниз: {stroki}"
    assert spisok["total_minor"] == sum(stroki), (
        f"итог {spisok['total_minor']} не сходится со столбцом {sum(stroki)} = {stroki}"
    )
    # И то же число лежит в кэше заявки: у суммы один писатель (§Р5).
    assert root_client.get(f"{API}/deals/{zayavka}").json()["amount"] == sum(stroki)
