"""Смена роли сотрудников (root ↔ manager) и окончательное удаление аккаунта."""

from fastapi.testclient import TestClient

from tests.conftest import API, make_manager, register
from web.main import app


def _staff_id(root_client: TestClient, email: str) -> int:
    staff = root_client.get(f"{API}/staff").json()["items"]
    return next(u["id"] for u in staff if u["email"] == email)


def test_root_can_promote_and_demote(root_client):
    manager = make_manager(root_client, "promote@test.local")
    uid = manager.get(f"{API}/auth/me").json()["id"]

    # менеджеру раздел сотрудников недоступен
    assert manager.get(f"{API}/staff").status_code == 403

    # повышение до root — доступ появляется сразу (роль читается из БД на каждый запрос)
    resp = root_client.post(f"{API}/staff/{uid}/role", json={"role": "root"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "root"
    assert manager.get(f"{API}/staff").status_code == 200

    # понижение обратно до менеджера — доступ пропадает
    resp = root_client.post(f"{API}/staff/{uid}/role", json={"role": "manager"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "manager"
    assert manager.get(f"{API}/staff").status_code == 403


def test_cannot_change_own_role(root_client):
    """Свою роль менять нельзя — так последний root не снимет доступ сам с себя."""
    uid = root_client.get(f"{API}/auth/me").json()["id"]
    resp = root_client.post(f"{API}/staff/{uid}/role", json={"role": "manager"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "cannot_change_own_role"


def test_unknown_role_rejected(root_client):
    manager = make_manager(root_client, "badrole@test.local")
    uid = manager.get(f"{API}/auth/me").json()["id"]
    resp = root_client.post(f"{API}/staff/{uid}/role", json={"role": "superuser"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bad_role"


def test_role_change_requires_active(root_client):
    register(TestClient(app), "Pendingrole", "pendingrole@test.local")
    uid = _staff_id(root_client, "pendingrole@test.local")
    resp = root_client.post(f"{API}/staff/{uid}/role", json={"role": "root"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_active"


def test_manager_cannot_change_roles_or_delete(root_client, manager_client):
    victim = make_manager(root_client, "rolevictim@test.local")
    vid = victim.get(f"{API}/auth/me").json()["id"]
    assert manager_client.post(f"{API}/staff/{vid}/role", json={"role": "root"}).status_code == 403
    assert manager_client.delete(f"{API}/staff/{vid}").status_code == 403


def test_delete_user_permanently(root_client):
    victim = make_manager(root_client, "deleteme@test.local")
    vid = victim.get(f"{API}/auth/me").json()["id"]

    # менеджер заводит клиента — авторство должно пережить удаление автора
    created = victim.post(f"{API}/clients", json={"name": "Orphan Client"})
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]
    assert created.json()["manager_id"] == vid

    resp = root_client.delete(f"{API}/staff/{vid}")
    assert resp.status_code == 200, resp.text

    # аккаунт исчез, сессия мертва, email снова свободен
    assert vid not in [u["id"] for u in root_client.get(f"{API}/staff").json()["items"]]
    assert victim.get(f"{API}/auth/me").status_code == 401
    assert register(TestClient(app), "Deleteme", "deleteme@test.local").status_code == 201

    # клиент остался, но менеджер обнулён (ON DELETE SET NULL)
    client = root_client.get(f"{API}/clients/{client_id}")
    assert client.status_code == 200
    assert client.json()["manager_id"] is None


def test_cannot_delete_self(root_client):
    uid = root_client.get(f"{API}/auth/me").json()["id"]
    resp = root_client.delete(f"{API}/staff/{uid}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "cannot_delete_self"


def test_delete_disabled_user(root_client):
    victim = make_manager(root_client, "disabledgone@test.local")
    vid = victim.get(f"{API}/auth/me").json()["id"]
    assert root_client.post(f"{API}/staff/{vid}/disable").status_code == 200
    # деактивированного тоже можно удалить окончательно
    assert root_client.delete(f"{API}/staff/{vid}").status_code == 200
    assert vid not in [u["id"] for u in root_client.get(f"{API}/staff").json()["items"]]


def test_shtat_znaet_otkrytye_zayavki_sotrudnika(root_client):
    """У сотрудника в штате — число открытых заявок (план М-02): «кто перегружен»
    спрашивают у штата, а не у канбана по одному человеку."""
    me = root_client.get(f"{API}/auth/me").json()
    klient = root_client.post(f"{API}/clients", json={"name": "Клиент для счёта штата"}).json()
    deal = root_client.post(
        f"{API}/deals", json={"title": "Заявка для счёта штата", "client_id": klient["id"], "manager_id": me["id"]}
    )
    assert deal.status_code == 201, deal.text
    [ya] = [u for u in root_client.get(f"{API}/staff").json()["items"] if u["id"] == me["id"]]
    assert ya["deals_open"] >= 1
