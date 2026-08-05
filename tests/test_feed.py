"""Единая лента: письма, звонки, встречи и заметки одним потоком.

Решение принимается ДО почты и телефонии. Если каждый канал заведёт свой
журнал, свести их потом придётся вручную: разные поля, разное время, разные
привязки. Поэтому проверяем именно то, ради чего лента и делается — что записи
разных видов лежат вместе и фильтруются, а не живут по отдельным спискам.
"""

from tests.conftest import API
from tests.test_deals import DEALS, make_client

FEED_KINDS = ("note", "call", "meeting", "email")


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
