"""Ссылка в никуда — отказ с объяснением, а не ошибка сервера.

Клиент, заявка, ответственный, фирма приходят числом в теле запроса, и число это
выбирает не сервер. Непроверенное доезжает до вставки и падает нарушением
внешнего ключа — пятисоткой на обычную опечатку.

Проверка проходит по ВСЕМ полям такого рода разом: новый блок с новой ссылкой
попадёт под неё сам, если добавить его в список ниже, а забытая проверка видна
здесь, а не в журнале сервера через неделю.
"""

import pytest

from tests.conftest import API

GHOST = 987_654


@pytest.fixture(scope="module")
def stage(root_client, manager_client):
    """Живые записи, к которым можно привязываться по-настоящему."""
    for block in ("warehouse", "documents", "tasks", "boards", "companies"):
        root_client.post(f"{API}/modules/{block}", json={"enabled": True})
    client = manager_client.post(f"{API}/clients", json={"name": "Живой для ссылок"}).json()
    deal = manager_client.post(
        f"{API}/deals", json={"title": "Живая для ссылок", "client_id": client["id"]}
    ).json()
    product = root_client.post(f"{API}/warehouse/products", json={"name": "Живой товар"}).json()
    return {"client": client, "deal": deal, "product": product}


@pytest.mark.parametrize(
    "method, path, body, expected",
    [
        ("post", "/deals", {"title": "x", "client_id": GHOST}, "client_not_found"),
        ("post", "/tasks", {"title": "x", "client_id": GHOST}, "client_not_found"),
        ("post", "/tasks", {"title": "x", "deal_id": GHOST}, "deal_not_found"),
        ("post", "/tasks", {"title": "x", "assignee_id": GHOST}, "assignee_not_found"),
        ("post", "/boards", {"title": "x", "client_id": GHOST}, "client_not_found"),
        ("post", "/documents", {"item": "x", "client_id": GHOST}, "client_not_found"),
        ("post", "/documents", {"item": "x", "deal_id": GHOST}, "deal_not_found"),
    ],
)
def test_a_reference_to_nowhere_is_refused(manager_client, stage, method, path, body, expected):
    response = getattr(manager_client, method)(f"{API}{path}", json=body)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == expected


def test_the_same_fields_work_when_the_record_is_real(manager_client, stage):
    """Обратная сторона: проверка не должна мешать нормальной работе."""
    task = manager_client.post(
        f"{API}/tasks",
        json={
            "title": "Настоящая связка",
            "client_id": stage["client"]["id"],
            "deal_id": stage["deal"]["id"],
        },
    )
    assert task.status_code == 201, task.text
    assert task.json()["client_id"] == stage["client"]["id"]

    board = manager_client.post(
        f"{API}/boards", json={"title": "Доска клиента", "client_id": stage["client"]["id"]}
    )
    assert board.status_code == 201, board.text


def test_an_empty_reference_is_not_an_error(manager_client, stage):
    """«Ничья заявка» и «задача без клиента» — законные состояния."""
    task = manager_client.post(
        f"{API}/tasks", json={"title": "Ничья", "client_id": None, "deal_id": None}
    )
    assert task.status_code == 201, task.text
    assert task.json()["client_id"] is None


def test_a_deleted_record_is_not_a_valid_reference(manager_client, root_client, stage):
    """К лежащему в корзине не привязываемся: запись потом никто не найдёт."""
    doomed = manager_client.post(f"{API}/clients", json={"name": "В корзину"}).json()
    assert manager_client.delete(f"{API}/clients/{doomed['id']}").status_code == 200

    refused = manager_client.post(f"{API}/tasks", json={"title": "x", "client_id": doomed["id"]})
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "client_not_found"
