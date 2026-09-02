"""Акт выполненных работ: одно действие вместо трёх.

Проверяется здесь не «создаётся ли акт», а **целостность**: списание материалов,
фиксация работ и перевод заявки обязаны случаться вместе или не случаться вовсе.
Половина проведения — это расхождение, которое потом никто не восстановит:
детали ушли с полки, а работа числится незакрытой, — и узнать, что именно
имелось в виду, будет неоткуда.

Поэтому половина файла — про отказы посередине, и каждый тест ломает свою
часть цепочки: не хватило на складе, заявку успели передвинуть, нажали дважды,
склад выключен. Ожидание одно и то же с двух сторон: **не сделалось ничего**.
"""

import itertools
import re

import pytest
from sqlalchemy import select

from core import events
from core import exceptions as errors
from core.services.deal_service import DEAL_STAGE_CHANGED
from database.models.document import (
    KIND_WAYBILL_OUT,
    STATUS_DRAFT,
    STATUS_ISSUED,
    Document,
    DocumentLine,
)
from database.repositories import documents as documents_repo
from tests.conftest import API
from web.api.routes.documents import ACT_PRINT_STRINGS

ACTS = f"{API}/documents/acts"
DOCS = f"{API}/documents"
DEALS = f"{API}/deals"
STOCK = f"{API}/warehouse"

_counter = itertools.count(1)


def uniq() -> str:
    return f"{next(_counter):05d}"


@pytest.fixture(autouse=True)
def blocks_on(root_client):
    """Акт стоит на бланках, а списывать ему нужен склад."""
    for key in ("documents", "warehouse"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    yield


@pytest.fixture
def deal(root_client):
    client = root_client.post(f"{API}/clients", json={"name": f"Заказчик {uniq()}"}).json()
    created = root_client.post(
        DEALS, json={"title": f"Работа {uniq()}", "client_id": client["id"]}
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture
def refuse_stage_change():
    """Сломать смену этапа на время теста — участником, который отказывает.

    Так воспроизводится «ошибка посередине»: склад к этому моменту уже списан
    (подписчик склада идёт раньше), а воронка ещё нет. Именно это состояние и не
    должно пережить запрос.

    Снимаем в фикстуре, а не в конце теста: тело до конца может и не дойти, а
    забытый подписчик посыплет совершенно посторонние файлы — реестр подписок
    глобальный.
    """
    added = []

    def refuses(event):
        raise errors.ValidationError("Воронка сломалась", code="pipeline_broke")

    def install():
        added.append(events.subscribe(DEAL_STAGE_CHANGED, refuses, is_participant=True))

    yield install
    for sub in added:
        events.unsubscribe(sub)


def product(root_client, stock="10", cost="10000", price="50000", service=False):
    item = root_client.post(
        f"{STOCK}/products",
        json={
            "name": f"Деталь {uniq()}", "sku": f"ACT-{uniq()}",
            "cost": None if service else cost, "price": price, "is_service": service,
        },
    ).json()
    if stock and not service:
        root_client.post(
            f"{STOCK}/moves", json={"product_id": item["id"], "kind": "in", "quantity": stock}
        )
    return item


def make_act(root_client, deal, **fields):
    created = root_client.post(ACTS, json={"deal_id": deal["id"], **fields})
    assert created.status_code == 201, created.text
    return created.json()


def add_line(root_client, act, **fields):
    added = root_client.post(f"{ACTS}/{act['id']}/lines", json=fields)
    assert added.status_code == 201, added.text
    return added.json()


def act_with(root_client, deal, item, quantity="2", **fields):
    act = make_act(root_client, deal, **fields)
    add_line(root_client, act, product_id=item["id"], quantity=quantity)
    return act


def stock_of(root_client, product_id) -> int:
    return root_client.get(f"{STOCK}/products/{product_id}").json()["stock_milli"]


def stage_of(root_client, deal) -> str:
    return root_client.get(f"{DEALS}/{deal['id']}").json()["stage"]


def feed(root_client, deal, kind=None) -> list[dict]:
    query = f"?kind={kind}" if kind else ""
    return root_client.get(f"{DEALS}/{deal['id']}/feed{query}").json()["items"]


def carried_out(root_client, deal) -> list[dict]:
    """Строки ленты именно о ПРОВЕДЕНИИ акта.

    Отбираем по словам, а не берём все записи вида «бланк»: заведение акта тоже
    попадает в ленту — общим для всех бумаг подписчиком на выпуск. Это верно
    («по этой заявке завели акт») и работает одинаково у акта и у заказа, но к
    проведению отношения не имеет, и путать их в проверке нельзя.
    """
    return [row for row in feed(root_client, deal, kind="document") if "carried out" in row["body"]]


# --- то, ради чего акт заведён ------------------------------------------------


def test_odno_deystvie_delaet_vse_tri_veshchi(root_client, deal):
    """Одно нажатие: материалы списаны, работы зафиксированы, заявка переехала.

    До акта это были три независимых действия на трёх экранах, и связывала их
    только память мастера. Здесь один запрос, и после него обязаны сойтись все
    три ответа сразу.
    """
    item = product(root_client, stock="10")
    act = act_with(root_client, deal, item, quantity="3")
    add_line(root_client, act, name="Выезд мастера", quantity="1", price=100000)
    assert stage_of(root_client, deal) == "new"

    done = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert done.status_code == 200, done.text

    assert done.json()["status"] == "closed", "акт не зафиксировал работы"
    assert stock_of(root_client, item["id"]) == 7000, "материалы не списались"
    assert stage_of(root_client, deal) == "in_progress", "заявка осталась на месте"


def test_akt_pomnit_kuda_perevyol_zayavku(root_client, deal):
    """Записанное намерение — то единственное, чем расхождение обнаружимо.

    «Акт закрыт → заявка обязана стоять в его `next_stage`» проверяется
    запросом. Параметр, живший только в теле HTTP-запроса, не оставил бы следа
    вовсе, и сверять было бы не с чем.
    """
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="1", next_stage="ready")

    assert root_client.get(f"{ACTS}/{act['id']}").json()["next_stage"] == "ready"
    root_client.post(f"{ACTS}/{act['id']}/complete", json={})

    closed = root_client.get(f"{ACTS}/{act['id']}").json()
    assert closed["next_stage"] == "ready"
    assert stage_of(root_client, deal) == closed["next_stage"]


def test_sleduyushchiy_etap_beryotsya_sam(root_client, deal):
    """Проведение без выбора этапа обязано делать осмысленное.

    Иначе «одно действие» на деле требует двух: сначала выбрать, потом нажать.
    Берём соседа по воронке, а не «первый выигранный»: у ремонта после работ
    идёт «готово к выдаче», а вовсе не «выдано» — деньги ещё не получены.
    """
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="1")

    root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert stage_of(root_client, deal) == "in_progress"


def test_etap_v_zaprose_vazhnee_zapisannogo(root_client, deal):
    """Решение принимают в момент подписи — и оно же остаётся на акте."""
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="1", next_stage="in_progress")

    root_client.post(f"{ACTS}/{act['id']}/complete", json={"stage": "ready"})

    assert stage_of(root_client, deal) == "ready"
    assert root_client.get(f"{ACTS}/{act['id']}").json()["next_stage"] == "ready"


# --- либо всё, либо ничего ----------------------------------------------------


def test_ne_smenilsya_etap_znachit_i_ne_spisalos(root_client, deal, refuse_stage_change):
    """**Главная проверка задачи.** Отказ в конце цепочки отменяет её начало.

    Склад к этому моменту уже списан: подписчик склада стоит раньше подписчика
    воронки. Дальше воронка отказывает — и списания не должно остаться ни
    следа. Иначе на складе минус, работа числится незакрытой, и связать одно с
    другим можно только вручную по журналу.
    """
    item = product(root_client, stock="10")
    act = act_with(root_client, deal, item, quantity="4")
    refuse_stage_change()

    denied = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "pipeline_broke"

    assert stock_of(root_client, item["id"]) == 10000, "материалы списались вопреки отказу"
    assert stage_of(root_client, deal) == "new"
    assert root_client.get(f"{ACTS}/{act['id']}").json()["status"] == "issued", (
        "акт закрылся, хотя проведение не состоялось"
    )
    moves = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    assert [m for m in moves if m["document_id"] == act["id"]] == [], (
        "движения по откатанному акту остались в журнале склада"
    )
    assert not carried_out(root_client, deal), "лента запомнила отменённое проведение"


def test_ne_spisalos_znachit_i_etap_ne_smenilsya(root_client, deal):
    """Обратная половина: сломалось начало — не состоялся и конец.

    Нехватка на складе останавливает проведение. У ручного движения минус
    разрешён (деталь поставили сегодня, накладную занесут в пятницу), у акта
    этого мало: списывать нечего физически.
    """
    item = product(root_client, stock="1")
    act = act_with(root_client, deal, item, quantity="5")

    denied = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "not_enough_stock"
    # Позиции названы поимённо: отказ без списка отправляет человека сверять
    # бумагу со складом построчно руками.
    assert item["name"] in denied.json()["error"]["message"]

    assert stock_of(root_client, item["id"]) == 1000
    assert stage_of(root_client, deal) == "new", "заявка переехала при несостоявшемся списании"
    assert root_client.get(f"{ACTS}/{act['id']}").json()["status"] == "issued"


def test_nekhvatku_mozhno_podtverdit(root_client, deal):
    """Не отказ насовсем: подтвердили — провелось, и минус виден.

    Склад не сторож, а зеркало: он показывает минус, а не отказывается его
    показать. Но молчаливым такой минус быть не должен.
    """
    item = product(root_client, stock="1")
    act = act_with(root_client, deal, item, quantity="5")

    forced = root_client.post(f"{ACTS}/{act['id']}/complete", json={"confirm_negative": True})
    assert forced.status_code == 200, forced.text
    assert stock_of(root_client, item["id"]) == -4000
    assert stage_of(root_client, deal) == "in_progress"


def test_dvoynoe_provedenie_ne_spisyvaet_dvazhdy(root_client, deal):
    """Нажали дважды — списалось дважды.

    Защита стоит на условной смене статуса, а не на проверке «уже проведён»:
    проверка гоняется — оба прочитали `issued`, оба пошли списывать, — а
    условный UPDATE пропускает ровно одного.
    """
    item = product(root_client, stock="10")
    act = act_with(root_client, deal, item, quantity="3")

    assert root_client.post(f"{ACTS}/{act['id']}/complete", json={}).status_code == 200
    again = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert again.status_code == 422, again.text
    assert again.json()["error"]["code"] == "act_finished"

    assert stock_of(root_client, item["id"]) == 7000, "списалось дважды"
    history = root_client.get(f"{DEALS}/{deal['id']}").json()["stage_history"]
    assert len(history) == 2, "в журнал этапов попал переход, которого не было"


def test_provedennyy_akt_ne_pravitsya(root_client, deal):
    """Подписанную бумагу задним числом не дописывают: у клиента её половина."""
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="1")
    root_client.post(f"{ACTS}/{act['id']}/complete", json={})

    denied = root_client.post(f"{ACTS}/{act['id']}/lines", json={"name": "Задним числом", "quantity": "1"})
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "act_finished"


# --- что списывается, а что нет -----------------------------------------------


def test_raboty_sklada_ne_kasayutsya(root_client, deal):
    """Услуга и разовая строка — выполненная работа, а не материал.

    Остатка у них нет, и проверка нехватки объявила бы «консультации на складе
    ноль»: ни один акт с работами не провёлся бы вовсе.
    """
    service = product(root_client, service=True, stock=None)
    act = make_act(root_client, deal)
    add_line(root_client, act, product_id=service["id"], quantity="2")
    add_line(root_client, act, name="Диагностика", quantity="1", price=50000)

    done = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert done.status_code == 200, done.text
    assert root_client.get(f"{STOCK}/products/{service['id']}/moves").json()["total"] == 0
    assert stage_of(root_client, deal) == "in_progress"


def test_spisanie_nazyvaetsya_spisaniem(root_client, deal):
    """Деталь, поставленная в работу, со склада списана, а не «расходована».

    Журнал склада обязан читаться словами, а не только цифрами: `out` — это
    продажа и выдача клиенту, а по акту это `writeoff`.
    """
    item = product(root_client, stock="4")
    act = act_with(root_client, deal, item, quantity="1")
    root_client.post(f"{ACTS}/{act['id']}/complete", json={})

    moves = root_client.get(f"{STOCK}/products/{item['id']}/moves").json()["items"]
    mine = [m for m in moves if m["document_id"] == act["id"]]
    assert len(mine) == 1
    assert mine[0]["kind"] == "writeoff"
    assert act["number"] in mine[0]["comment"], "по комментарию не найти, откуда взялся минус"


# --- деньги -------------------------------------------------------------------


def test_sebestoimost_schitaetsya_a_ne_khranitsya(root_client, deal):
    """Себестоимость акта — производное, и она не хранится числом.

    До проведения её неоткуда взять: `null`, а не ноль. «Не знаем» и «работа не
    стоила нам ничего» на экране обязаны выглядеть по-разному.
    """
    item = product(root_client, stock="10", cost="10000")
    act = act_with(root_client, deal, item, quantity="3")

    assert root_client.get(f"{ACTS}/{act['id']}").json()["cost"] is None

    root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert root_client.get(f"{ACTS}/{act['id']}").json()["cost"] == 30000

    # Закупочная цена поменялась — подписанный акт не меняется: снимок брали в
    # момент проведения, как и у самого движения склада.
    root_client.patch(f"{STOCK}/products/{item['id']}", json={"cost": 99900})
    assert root_client.get(f"{ACTS}/{act['id']}").json()["cost"] == 30000


def test_summa_schitaetsya_odnim_deleniem(root_client, deal):
    """Делим на тысячу один раз в конце: иначе ошибка округления копится по
    числу позиций, и итог расходится с суммой строк."""
    item = product(root_client, stock="10", price=33333)
    act = make_act(root_client, deal)
    add_line(root_client, act, product_id=item["id"], quantity="0.333")
    add_line(root_client, act, name="Работа", quantity="1", price=10000)

    # 333 × 33333 / 1000 = 11 099.889 → 11 100, плюс 10 000.
    assert root_client.get(f"{ACTS}/{act['id']}").json()["total"] == 21100


def test_tsena_i_nazvanie_fiksiruyutsya_snimkom(root_client, deal):
    """Товар переименуют, прайс поменяют — в подписанном акте остаётся то, что
    клиенту назвали."""
    item = product(root_client, stock="10", price=50000)
    act = act_with(root_client, deal, item, quantity="2")

    root_client.patch(f"{STOCK}/products/{item['id']}", json={"name": "Другое", "price": 99900})

    line = root_client.get(f"{ACTS}/{act['id']}").json()["lines"][0]
    assert line["name"] == item["name"]
    assert line["price"] == 50000


# --- щель, которую акт продавил в стороже неизменяемости -----------------------
#
# Сторож «строки проведённой бумаги не правят» стоит на строках НАКЛАДНЫХ, а щель
# в нём открыта из-за акта: себестоимость ему пишет подписчик склада уже после
# захвата статуса, и переставлять там нечего. Разбор — docs/17-nakladnye.md §4.3.


def _stroka_provedennoy_nakladnoy(db) -> DocumentLine:
    """Строка накладной, вышедшей из черновика: сторож стоит только на такой.

    Собирается прямо в сессии, а не ручками накладных: проверяется сторож на
    событиях ORM, и путь бумаги вместе с блоком «Накладные» тут ни при чём.
    """
    bumaga = Document(
        number=f"ACT-SHCHEL-{uniq()}", kind=KIND_WAYBILL_OUT, status=STATUS_DRAFT
    )
    db.add(bumaga)
    db.flush()
    db.add(
        DocumentLine(
            document_id=bumaga.id,
            name_snapshot="Деталь",
            quantity_milli=1_000,
            cost_minor=None,
        )
    )
    db.flush()
    # Статус — условным UPDATE, как в бою: правка через объект уехала бы в
    # соседнего сторожа, «шапку проведённой не правят», и мы проверяли бы его.
    assert documents_repo.take_status(
        db, bumaga, expected=STATUS_DRAFT, status=STATUS_ISSUED
    )
    return db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == bumaga.id)
    ).one()


def test_akt_pishet_sebestoimost_v_pustuyu_stroku(root_client, deal, db):
    """Акт пишет себестоимость в ПУСТУЮ строку: `NULL → число`, и ничего сверх.

    Строки акта сторож не сторожит — он про накладные, — но щель прорублена
    ради этого вызова, и мерка у него та же. Начни акт снимать себестоимость
    раньше, при заведении строки например, и проведению понадобилось бы «число →
    другое число», то есть право переписывать выданную бумагу.
    """
    item = product(root_client, stock="10", cost="10000")
    act = act_with(root_client, deal, item, quantity="3")

    stroka = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == act["id"])
    ).one()
    assert stroka.cost_minor is None, "себестоимость снята раньше проведения"

    assert root_client.post(f"{ACTS}/{act['id']}/complete", json={}).status_code == 200
    # Снимок транзакции взят чтением выше и проведения не увидит: сессия проверки
    # живёт в REPEATABLE READ, как и всё в MySQL по умолчанию.
    db.rollback()

    stroka = db.scalars(
        select(DocumentLine).where(DocumentLine.document_id == act["id"])
    ).one()
    # Сверяем с карточкой товара, а не с числом в тексте: единицы поменяются —
    # записанная сюда «сотня» разойдётся с ней молча.
    assert stroka.cost_minor == item["cost"], "проведение не записало себестоимость"


def test_shchel_otkryta_na_pole_a_ne_na_pravku_s_nim(db):
    """Щель — это поле `cost_minor`, а не «любая правка, где оно есть».

    Проверяется случай, которого нет у соседей: законная запись себестоимости
    везёт с собой чужое поле. Пропусти сторож такую пару — и выданная бумага
    правится в одно движение, прикрывшись законным полем; а расширяется он до
    этого одной строкой, и ни одна другая проверка на ней не краснеет.

    Вторая половина — сама щель открыта: иначе проверка зеленела бы и на
    стороже, запретившем себестоимость вовсе, то есть на сломанном проведении.
    """
    stroka = _stroka_provedennoy_nakladnoy(db)
    stroka.cost_minor = 500
    stroka.name_snapshot = "переписано заодно"
    with pytest.raises(errors.ForbiddenError) as otkaz:
        db.flush()
    assert otkaz.value.code == "waybill_immutable"
    db.rollback()

    stroka = _stroka_provedennoy_nakladnoy(db)
    stroka.cost_minor = 500
    db.flush()
    assert stroka.cost_minor == 500


# --- блоки --------------------------------------------------------------------


def test_bez_sklada_akt_provoditsya(root_client, deal):
    """Склад выключен — подписчика не зовут, и это не половина операции.

    Пока блока нет, списания не существует как действия: мастерской без учёта
    деталей акт нужен ровно так же. Работы фиксируются, заявка переезжает.
    """
    item = product(root_client, stock="10")
    act = act_with(root_client, deal, item, quantity="2")

    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        done = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "closed"
        # Себестоимость неизвестна: снимать её было неоткуда.
        assert done.json()["cost"] is None
        assert stage_of(root_client, deal) == "in_progress"
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})

    assert stock_of(root_client, item["id"]) == 10000, "выключенный склад всё-таки тронули"


def test_vyklyuchennye_blanki_zakryvayut_i_akt(root_client, deal):
    """Акт — вид бланка, и выключается вместе с ними: своего блока у него нет."""
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="1")

    root_client.post(f"{API}/modules/documents", json={"enabled": False})
    try:
        assert root_client.get(f"{ACTS}/{act['id']}").status_code == 403
        assert root_client.post(f"{ACTS}/{act['id']}/complete", json={}).status_code == 403
    finally:
        root_client.post(f"{API}/modules/documents", json={"enabled": True})

    assert stock_of(root_client, item["id"]) == 5000


# --- лента и журнал -----------------------------------------------------------


def test_v_lente_odna_stroka_na_akt(root_client, deal):
    """Акт на три позиции — одна строка в ленте, а не три.

    Три движения склада это одно решение человека, и лента должна показывать
    его так же. Обещание записано в докстроке `STOCK_WRITTEN_OFF`.
    """
    act = make_act(root_client, deal)
    for _ in range(3):
        add_line(root_client, act, product_id=product(root_client, stock="10")["id"], quantity="1")

    root_client.post(f"{ACTS}/{act['id']}/complete", json={})

    about_act = carried_out(root_client, deal)
    assert len(about_act) == 1, "строк про одно проведение в ленте больше одной"
    assert act["number"] in about_act[0]["body"]
    assert "3 line(s)" in about_act[0]["body"], "в строке не видно, сколько всего сделано"
    assert feed(root_client, deal, kind="stock") == [], (
        "списание по акту рассыпалось в ленте по строке на деталь"
    )

    # Переезд заявки — своя строка, и в ней видно, ПОЧЕМУ она переехала.
    stages = feed(root_client, deal, kind="stage")
    assert any(act["number"] in entry["body"] for entry in stages), (
        "в ленте не видно, что этап сменил акт"
    )


def test_zhurnal_znaet_kto_zakryl_rabotu(root_client, deal):
    """«Кто закрыл эту работу» — вопрос к журналу, и ответ там один строкой.

    Соседние записи (движение склада, смена этапа) объясняют, из чего действие
    состояло, но сами на него не отвечают.
    """
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="1")
    root_client.post(f"{ACTS}/{act['id']}/complete", json={})

    entries = root_client.get(f"{API}/audit?action=act.completed").json()["items"]
    mine = [e for e in entries if e["entity_label"] == act["number"]]
    assert len(mine) == 1, "проведение акта не попало в журнал"
    assert mine[0]["value_before"] == "new"
    assert mine[0]["value_after"] == "in_progress"
    assert mine[0]["source_ref"] == act["number"]


# --- отказы при заведении -----------------------------------------------------


def test_akt_bez_zayavki_ne_zavoditsya(root_client):
    """Акт закрывает работу; без заявки закрывать нечего и двигать некуда.

    Отказ ясный и сразу, а не «этап не найден» при проведении: выяснять это
    тогда, когда работа уже сделана, поздно.
    """
    denied = root_client.post(ACTS, json={"deal_id": None})
    # Пустая заявка не проходит проверку схемы запроса: поле обязательное.
    assert denied.status_code == 422, denied.text


def test_pustoy_akt_ne_provoditsya(root_client, deal):
    """Провести пустой значит закрыть работу, не сказав, в чём она состояла."""
    act = make_act(root_client, deal)
    denied = root_client.post(f"{ACTS}/{act['id']}/complete", json={})
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "act_is_empty"
    assert stage_of(root_client, deal) == "new"


def test_nesushchestvuyushchiy_etap_otvergaetsya_pri_zavedenii(root_client, deal):
    """Опечатка в ключе этапа должна всплыть сейчас, а не в момент подписи."""
    denied = root_client.post(ACTS, json={"deal_id": deal["id"], "next_stage": "нетакого"})
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "unknown_stage"


def test_zakaz_po_adresu_akta_ne_provoditsya(root_client, deal):
    """Квитанция и заказ сюда не проходят: проводить их нечем.

    Номер в адресе — не пропуск к чужому виду бумаги: у заказа проведение своё,
    и молча выполнить его здесь значило бы двинуть склад мимо резерва.
    """
    intake = root_client.post(
        DOCS, json={"deal_id": deal["id"], "item": "Ноутбук", "client_id": deal["client_id"]}
    ).json()
    denied = root_client.get(f"{ACTS}/{intake['id']}")
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "not_an_act"


def test_otmena_neprovedennogo_akta(root_client, deal):
    """Передумали до проведения — склада и воронки это не касается."""
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, quantity="2")

    cancelled = root_client.post(f"{ACTS}/{act['id']}/cancel", json={"note": "передумали"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert stock_of(root_client, item["id"]) == 5000
    assert stage_of(root_client, deal) == "new"


# --- бумага -------------------------------------------------------------------


def test_pechatnaya_forma_daet_perechen_i_itog(root_client, deal):
    """Акт отвечает на вопрос «что сделано и принято» — значит перечень работ,
    итог и формулировка о приёмке над подписями."""
    item = product(root_client, stock="10", price=50000)
    act = act_with(root_client, deal, item, quantity="2", terms="Гарантия 6 месяцев")
    add_line(root_client, act, name="Выезд", quantity="1", price=30000)

    page = root_client.get(f"{ACTS}/{act['id']}/print")
    assert page.status_code == 200, page.text
    assert act["number"] in page.text
    assert item["name"] in page.text
    assert "Выезд" in page.text
    # 2 × 500 + 300 = 1300, посчитано сервером и напечатано один раз.
    assert "1300.00" in page.text
    assert "претензий" in page.text, "на бумаге нет того, что подписывают"
    assert "Гарантия 6 месяцев" in page.text
    # Номер уходит в штрихкод: акт находят сканером так же, как квитанцию.
    assert "<svg" in page.text


def test_sebestoimosti_na_bumage_net(root_client, deal):
    """Клиенту показывают, во сколько работа обошлась ЕМУ.

    Закупочная цена — наше внутреннее дело, и место ей на экране, под правом на
    раздел, а не в бумаге, которую отдают на руки.
    """
    item = product(root_client, stock="10", cost=12345, price=50000)
    act = act_with(root_client, deal, item, quantity="1")
    root_client.post(f"{ACTS}/{act['id']}/complete", json={})

    page = root_client.get(f"{ACTS}/{act['id']}/print")
    assert "123.45" not in page.text, "себестоимость уехала клиенту на бумаге"


def test_kolonka_summ_skladyvaetsya_v_itogo(root_client, deal):
    """Напечатанные суммы строк складываются в напечатанное «Итого». Всегда.

    Бумагу подписывают, и заказчик её складывает. До правки колонка «Сумма»
    считалась округлением НА КАЖДОЙ строке, а «Итого» — округлением один раз по
    всем: на двух строках с количеством 0,5 и ценой 12 345 выходило 61.73 +
    61.73 = 123.46 под надписью «Итого 123.45».

    Числа подобраны так, что остаток от деления равен ровно половине, — это и
    есть тот случай, где два способа округления расходятся. На круглых ценах,
    которыми пользуются соседние проверки печати, расхождения не бывает вовсе,
    поэтому они его и не ловили.
    """
    akt = root_client.post(
        ACTS, json={"deal_id": deal["id"], "title": "Акт с половинками"}
    ).json()
    for nomer in range(2):
        dobavlena = root_client.post(
            f"{ACTS}/{akt['id']}/lines",
            json={"name": f"Работа {nomer}", "quantity": "0.5", "price": 12345},
        )
        assert dobavlena.status_code == 201, dobavlena.text

    stranitsa = root_client.get(f"{ACTS}/{akt['id']}/print")
    assert stranitsa.status_code == 200, stranitsa.text

    # Ячейки колонок читаем целиком: в них не голое число, а сумма с валютой.
    # Первый разбор брал только цифры и точки и отбрасывал деньги вовсе, оставив
    # одни количества, — то есть проверка смотрела не на ту колонку.
    yacheyki = re.findall(r'<td class="num">(.*?)</td>', stranitsa.text, re.S)
    # Три ячейки на строку (количество, цена, сумма) плюс одна в подвале — само
    # «Итого». Подпись «Итого» словом сюда не попадает: у её ячейки есть
    # `colspan`, и образец её не берёт.
    assert len(yacheyki) == 3 * 2 + 1, f"на листе не та таблица: {yacheyki}"

    def kak_chislo(s: str) -> int | None:
        nayd = re.search(r"-?[\d\s ]+[.,]\d\d", s)
        if nayd is None:
            return None
        chistoe = nayd.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
        return round(float(chistoe) * 100)

    na_stroku = 3
    tel = yacheyki[: 2 * na_stroku]
    summy = [kak_chislo(tel[i]) for i in range(na_stroku - 1, len(tel), na_stroku)]
    assert all(s is not None for s in summy), f"суммы строк не разобрались: {tel}"

    itogo = kak_chislo(yacheyki[-1])
    assert itogo is not None, f"«Итого» не разобралось: {yacheyki[-1]!r}"
    assert sum(summy) == itogo, (
        f"колонка сумм даёт {sum(summy)}, а под ней напечатано «Итого» {itogo} — "
        "заказчик подписывает лист, который не сходится"
    )


def _zagolovok_lista(html: str) -> str:
    """Что напечатано в h1 — и только в нём.

    То же слово стоит в заголовке вкладки, поэтому проверка «есть где-нибудь на
    листе» зеленеет и на пустой шапке. Ровно так прошла печать накладной.
    """
    nayden = re.search(r"<h1>(.*?)</h1>", html, re.S)
    assert nayden is not None, "на листе нет заголовка"
    return " ".join(nayden.group(1).split())


def test_svoyo_nazvanie_akta_stoit_na_liste(root_client, deal):
    """Вписали «Наряд-заказ» — оно и напечатано.

    Заголовок брался из словаря по языку бумаги, и вписанное руками не попадало
    на лист НИКОГДА: список CRM показывал одно название, а клиент подписывал
    бумагу с другим.
    """
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, title="Наряд-заказ")
    assert act["payload"]["fields"]["item"] == "Наряд-заказ"

    page = root_client.get(f"{ACTS}/{act['id']}/print")
    assert page.status_code == 200, page.text
    zagolovok = _zagolovok_lista(page.text)
    assert zagolovok.startswith("Наряд-заказ"), (
        f"в шапке листа «{zagolovok}», а акт назван «Наряд-заказ»"
    )
    assert ACT_PRINT_STRINGS["ru"]["title"] not in zagolovok


def test_bez_svoego_nazvaniya_spisok_i_list_govoryat_odno(root_client, deal):
    """Названия не вписали — на листе стоит перевод ровно того, что в списке.

    Умолчание лежит в снимке ПО-АНГЛИЙСКИ (`act_service.DEFAULT_TITLE`), а лист
    печатается по языку бумаги. Напечатай снимок как есть — повторная печать
    выданного акта дала бы лист, не совпадающий с тем, что у клиента на руках.
    """
    item = product(root_client, stock="5")
    act = act_with(root_client, deal, item, locale="ru")

    # То же поле, что показывают список бланков и карточка акта.
    v_spiske = act["payload"]["fields"]["item"]
    assert v_spiske == ACT_PRINT_STRINGS["en"]["title"], (
        f"в записи «{v_spiske}», а словарь печати называет тот же акт "
        f"«{ACT_PRINT_STRINGS['en']['title']}» — список и бумага разошлись"
    )

    page = root_client.get(f"{ACTS}/{act['id']}/print")
    assert page.status_code == 200, page.text
    zagolovok = _zagolovok_lista(page.text)
    assert zagolovok.startswith(ACT_PRINT_STRINGS["ru"]["title"]), (
        f"в шапке русского листа «{zagolovok}» — не то, что показывает список"
    )
