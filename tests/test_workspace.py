"""Универсальность: слова в интерфейсе принадлежат бизнесу, а не разработчику.

Проверочный вопрос из задачи: «как это назовёт мастер по ремонту ноутбуков и
как — администратор салона?» Первый скажет «заказ», второй «запись». Ответы
разные — значит название обязано быть настройкой.

Здесь же живут валюта и название дела: полные настройки читает только root, а
эти две вещи нужны каждому сотруднику на каждом экране.
"""

from tests.conftest import API

WORKSPACE = f"{API}/workspace"
SETTINGS = f"{API}/settings"


def test_every_employee_can_read_the_workspace(manager_client):
    """Менеджеру полные настройки закрыты — там режим обслуживания и контакты.

    Но без валюты суммы показываются голым числом, а без названия разделы
    называются чужими словами. Поэтому эти поля отдельной точкой.
    """
    assert manager_client.get(SETTINGS).status_code == 403

    workspace = manager_client.get(WORKSPACE)
    assert workspace.status_code == 200
    body = workspace.json()
    assert set(body) == {"brand_name", "currency", "deal_term"}


def test_defaults_are_sane_before_anyone_configures_anything(manager_client):
    body = manager_client.get(WORKSPACE).json()
    assert body["currency"] == "USD"
    assert body["deal_term"] == "deal"


def test_the_term_follows_the_setting(root_client, manager_client):
    """Мастерская называет это заказом, салон — записью."""
    try:
        root_client.patch(SETTINGS, json={"values": {"deal_term": "booking"}})
        assert manager_client.get(WORKSPACE).json()["deal_term"] == "booking"
    finally:
        root_client.patch(SETTINGS, json={"values": {"deal_term": "deal"}})


def test_currency_follows_the_setting(root_client, manager_client):
    try:
        root_client.patch(SETTINGS, json={"values": {"currency": "EUR"}})
        assert manager_client.get(WORKSPACE).json()["currency"] == "EUR"
    finally:
        root_client.patch(SETTINGS, json={"values": {"currency": "USD"}})


def test_workspace_needs_a_login(base_client):
    """Название дела и валюта — не секрет, но и не для посторонних."""
    assert base_client.get(WORKSPACE).status_code in (401, 403)
