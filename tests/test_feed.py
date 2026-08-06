"""Единая лента: письма, звонки, встречи и заметки одним потоком.

Решение принимается ДО почты и телефонии. Если каждый канал заведёт свой
журнал, свести их потом придётся вручную: разные поля, разное время, разные
привязки. Поэтому проверяем именно то, ради чего лента и делается — что записи
разных видов лежат вместе и фильтруются, а не живут по отдельным спискам.
"""

import pytest

from core.services import modules_service
from core.services.document_service import DOCUMENT_ISSUED
from core.services.warehouse_service import STOCK_WRITTEN_OFF
from tests.conftest import API, make_manager
from tests.test_deals import DEALS, make_client

# Фикстура подписки живёт вместе с механизмом; здесь она нужна, чтобы проверить
# обещание «упавшая строка в ленте ничего не отменяет» на настоящих операциях.
from tests.test_events import subscribe  # noqa: F401

FEED_KINDS = ("note", "call", "meeting", "email")

DOCUMENTS = f"{API}/documents"
WAREHOUSE = f"{API}/warehouse"
MODULES = f"{API}/modules"


def make_deal(manager_client, client_id):
    return manager_client.post(DEALS, json={"title": "Заказ", "client_id": client_id}).json()


def test_all_kinds_land_in_one_stream(manager_client):
    client = make_client(manager_client, "Клиент ленты")
    for kind in FEED_KINDS:
        added = manager_client.post(
            f"{API}/clients/{client['id']}/notes", json={"kind": kind, "body": f"Событие {kind}"}
        )
        assert added.status_code == 201, added.text

    feed = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    assert {n["kind"] for n in feed} == set(FEED_KINDS), "виды разъехались по разным спискам"


def test_feed_filters_by_kind(manager_client):
    client = make_client(manager_client, "Клиент фильтра")
    manager_client.post(f"{API}/clients/{client['id']}/notes", json={"kind": "call", "body": "Звонил"})
    manager_client.post(f"{API}/clients/{client['id']}/notes", json={"kind": "note", "body": "Заметка"})

    calls = manager_client.get(f"{API}/clients/{client['id']}/notes?kind=call").json()["items"]
    assert [n["body"] for n in calls] == ["Звонил"]


def test_direction_belongs_to_calls_and_letters_only(manager_client):
    """У заметки направления нет, и «нет направления» — не то же самое, что
    «входящее»: иначе в отчёте все заметки станут входящими звонками."""
    client = make_client(manager_client, "Клиент направления")
    call = manager_client.post(
        f"{API}/clients/{client['id']}/notes",
        json={"kind": "call", "body": "Клиент позвонил", "direction": "in"},
    ).json()
    note = manager_client.post(
        f"{API}/clients/{client['id']}/notes", json={"kind": "note", "body": "Просто мысль"}
    ).json()

    assert call["direction"] == "in"
    assert note["direction"] is None

    bad = manager_client.post(
        f"{API}/clients/{client['id']}/notes",
        json={"kind": "call", "body": "Кривое", "direction": "вбок"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "bad_direction"


def test_deal_feed_shows_only_its_own_events(manager_client):
    """У клиента событий больше, чем у одной заявки: лента заявки обязана
    показывать её собственные, иначе смысл привязки теряется."""
    client = make_client(manager_client, "Клиент двух заказов")
    first = make_deal(manager_client, client["id"])
    second = make_deal(manager_client, client["id"])

    manager_client.post(
        f"{DEALS}/{first['id']}/feed", json={"kind": "call", "body": "По первому заказу", "direction": "out"}
    )
    manager_client.post(
        f"{DEALS}/{second['id']}/feed", json={"kind": "note", "body": "По второму заказу"}
    )
    # событие про клиента вообще, без заявки
    manager_client.post(
        f"{API}/clients/{client['id']}/notes", json={"kind": "note", "body": "Просто про клиента"}
    )

    mine = manager_client.get(f"{DEALS}/{first['id']}/feed").json()["items"]
    assert [n["body"] for n in mine] == ["По первому заказу"]

    # а в ленте клиента видно всё
    everything = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    assert len(everything) == 3


def test_event_written_into_a_deal_keeps_the_client(manager_client):
    """Запись в ленте всегда о ком-то: клиента берём из заявки, а не просим
    указывать второй раз."""
    client = make_client(manager_client, "Клиент связи")
    deal = make_deal(manager_client, client["id"])
    note = manager_client.post(
        f"{DEALS}/{deal['id']}/feed", json={"kind": "email", "body": "Отправил счёт", "direction": "out"}
    ).json()

    assert note["client_id"] == client["id"]
    assert note["deal_id"] == deal["id"]


def test_feed_is_ordered_by_when_it_happened(manager_client):
    """Звонок вчерашний, а занесли его сегодня — в ленте он стоит вчерашним."""
    client = make_client(manager_client, "Клиент порядка")
    manager_client.post(
        f"{API}/clients/{client['id']}/notes",
        json={"kind": "call", "body": "Позавчерашний", "happened_at": "2026-08-01T10:00:00"},
    )
    manager_client.post(
        f"{API}/clients/{client['id']}/notes",
        json={"kind": "note", "body": "Вчерашний", "happened_at": "2026-08-04T10:00:00"},
    )

    feed = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    assert [n["body"] for n in feed] == ["Вчерашний", "Позавчерашний"]


def test_existing_notes_survive_the_generalisation(manager_client):
    """Заметки существовали до ленты: у них нет ни направления, ни заявки, и
    переписывать их задним числом нельзя."""
    client = make_client(manager_client, "Старый клиент")
    note = manager_client.post(
        f"{API}/clients/{client['id']}/notes", json={"body": "Как раньше"}
    ).json()

    assert note["kind"] == "note"
    assert note["direction"] is None
    assert note["deal_id"] is None


# --- события по заявке: бланки и склад ---
#
# Лента отвечала на вопрос «что происходило с заявкой» наполовину: звонки и
# письма в ней были, а выданная квитанция лежала в `document_events`, списание —
# в `stock_moves`. Здесь проверяется не «обработчик вызвался», а то, ради чего
# всё делалось: человек открывает ленту и видит там оба события.


@pytest.fixture(scope="module")
def warehouse_on(root_client):
    """Склад по умолчанию выключен, а списывать под заявку надо.

    Возвращаем состояние в конце: реестр блоков глобальный, и оставленный
    включённым склад посыпал бы совершенно посторонние файлы.
    """
    assert root_client.post(f"{MODULES}/warehouse", json={"enabled": True}).status_code == 200
    yield
    root_client.post(f"{MODULES}/warehouse", json={"enabled": False})
    modules_service.invalidate()


@pytest.fixture
def deal(manager_client):
    client = make_client(manager_client, "Клиент событий заявки")
    return make_deal(manager_client, client["id"])


def deal_feed(client, deal_id, kind=None):
    query = f"?kind={kind}" if kind else ""
    return client.get(f"{DEALS}/{deal_id}/feed{query}").json()["items"]


def issue_form(client, deal_id, item="Ноутбук Asus X515"):
    response = client.post(DOCUMENTS, json={"deal_id": deal_id, "item": item})
    assert response.status_code == 201, response.text
    return response.json()


def set_form_status(client, form_id, status, note=""):
    return client.post(f"{DOCUMENTS}/{form_id}/status", json={"status": status, "note": note})


def new_product(client, **fields):
    payload = {"name": "Матрица 15.6", "unit": "pcs", "cost": 120000}
    payload.update(fields)
    response = client.post(f"{WAREHOUSE}/products", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def move(client, product_id, quantity, kind="out", deal_id=None, **fields):
    payload = {"product_id": product_id, "kind": kind, "quantity": quantity, "deal_id": deal_id}
    payload.update(fields)
    return client.post(f"{WAREHOUSE}/moves", json=payload)


def test_issued_form_lands_in_the_deal_feed(manager_client, deal):
    """Выдали квитанцию — это видно в ленте, а не только в списке бланков.

    Одной записью: у бланка есть номер и предмет, и обе вещи человек называет
    вслух («где там ноутбук по двести двадцать третьему»).
    """
    form = issue_form(manager_client, deal["id"])

    entries = deal_feed(manager_client, deal["id"], kind="document")
    assert len(entries) == 1, "выпуск бланка не дошёл до ленты"
    body = entries[0]["body"]
    assert form["number"] in body, body
    assert "issued" in body, body
    assert "Ноутбук Asus X515" in body, body


def test_closing_a_form_lands_and_repeating_it_adds_nothing(manager_client, deal):
    """Закрытие — пара к выпуску, иначе лента остаётся с открытым концом.

    Повтор того же состояния записи не двоит: `set_status` на неизменившемся
    состоянии не событие, а холостой запрос от дважды нажатой кнопки.
    """
    form = issue_form(manager_client, deal["id"])
    assert set_form_status(manager_client, form["id"], "closed", "клиент забрал 12.08").status_code == 200
    assert set_form_status(manager_client, form["id"], "closed").status_code == 200

    entries = deal_feed(manager_client, deal["id"], kind="document")
    assert len(entries) == 2, "закрытие бланка не дошло до ленты или задвоилось"
    newest = entries[0]["body"]
    assert "closed" in newest, newest
    # Приписка оператора и есть причина закрытия — она информативнее любой
    # формулировки от кода.
    assert "клиент забрал 12.08" in newest, newest


def test_intermediate_form_statuses_stay_out_of_the_feed(manager_client, deal):
    """«В работе» и «готово» — путь бумаги, а не событие по заявке.

    Они лежат в `document_events` и рисуются на экране бланка. Пусти их в ленту —
    и одна квитанция даст четыре строки вместо двух, а лента заводилась ради
    того, чтобы её читали целиком.
    """
    form = issue_form(manager_client, deal["id"])
    for status in ("in_progress", "ready"):
        assert set_form_status(manager_client, form["id"], status).status_code == 200

    assert len(deal_feed(manager_client, deal["id"], kind="document")) == 1


def test_write_off_lands_in_the_deal_feed_as_one_entry(manager_client, deal, warehouse_on):
    """Списали деталей — это видно в ленте: что, сколько и почему.

    Именно ради этой строки лента и открывается: «вчера выдали квитанцию,
    сегодня списали две матрицы» — один поток, а не три журнала.

    Себестоимости в строке нет, и это не упущение — см. соседний тест: тело
    записи ленты вычёркиванию не поддаётся, и деньги в нём проносились мимо
    права `warehouse.view_amounts`.
    """
    product = new_product(manager_client, name="Матрица 15.6", cost=120000)
    assert move(manager_client, product["id"], "2", deal_id=deal["id"]).status_code == 201

    entries = deal_feed(manager_client, deal["id"], kind="stock")
    assert len(entries) == 1, "списание не дошло до ленты или разложилось по строкам"
    body = entries[0]["body"]
    assert "Матрица 15.6" in body, body
    assert "2 pcs" in body, body


def test_the_feed_does_not_carry_the_write_off_cost_past_the_money_right(
    root_client, manager_client, deal, warehouse_on
):
    """Автоматика не проносит деньги мимо права, которым они закрыты.

    Подписчик вписывал себестоимость списания прямо в тело записи ленты
    («Stock: Матрица — 2 pcs, 2400.00 USD»). Тело — обычная строка, и ни
    `GET /deals/{id}/feed`, ни `GET /clients/{id}/notes` не умеют вычёркивать
    из неё числа. Получалось, что `GET /warehouse/moves` честно отдавал
    `cost: null` тому, кому суммы не положены, а лента показывала ту же сумму
    словами: право обходилось соседним экраном.

    Проверяется на живом сотруднике, а не чтением строки: важно не то, как
    подписчик её собрал, а то, что видно на другом конце.
    """
    product = new_product(manager_client, name="Матрица для проверки прав", cost=120000)
    assert move(manager_client, product["id"], "2", deal_id=deal["id"]).status_code == 201

    role = root_client.post(
        f"{API}/roles",
        json={
            "name": "Лента без сумм",
            "permissions": ["clients.view", "deals.view", "deals.view_others", "warehouse.view"],
        },
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]
    poor = make_manager(root_client, "feedmoney@test.local")
    user_id = next(
        u["id"] for u in root_client.get(f"{API}/staff").json()["items"]
        if u["email"] == "feedmoney@test.local"
    )
    assert root_client.post(
        f"{API}/roles/assign/{user_id}", json={"role_id": role_id}
    ).status_code == 200
    try:
        entries = deal_feed(poor, deal["id"], kind="stock")
        assert entries, "лента пуста — проверять нечего"
        body = entries[0]["body"]
        assert "2400.00" not in body, body
        assert "1200.00" not in body, body
        # Соседний экран, где право проверяется, ведёт себя как раньше.
        moves = poor.get(f"{WAREHOUSE}/moves", params={"deal_id": deal["id"]}).json()
        assert all(row["cost"] is None for row in moves["items"]), moves
        assert moves["cost"] is None
        # А тому, кому суммы положены, себестоимость по-прежнему видна — иначе
        # проверка проходила бы и на складе, разучившемся считать деньги.
        rich = manager_client.get(f"{WAREHOUSE}/moves", params={"deal_id": deal["id"]}).json()
        assert rich["cost"] == 240000, rich
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{API}/roles/{role_id}")


def test_a_second_write_off_is_a_second_event_not_a_doubled_one(
    manager_client, deal, warehouse_on
):
    """Одно движение — одна строка. Два движения — две, и это не задвоение.

    Партии в этом складе нет: движение заводится по одному, и одно движение —
    ровно одно действие кладовщика. Склеивать соседние значило бы решать за него,
    что он имел в виду.
    """
    product = new_product(manager_client, name="Шлейф матрицы", cost=30000)
    assert move(manager_client, product["id"], "1", deal_id=deal["id"]).status_code == 201
    assert move(manager_client, product["id"], "1", deal_id=deal["id"]).status_code == 201

    assert len(deal_feed(manager_client, deal["id"], kind="stock")) == 2


def test_return_to_stock_is_not_a_deal_event(manager_client, deal, warehouse_on):
    """Положили обратно — это поправка учёта, а не событие по заявке.

    Итог виден во врезке себестоимости прямо под лентой. Пускать в ленту оба
    направления значит превратить её в журнал склада — ровно то, из-за чего
    ленту перестают читать целиком.
    """
    product = new_product(manager_client, name="Клавиатура", cost=45000)
    assert move(manager_client, product["id"], "5", kind="in", deal_id=deal["id"]).status_code == 201
    assert move(manager_client, product["id"], "1", kind="return", deal_id=deal["id"]).status_code == 201

    assert deal_feed(manager_client, deal["id"], kind="stock") == []


def test_write_off_without_a_deal_reaches_no_feed(manager_client, deal, warehouse_on):
    """Списание не под заявку в ленту не идёт: её попросту нет.

    Инвентаризация и брак на складе — дело склада; заявка тут ни при чём.
    """
    product = new_product(manager_client, name="Термопаста", cost=5000)
    assert move(manager_client, product["id"], "1", kind="writeoff").status_code == 201

    assert deal_feed(manager_client, deal["id"], kind="stock") == []


def test_field_edits_never_reach_the_feed(manager_client, deal, warehouse_on):
    """Правка полей заявки — не событие по заявке.

    Лента, ставшая журналом изменений, перестаёт читаться целиком, и в ней тонут
    звонки с письмами, ради которых она делалась. Полный аудит — отдельный экран.
    """
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"amount": 500000})
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"description": "Заменить матрицу"})
    manager_client.get(f"{DEALS}/{deal['id']}")

    assert deal_feed(manager_client, deal["id"]) == []


def test_entries_survive_switching_off_the_module_that_made_them(
    root_client, manager_client, deal, warehouse_on
):
    """Запись о списании принадлежит ленте, а не складу — как письмо и звонок.

    Выключенный склад перестаёт порождать события; уже написанные строки от
    этого никуда не деваются и продолжают читаться.
    """
    product = new_product(manager_client, name="Аккумулятор", cost=80000)
    assert move(manager_client, product["id"], "1", deal_id=deal["id"]).status_code == 201
    issue_form(manager_client, deal["id"], item="Ноутбук на аккумуляторе")

    assert root_client.post(f"{MODULES}/warehouse", json={"enabled": False}).status_code == 200
    assert root_client.post(f"{MODULES}/documents", json={"enabled": False}).status_code == 200
    try:
        kinds = [entry["kind"] for entry in deal_feed(manager_client, deal["id"])]
        assert "stock" in kinds, "запись о списании исчезла вместе с выключенным складом"
        assert "document" in kinds, "запись о бланке исчезла вместе с выключенными бланками"
    finally:
        root_client.post(f"{MODULES}/warehouse", json={"enabled": True})
        root_client.post(f"{MODULES}/documents", json={"enabled": True})


def test_new_system_entries_cannot_be_deleted_by_anyone(
    root_client, manager_client, deal, warehouse_on
):
    """След выданной бумаги и ушедшего со склада товара не стирает никто.

    Автором записи стоит тот, кто выдал бланк и списал деталь, — правило «автор
    может удалить своё» отдало бы ему право убрать отметку о собственном
    действии.
    """
    product = new_product(manager_client, name="Корпус", cost=60000)
    assert move(manager_client, product["id"], "1", deal_id=deal["id"]).status_code == 201
    issue_form(manager_client, deal["id"], item="Корпус в сборе")

    client_id = deal["client_id"]
    for kind in ("stock", "document"):
        entries = deal_feed(manager_client, deal["id"], kind=kind)
        assert entries, f"нечего проверять: записи вида {kind} нет"
        note_id = entries[0]["id"]

        denied = manager_client.delete(f"{API}/clients/{client_id}/notes/{note_id}")
        assert denied.status_code == 403, kind
        assert denied.json()["error"]["code"] == "system_note_immutable"
        # и root тоже: журнал, который можно поправить, ничего не доказывает
        assert root_client.delete(f"{API}/clients/{client_id}/notes/{note_id}").status_code == 403


def test_a_broken_feed_entry_undoes_neither_the_form_nor_the_write_off(
    manager_client, deal, warehouse_on, subscribe, caplog
):
    """Наблюдатель упал — операция всё равно состоялась.

    Бумага уже напечатана и отдана, товар уже физически ушёл с полки. Отменять
    их из-за не записавшейся строки в ленте значит спорить с тем, что человек
    только что сделал руками, и вернуть на остаток то, чего на складе нет.
    """
    def breaks(event):
        raise RuntimeError("наблюдателю поплохело")

    subscribe(DOCUMENT_ISSUED, breaks)
    subscribe(STOCK_WRITTEN_OFF, breaks)

    product = new_product(manager_client, name="Вентилятор", cost=25000)
    with caplog.at_level("ERROR", logger="opencrm.events"):
        form = manager_client.post(DOCUMENTS, json={"deal_id": deal["id"], "item": "Ноутбук"})
        written = move(manager_client, product["id"], "1", deal_id=deal["id"])

    assert form.status_code == 201, "мелочь в ленте отменила выпуск бланка"
    assert written.status_code == 201, "мелочь в ленте отменила списание"
    assert "breaks" in caplog.text, "падение осталось без следа в журнале"

    # соседний подписчик отработал: своя строка на месте у обоих
    assert len(deal_feed(manager_client, deal["id"], kind="document")) == 1
    assert len(deal_feed(manager_client, deal["id"], kind="stock")) == 1
    # и склад остался с записанным движением, а не с откатом
    moves = manager_client.get(f"{WAREHOUSE}/moves?deal_id={deal['id']}").json()
    assert moves["total"] == 1


def test_the_actor_travels_into_both_new_entries(manager_client, deal, warehouse_on):
    """Записи принадлежат живому человеку, а не «системе».

    Иначе половина истории заявки оказывается ничьей, и на вопрос «кто это
    списал» отвечать нечем.
    """
    me = manager_client.get(f"{API}/auth/me").json()
    product = new_product(manager_client, name="Блок питания", cost=40000)
    assert move(manager_client, product["id"], "1", deal_id=deal["id"]).status_code == 201
    issue_form(manager_client, deal["id"], item="Блок питания")

    for kind in ("stock", "document"):
        entry = deal_feed(manager_client, deal["id"], kind=kind)[0]
        assert entry["author_id"] == me["id"], f"запись вида {kind} осталась ничьей"


def test_the_feed_says_who(manager_client):
    """Лента отвечает на «кто», а не только на «что» и «когда».

    `author_id` писался верно с самого начала, но до экрана не доезжал: в
    ответе было число, а имени не было ни в API, ни в разметке. Половина
    смысла ленты при этом пропадала — «позвонили и договорились» без имени не
    отвечает на вопрос, ради которого в ленту и заходят.
    """
    client = make_client(manager_client, "Клиент с автором")
    deal = make_deal(manager_client, client["id"])
    manager_client.post(f"{DEALS}/{deal['id']}/feed", json={"kind": "note", "body": "Позвонил"})

    entries = manager_client.get(f"{DEALS}/{deal['id']}/feed").json()["items"]
    assert entries, "лента пуста — проверять нечего"
    assert all("author_name" in e for e in entries), "поля автора нет в ответе"
    assert all(e["author_name"] for e in entries), "автор не подставился"

    # то же в ленте клиента: одна запись не может называться по-разному на двух экранах
    notes = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    assert all(n["author_name"] for n in notes if n["author_id"])


def test_an_entry_without_a_person_has_no_author(manager_client, root_client):
    """Пусто — законное состояние, а не недосмотр.

    У звонка, пришедшего от станции, живого автора нет. Подписать его
    «системой» значило бы соврать: человека там действительно не было, и
    именно это отличает такую запись от действия сотрудника.

    Идём настоящим путём — подписанным вебхуком, а не сборкой записи руками:
    проверять надо то, что происходит в бою.
    """
    import hmac, hashlib, json, time

    root_client.post(f"{API}/modules/telephony", json={"enabled": True})
    secret = root_client.post(f"{API}/telephony/settings/secret", json={}).json()["secret"]
    client = make_client(manager_client, "Клиент со звонком")
    root_client.patch(f"{API}/clients/{client['id']}", json={"phone": "+380671112233"})

    body = json.dumps({
        "call_id": "feed-author-probe", "direction": "in",
        "from": "+380671112233", "to": "0442000000",
        "started_at": "2026-08-06T10:00:00+00:00", "status": "answered", "duration": 42,
    }).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()

    sent = root_client.post(
        f"{API}/telephony/webhook", content=body,
        headers={"Content-Type": "application/json",
                 "X-OpenCRM-Timestamp": stamp, "X-OpenCRM-Signature": signature},
    )
    assert sent.status_code == 200, sent.text

    notes = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    call = next((n for n in notes if n["kind"] == "call"), None)
    assert call is not None, "звонок не попал в ленту — проверять нечего"
    assert call["author_id"] is None
    assert call["author_name"] is None, "звонку от станции приписали автора"
