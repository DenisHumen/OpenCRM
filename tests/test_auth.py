from fastapi.testclient import TestClient

from tests.conftest import API, login, register
from web.main import app


def test_register_pending_then_approve_then_login(root_client):
    anon = TestClient(app)
    response = register(anon, "Anna", "anna@test.local")
    assert response.status_code == 201
    user = response.json()["user"]
    assert user["status"] == "pending"

    # вход до одобрения запрещён
    blocked = login(TestClient(app), "anna@test.local", "manager-pass-123")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_pending"

    assert root_client.post(f"{API}/staff/{user['id']}/approve").status_code == 200

    ok = login(TestClient(app), "anna@test.local", "manager-pass-123")
    assert ok.status_code == 200
    assert ok.json()["role"] == "manager"


def test_register_duplicate_email(root_client):
    anon = TestClient(app)
    assert register(anon, "Bob", "bob@test.local").status_code == 201
    duplicate = register(anon, "Bob2", "bob@test.local")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_taken"


def test_register_weak_password():
    response = register(TestClient(app), "Weak", "weak@test.local", password="short")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "weak_password"


def test_login_wrong_password_and_rate_limit():
    email = "ratelimit@test.local"
    register(TestClient(app), "Rate", email)
    client = TestClient(app)
    for _ in range(5):
        response = login(client, email, "wrong-password-x")
        assert response.status_code == 401
    limited = login(client, email, "wrong-password-x")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "login_rate_limited"


def test_reject_registration(root_client):
    response = register(TestClient(app), "Rejected", "rejected@test.local")
    user_id = response.json()["user"]["id"]
    assert root_client.post(f"{API}/staff/{user_id}/reject").status_code == 200
    # аккаунт удалён — можно зарегистрироваться заново
    assert register(TestClient(app), "Rejected", "rejected@test.local").status_code == 201


def test_disable_kills_session(root_client, manager_client):
    from tests.conftest import make_manager

    victim = make_manager(root_client, "victim@test.local")
    victim_id = victim.get(f"{API}/auth/me").json()["id"]
    assert root_client.post(f"{API}/staff/{victim_id}/disable").status_code == 200
    # сессия отозвана немедленно
    assert victim.get(f"{API}/auth/me").status_code == 401
    # вход запрещён
    blocked = login(TestClient(app), "victim@test.local", "manager-pass-123")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_disabled"
    # enable возвращает доступ
    assert root_client.post(f"{API}/staff/{victim_id}/enable").status_code == 200
    assert login(TestClient(app), "victim@test.local", "manager-pass-123").status_code == 200


def test_staff_endpoints_root_only(manager_client):
    assert manager_client.get(f"{API}/staff").status_code == 403


def test_locale_saved_in_profile(root_client, manager_client):
    """Язык интерфейса — свойство аккаунта: свой у каждого, живёт в БД."""
    me = manager_client.get(f"{API}/auth/me").json()
    assert me["locale"] == "en"  # английский по умолчанию — и при первой установке тоже
    updated = manager_client.patch(f"{API}/auth/me", json={"locale": "ru"})
    assert updated.status_code == 200
    assert updated.json()["locale"] == "ru"
    # выбор сохранён в БД: новый вход с другого клиента видит ru
    fresh = TestClient(app)
    assert login(fresh, "manager@test.local", "manager-pass-123").json()["locale"] == "ru"
    # у соседа язык свой — переключение одного не задевает остальных
    assert root_client.get(f"{API}/auth/me").json()["locale"] == "en"
    bad = manager_client.patch(f"{API}/auth/me", json={"locale": "de"})
    assert bad.status_code == 422

    manager_client.patch(f"{API}/auth/me", json={"locale": "en"})


def test_password_reset_flow(root_client):
    from tests.conftest import make_manager

    make_manager(root_client, "resetme@test.local")
    staff = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in staff if u["email"] == "resetme@test.local")
    response = root_client.post(f"{API}/staff/{user_id}/reset-password")
    assert response.status_code == 200
    temp_password = response.json()["temp_password"]

    client = TestClient(app)
    assert login(client, "resetme@test.local", temp_password).status_code == 200
    # рабочие эндпоинты закрыты до смены
    assert client.get(f"{API}/clients").status_code == 403
    changed = client.post(
        f"{API}/auth/me/password",
        json={"old_password": temp_password, "new_password": "brand-new-pass-1"},
    )
    assert changed.status_code == 200
    assert client.get(f"{API}/clients").status_code == 200


def test_logout(root_client):
    from tests.conftest import make_manager

    client = make_manager(root_client, "logout@test.local")
    assert client.get(f"{API}/auth/me").status_code == 200
    assert client.post(f"{API}/auth/logout").status_code == 200
    assert client.get(f"{API}/auth/me").status_code == 401


def test_csrf_required_for_mutations(manager_client):
    # запрос с session-cookie, но без CSRF-заголовка — отклоняется
    headers = dict(manager_client.headers)
    headers.pop("X-CSRF-Token", None)
    response = manager_client.post(
        f"{API}/clients", json={"name": "CSRF Test"}, headers={"X-CSRF-Token": "wrong"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
