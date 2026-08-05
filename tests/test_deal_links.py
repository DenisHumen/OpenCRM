"""Связь заявки с досками и клиентом.

Раньше доска знала только клиента, а у клиента за год бывает пять заказов —
все его доски лежали одной кучей. Проверяем не «сохраняется ли поле», а то, что
эта связь не сломала существующее: доски делались до появления заявок, и без
заявки обязаны работать по-прежнему.
"""

from tests.conftest import API
from tests.test_deals import DEALS, make_client

BOARDS = f"{API}/boards"


def make_deal(manager_client, client_id, title="Заказ клиента"):
    return manager_client.post(DEALS, json={"title": title, "client_id": client_id}).json()


def test_board_works_without_a_deal(manager_client):
    """Главное требование миграции: у всех имеющихся досок заявки нет.

    Проставлять им что-нибудь задним числом нельзя — это засорило бы воронку
    записями, которых в жизни не было.
    """
    board = manager_client.post(BOARDS, json={"title": "Доска без заявки"})
    assert board.status_code == 201, board.text
    assert board.json()["deal_id"] is None

    opened = manager_client.get(f"{BOARDS}/{board.json()['id']}")
    assert opened.status_code == 200
    assert opened.json()["deal_id"] is None


def test_board_can_be_attached_to_a_deal_and_detached(manager_client):
    client = make_client(manager_client, "Клиент с досками")
    deal = make_deal(manager_client, client["id"])

    board = manager_client.post(
        BOARDS, json={"title": "Доска заказа", "client_id": client["id"], "deal_id": deal["id"]}
    ).json()
    assert board["deal_id"] == deal["id"]

    # и отвязать: доска переехала или создавалась не под эту заявку
    detached = manager_client.patch(f"{BOARDS}/{board['id']}", json={"deal_id": None}).json()
    assert detached["deal_id"] is None


def test_deal_card_lists_its_boards(manager_client):
    """«Что мы для него сделали» — вопрос к системе, а не к памяти."""
    client = make_client(manager_client, "Клиент подборки")
    deal = make_deal(manager_client, client["id"])
    mine = manager_client.post(
        BOARDS, json={"title": "Подборка по заказу", "deal_id": deal["id"]}
    ).json()
    manager_client.post(BOARDS, json={"title": "Чужая доска"})

    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert [b["id"] for b in card["boards"]] == [mine["id"]]
    assert card["boards"][0]["title"] == "Подборка по заказу"


def test_client_card_lists_their_deals(manager_client):
    """У клиента за год пять заказов — в карточке должны быть видны все."""
    client = make_client(manager_client, "Постоянный клиент")
    first = make_deal(manager_client, client["id"], "Первый заказ")
    second = make_deal(manager_client, client["id"], "Второй заказ")

    card = manager_client.get(f"{API}/clients/{client['id']}").json()
    ids = [d["id"] for d in card["deals"]]
    assert first["id"] in ids and second["id"] in ids


def test_deleting_a_deal_does_not_delete_its_boards(manager_client):
    """Заявку закрыли и убрали, а работы остаются: их показывают в портфолио,
    и терять их вместе с записью в воронке нельзя."""
    client = make_client(manager_client, "Клиент удаления")
    deal = make_deal(manager_client, client["id"])
    board = manager_client.post(
        BOARDS, json={"title": "Работы остаются", "deal_id": deal["id"]}
    ).json()

    assert manager_client.delete(f"{DEALS}/{deal['id']}").status_code == 200

    survived = manager_client.get(f"{BOARDS}/{board['id']}")
    assert survived.status_code == 200, "доска исчезла вместе с заявкой"
    assert survived.json()["title"] == "Работы остаются"


def test_deal_card_hides_boards_when_the_module_is_off(root_client, manager_client):
    """Выключенный блок досок не должен оставлять в карточке пустой раздел."""
    client = make_client(manager_client, "Клиент без досок")
    deal = make_deal(manager_client, client["id"])
    manager_client.post(BOARDS, json={"title": "Скроется", "deal_id": deal["id"]})

    root_client.post(f"{API}/modules/boards", json={"enabled": False})
    try:
        card = manager_client.get(f"{DEALS}/{deal['id']}").json()
        assert card["boards"] == []
    finally:
        root_client.post(f"{API}/modules/boards", json={"enabled": True})
