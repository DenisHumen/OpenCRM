"""Блоки системы: включение, выключение и главное — независимость друг от друга.

Смысл модульности в том, что дизайн-студия выключает склад, а мастерская —
доски, и обе продолжают работать. Поэтому проверяем не «переключается ли
флажок», а то, ради чего он существует: что выключенный блок исчезает целиком
(включая прямые адреса и публичные ссылки), что при этом остальные разделы
работают, и что данные выключенного блока никуда не деваются.
"""

import pytest
from fastapi.testclient import TestClient

from core import modules
from core.services import modules_service
from tests.conftest import API
from web.main import app

MODULES = f"{API}/modules"

SWITCHABLE = [m.key for m in modules.MODULES if not m.core and m.ready]


@pytest.fixture(autouse=True)
def restore_modules(root_client):
    """Состояние блоков глобальное и переживает тест, а база у тестов общая.

    Без восстановления один упавший тест оставил бы бланки выключенными, и
    посыпались бы совершенно посторонние файлы. Восстанавливаем в фикстуре, а не
    в конце теста: тело до конца может и не дойти.
    """
    yield
    # Кэш мог остаться от подменённого реестра — сбрасываем до восстановления.
    modules_service.invalidate()
    for module in modules.MODULES:
        if not module.core and module.ready:
            root_client.post(f"{MODULES}/{module.key}", json={"enabled": module.default})


def switch(client, key: str, enabled: bool):
    return client.post(f"{MODULES}/{key}", json={"enabled": enabled})


def states(client) -> dict[str, bool]:
    return {m["key"]: m["enabled"] for m in client.get(MODULES).json()["items"]}


def test_every_module_from_the_registry_is_reported(manager_client):
    """Список блоков задаёт код, а не база: иначе новый блок не появился бы у
    того, кто обновился, а снесённый остался бы висеть."""
    listed = states(manager_client)
    assert set(listed) == set(modules.KEYS)


def test_core_modules_cannot_be_switched_off(root_client):
    """Клиенты и заявки — то, на чём держится всё остальное."""
    for key in (m.key for m in modules.MODULES if m.core):
        response = switch(root_client, key, False)
        assert response.status_code == 422, key
        assert response.json()["error"]["code"] == "module_is_core"
        assert states(root_client)[key] is True


def test_unbuilt_modules_cannot_be_switched_on(root_client):
    """Переключатель для того, чего нет, — обещание, а не функция."""
    planned = [m.key for m in modules.MODULES if not m.ready]
    assert planned, "в реестре не осталось запланированных блоков — поправьте тест"
    for key in planned:
        response = switch(root_client, key, True)
        assert response.status_code == 422, key
        assert response.json()["error"]["code"] == "module_not_ready"
        assert states(root_client)[key] is False


def test_unknown_module_is_rejected(root_client):
    response = switch(root_client, "телепатия", True)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_module"


def test_switching_off_documents_does_not_touch_anything_else(root_client, manager_client):
    """Главное свойство модульности.

    Выключили бланки — работа не должна встать: клиенты, заявки, доски и сводка
    обязаны отвечать как ни в чём не бывало. Ради этого всё и затевалось.
    """
    assert switch(root_client, "documents", False).status_code == 200

    closed = manager_client.get(f"{API}/documents")
    assert closed.status_code == 403
    assert closed.json()["error"]["code"] == "module_disabled"

    for path in ("/clients", "/deals", "/boards", "/dashboard", "/pipeline/stages"):
        alive = manager_client.get(f"{API}{path}")
        assert alive.status_code == 200, f"{path} слёг из-за выключенных бланков: {alive.text}"


def test_a_switched_off_module_is_closed_on_direct_links_too(root_client, manager_client):
    """Спрятать пункт меню мало: адрес остаётся в закладках и в старых письмах."""
    client = manager_client.post(f"{API}/clients", json={"name": "Владелец бланка"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Пылесос"}
    ).json()

    assert switch(root_client, "documents", False).status_code == 200

    for path in (
        f"/documents/{doc['id']}",
        f"/documents/by-number/{doc['number']}",
        f"/documents/{doc['id']}/print",
    ):
        blocked = manager_client.get(f"{API}{path}")
        assert blocked.status_code == 403, path
        assert blocked.json()["error"]["code"] == "module_disabled", path

    moved = manager_client.post(f"{API}/documents/{doc['id']}/status", json={"status": "ready"})
    assert moved.status_code == 403


def test_public_qr_link_closes_with_the_module(root_client, manager_client):
    """QR напечатан на бумаге и живёт своей жизнью. Выключили блок — ссылка
    обязана закрыться, иначе выключение косметическое."""
    client = manager_client.post(f"{API}/clients", json={"name": "С квитанцией"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Часы"}
    ).json()

    anon = TestClient(app)
    assert anon.get(f"/d/{doc['number']}").status_code == 200

    assert switch(root_client, "documents", False).status_code == 200
    assert anon.get(f"/d/{doc['number']}").status_code == 404


def test_data_survives_switching_a_module_off_and_on(root_client, manager_client):
    """Выключение — «убрать с глаз», а не «стереть». Передумали — всё на месте."""
    client = manager_client.post(f"{API}/clients", json={"name": "Не потеряться"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Швейная машинка"}
    ).json()

    switch(root_client, "documents", False)
    switch(root_client, "documents", True)

    back = manager_client.get(f"{API}/documents/{doc['id']}")
    assert back.status_code == 200
    assert back.json()["payload"]["fields"]["item"] == "Швейная машинка"
    assert back.json()["number"] == doc["number"]


def test_boards_switch_off_takes_their_share_links_with_them(root_client, manager_client):
    """Витрины — часть досок: выключили доски, делиться нечем."""
    assert switch(root_client, "boards", False).status_code == 200
    assert manager_client.get(f"{API}/boards").status_code == 403
    assert manager_client.patch(f"{API}/shares/1", json={"is_active": False}).status_code == 403
    # а бланки и клиенты продолжают работать
    assert manager_client.get(f"{API}/documents").status_code == 200
    assert manager_client.get(f"{API}/clients").status_code == 200


def test_dependencies_are_respected_in_both_directions(root_client, monkeypatch):
    """Зависимость между двумя необязательными блоками.

    Сейчас такой пары в реестре нет: всё готовое опирается на ядро, которое не
    выключается. Значит обе ветки проверки зависимостей не исполняются ни одним
    тестом — и молча сломаются к появлению склада, который опирается на заявки.
    Поэтому заводим пару прямо здесь: проверяем правило, а не сегодняшний состав
    реестра.
    """
    base = modules.Module(key="test_base", ready=True, default=False)
    on_top = modules.Module(key="test_on_top", ready=True, default=False, requires=("test_base",))
    fake = modules.MODULES + (base, on_top)
    monkeypatch.setattr(modules, "MODULES", fake)
    monkeypatch.setattr(modules, "BY_KEY", {m.key: m for m in fake})
    modules_service.invalidate()

    # надстройку нельзя включить, пока не включено основание
    denied = switch(root_client, "test_on_top", True)
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "module_requires"
    assert "test_base" in denied.json()["error"]["message"]

    assert switch(root_client, "test_base", True).status_code == 200
    assert switch(root_client, "test_on_top", True).status_code == 200

    # и нельзя выбить основание из-под включённой надстройки
    blocked = switch(root_client, "test_base", False)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "module_required_by"
    assert "test_on_top" in blocked.json()["error"]["message"]

    # выключили надстройку — основание снова свободно
    assert switch(root_client, "test_on_top", False).status_code == 200
    assert switch(root_client, "test_base", False).status_code == 200


def test_only_root_can_switch_modules(manager_client):
    """Это решение уровня «каким бизнесом мы занимаемся», а не личная настройка."""
    denied = switch(manager_client, "documents", False)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "root_required"
    # но видеть список менеджер обязан: иначе интерфейс не знает, что показывать
    assert manager_client.get(MODULES).status_code == 200


def test_the_switch_records_who_did_it(root_client):
    """Раздел пропал из меню — спрашивают не только «когда», но и «кто»."""
    switch(root_client, "documents", False)
    entry = next(m for m in root_client.get(MODULES).json()["items"] if m["key"] == "documents")
    assert entry["enabled"] is False
    assert entry["updated_by_name"], "не видно, кто выключил блок"
    assert entry["updated_at"]


def test_switchable_modules_round_trip(root_client):
    """Каждый необязательный блок выключается и включается обратно."""
    assert SWITCHABLE, "нет ни одного переключаемого блока"
    for key in SWITCHABLE:
        assert switch(root_client, key, False).status_code == 200, key
        assert states(root_client)[key] is False, key
        assert switch(root_client, key, True).status_code == 200, key
        assert states(root_client)[key] is True, key
