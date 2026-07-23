"""Регрессии на закрытые уязвимости.

Главная: подмена X-Forwarded-For больше не создаёт новый бакет rate-limit
и не даёт обойти защиту подбора PIN.
"""
from starlette.requests import Request

from tests.conftest import API, login, make_manager, png_bytes
from web.api import deps
from web.main import app
from fastapi.testclient import TestClient


def _request(xff: str | None, peer: str = "203.0.113.7") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 5555)})


def test_client_ip_ignores_xff_without_proxy(monkeypatch):
    # trusted_proxy_hops = 0: заголовок клиента — не источник истины, берём TCP-пир
    monkeypatch.setattr(deps._settings, "trusted_proxy_hops", 0)
    assert deps.client_ip(_request("1.2.3.4")) == "203.0.113.7"
    assert deps.client_ip(_request("1.2.3.4, 5.6.7.8")) == "203.0.113.7"
    assert deps.client_ip(_request(None)) == "203.0.113.7"


def test_client_ip_uses_rightmost_behind_one_proxy(monkeypatch):
    # За одним nginx реальный адрес — последний элемент (его дописал nginx).
    # Всё, что слева, шлёт клиент и подделать может как угодно.
    monkeypatch.setattr(deps._settings, "trusted_proxy_hops", 1)
    assert deps.client_ip(_request("9.9.9.9, 203.0.113.7")) == "203.0.113.7"
    assert deps.client_ip(_request("spoof-a, spoof-b, 198.51.100.5")) == "198.51.100.5"


def test_client_ip_two_proxies(monkeypatch):
    monkeypatch.setattr(deps._settings, "trusted_proxy_hops", 2)
    # клиент, наш прокси-1, наш прокси-2 → доверяем 2-му с конца
    assert deps.client_ip(_request("evil, 203.0.113.7, 10.0.0.1")) == "203.0.113.7"


def test_default_trusted_proxy_hops_is_zero():
    # Fail-safe: без явной настройки XFF не доверяется вовсе.
    from config.settings import Settings
    assert Settings().trusted_proxy_hops == 0


def test_pin_bruteforce_not_bypassable_via_xff(manager_client):
    """Ротация X-Forwarded-For не должна давать новые попытки подбора PIN."""
    board = manager_client.post(
        f"{API}/boards", json={"title": "XFF bypass"}
    ).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"pin": "4821"}
    ).json()
    token = share["token"]

    visitor = TestClient(app)
    # исчерпываем лимит (5 неверных без заголовка)
    for _ in range(5):
        assert visitor.post(f"/b/{token}/pin", data={"pin": "0000"}).status_code == 401

    # теперь любые попытки с РАЗНЫМ X-Forwarded-For всё равно заблокированы,
    # включая верный PIN — обход закрыт
    for i in range(10):
        blocked = visitor.post(
            f"/b/{token}/pin",
            data={"pin": "0000"},
            headers={"X-Forwarded-For": f"9.9.{i}.{i}"},
        )
        assert blocked.status_code == 429, f"XFF {i} обошёл лимит"

    assert visitor.post(
        f"/b/{token}/pin",
        data={"pin": "4821"},
        headers={"X-Forwarded-For": "9.9.100.100"},
        follow_redirects=False,
    ).status_code == 429


def test_password_change_revokes_other_sessions(root_client):
    """Смена пароля выкидывает угнанную сессию, текущую сохраняет."""
    session_a = make_manager(root_client, "revoke@test.local", "manager-pass-123")

    # второй вход тем же аккаунтом — как если бы cookie утекли злоумышленнику
    session_b = TestClient(app)
    assert login(session_b, "revoke@test.local", "manager-pass-123").status_code == 200
    assert session_b.get(f"{API}/auth/me").status_code == 200

    changed = session_a.post(
        f"{API}/auth/me/password",
        json={"old_password": "manager-pass-123", "new_password": "brand-new-pass-1"},
    )
    assert changed.status_code == 200

    # старая (чужая) сессия отозвана, текущая жива
    assert session_b.get(f"{API}/auth/me").status_code == 401
    assert session_a.get(f"{API}/auth/me").status_code == 200
