from datetime import timedelta

from fastapi.testclient import TestClient

from core.utils import ONLINE_THRESHOLD_SECONDS, is_online, now_utc
from tests.conftest import API, png_bytes, register
from web.main import app


# --- аватары ---

def test_avatar_upload_serve_and_delete(manager_client):
    resp = manager_client.post(
        f"{API}/auth/me/avatar",
        files={"file": ("me.png", png_bytes(size=(300, 200)), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    url = resp.json()["avatar_url"]
    assert url and url.startswith("/avatars/") and url.endswith(".webp")

    served = TestClient(app).get(url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"

    cleared = manager_client.delete(f"{API}/auth/me/avatar")
    assert cleared.status_code == 200
    assert cleared.json()["avatar_url"] is None
    assert TestClient(app).get(url).status_code == 404  # файл удалён с диска


def test_avatar_rejects_svg(manager_client):
    # SVG может нести скрипт — аватаром его не принимаем (только растр)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    resp = manager_client.post(
        f"{API}/auth/me/avatar", files={"file": ("x.svg", svg, "image/svg+xml")}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bad_avatar_type"


def test_avatar_rejects_non_image(manager_client):
    resp = manager_client.post(
        f"{API}/auth/me/avatar", files={"file": ("x.pdf", b"%PDF-1.4 not an image", "application/pdf")}
    )
    assert resp.status_code == 422


def test_avatar_requires_auth():
    anon = TestClient(app)
    resp = anon.post(f"{API}/auth/me/avatar", files={"file": ("x.png", png_bytes(), "image/png")})
    assert resp.status_code == 401


# --- присутствие (онлайн / последний раз в сети) ---

def test_is_online_helper():
    assert is_online(None) is False
    assert is_online(now_utc()) is True
    assert is_online(now_utc() - timedelta(seconds=ONLINE_THRESHOLD_SECONDS + 60)) is False


def test_me_reports_online(manager_client):
    me = manager_client.get(f"{API}/auth/me").json()
    assert me["is_online"] is True
    assert me["last_seen_at"] is not None


def test_heartbeat(manager_client):
    r = manager_client.get(f"{API}/auth/heartbeat")
    assert r.status_code == 200
    assert r.json()["is_online"] is True
    assert TestClient(app).get(f"{API}/auth/heartbeat").status_code == 401


def test_staff_list_presence(root_client, manager_client):
    # активный менеджер делает запрос → в сети
    assert manager_client.get(f"{API}/auth/me").json()["is_online"] is True
    # ни разу не входивший (pending) сотрудник — не в сети, last_seen пуст
    register(TestClient(app), "ghost", "ghost@test.local")

    items = root_client.get(f"{API}/staff").json()["items"]
    mgr = next(u for u in items if u["email"] == "manager@test.local")
    ghost = next(u for u in items if u["email"] == "ghost@test.local")

    assert mgr["is_online"] is True
    assert mgr["last_seen_at"] is not None
    assert ghost["is_online"] is False
    assert ghost["last_seen_at"] is None
