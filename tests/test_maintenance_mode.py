"""Ручной режим обслуживания: root закрывает сайт, себе доступ оставляет.

Это заслон перед всем приложением, и ошибка здесь стоит дороже обычной: пустить
лишнего — значит не закрыть сайт, а закрыть лишнего — значит запереть root'а
снаружи, и снять режим будет уже неоткуда. Проверяем обе стороны.
"""

import pytest
from fastapi.testclient import TestClient

from core.services import maintenance_mode
from tests.conftest import API, ROOT_EMAIL, ROOT_PASSWORD, login
from web.main import app

MAINT = f"{API}/settings/maintenance"


@pytest.fixture
def closed(root_client):
    """Сайт закрыт на работы; после теста открываем обратно."""
    root_client.post(MAINT, json={"enabled": True, "note": "Переносим базу, вернёмся к 14:00"})
    yield
    root_client.post(MAINT, json={"enabled": False})
    maintenance_mode.invalidate()


def test_only_root_can_close_the_site(manager_client):
    assert manager_client.post(MAINT, json={"enabled": True}).status_code == 403


def test_root_keeps_working_while_others_do_not(root_client, manager_client, closed):
    # root ходит везде как обычно — иначе снять режим было бы неоткуда
    assert root_client.get(f"{API}/clients").status_code == 200
    assert root_client.get(f"{API}/dashboard").status_code == 200

    # менеджер упирается в заглушку
    blocked = manager_client.get(f"{API}/clients")
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "maintenance_mode"


def test_visitors_see_the_page_with_the_note(closed):
    page = TestClient(app).get("/")
    assert page.status_code == 503
    assert page.headers.get("Retry-After")
    assert "Переносим базу, вернёмся к 14:00" in page.text


def test_showcase_is_closed_too(root_client, manager_client):
    """Витрина — та же система: клиент не должен видеть её во время работ."""
    board = manager_client.post(f"{API}/boards", json={"title": "Закрытая"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    anon = TestClient(app)
    assert anon.get(f"/b/{share['token']}").status_code == 200

    root_client.post(MAINT, json={"enabled": True})
    try:
        assert anon.get(f"/b/{share['token']}").status_code == 503
    finally:
        root_client.post(MAINT, json={"enabled": False})
        maintenance_mode.invalidate()


def test_root_does_not_pass_through_to_client_facing_pages(root_client, manager_client):
    """Пропуск для root нужен ради одного: войти в CRM и снять режим.

    К витрине это не относится. Раньше относилось — и root, проверяя свою же
    ссылку, видел работающую страницу и заключал, что режим не сработал. Он
    срабатывал; просто проверяющий был единственным, для кого сайт оставался
    открыт. «Закрыто» на клиентской стороне значит закрыто для всех.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Витрина root'а"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    # закрываем только после подготовки: при закрытом сайте менеджер уже не
    # смог бы создать доску
    root_client.post(MAINT, json={"enabled": True})
    try:
        assert root_client.get(f"/b/{share['token']}").status_code == 503
        assert root_client.get("/d/2000-000001").status_code == 503
        # а в CRM root по-прежнему проходит, иначе режим было бы не снять
        assert root_client.get(f"{API}/clients").status_code == 200
    finally:
        root_client.post(MAINT, json={"enabled": False})
        maintenance_mode.invalidate()


def test_client_facing_pages_are_never_cached(manager_client):
    """Ссылку отзывают, срок истекает, сайт закрывают — страница обязана
    перестать открываться. Из кэша браузера она открывалась бы и дальше."""
    board = manager_client.post(f"{API}/boards", json={"title": "Некэшируемая"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "no-store" in page.headers.get("Cache-Control", ""), (
        "витрина кэшируется: отозванная ссылка продолжит открываться у того, "
        "кто успел её загрузить"
    )


def test_root_can_still_sign_in_while_closed(root_client, closed):
    """Самая дорогая ошибка: закрыть вход и остаться снаружи навсегда."""
    fresh = TestClient(app)
    assert fresh.get("/login").status_code == 200          # страница входа открыта
    assert login(fresh, ROOT_EMAIL, ROOT_PASSWORD).status_code == 200
    assert fresh.get(f"{API}/clients").status_code == 200  # и внутри всё работает


def test_healthz_stays_green_while_closed(closed):
    """Иначе docker сочтёт контейнер больным и начнёт его перезапускать."""
    assert TestClient(app).get("/healthz").status_code == 200


def test_note_is_dropped_when_the_site_reopens(root_client):
    root_client.post(MAINT, json={"enabled": True, "note": "вернёмся к 14:00"})
    root_client.post(MAINT, json={"enabled": False})
    maintenance_mode.invalidate()
    state = root_client.get(MAINT).json()
    assert state["enabled"] is False
    assert state["note"] == "", "прошлое пояснение ввело бы в заблуждение в следующий раз"


def test_who_and_when_are_recorded(root_client, closed):
    """Забытый режим должен быть объясним: видно, кто закрыл и когда."""
    state = root_client.get(MAINT).json()
    assert state["enabled"] is True
    assert state["by"], "не записано, кто закрыл сайт"
    assert state["since"], "не записано, когда закрыли"
