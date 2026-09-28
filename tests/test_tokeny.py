"""Токены сотрудников к /api/v1 (docs/bloki/30-tokeny-i-mcp.md).

Вход правами сотрудника, строка один раз, отзыв и срок, только чтение, запретные
места, пометка в журнале, потолок запросов, описание для MCP.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from core.ratelimit import SlidingWindowLimiter
from core.services import token_service
from core.utils import now_utc
from database.models import UserToken
from database.session import SessionLocal
from tests.conftest import API
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры
from tests.test_route_guards import api_routes
from web.main import app

TOKENS = f"{API}/tokens"


def _moy_id(client) -> int:
    return client.get(f"{API}/auth/me").json()["id"]


def _vypustit(root_client, user_id: int | None = None, **pola) -> dict:
    otvet = root_client.post(TOKENS, json={"name": "Агент", "user_id": user_id, **pola})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def _po_tokenu(raw: str) -> TestClient:
    """Голый клиент: ни cookie, ни CSRF — только заголовок, как у программы."""
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def test_token_deystvuet_pravami_svoego_sotrudnika(root_client, role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Токен — напоминания", ["tasks.view", "tasks.create"])
    anna = staff_maker("token-anna@test.local", rol["id"])
    tok = _vypustit(root_client, _moy_id(anna))
    agent = _po_tokenu(tok["token"])

    assert agent.get(f"{API}/auth/me").json()["email"] == "token-anna@test.local"
    assert agent.get(f"{API}/tasks").status_code == 200
    # Запись без CSRF: чужая страница заголовок Authorization прислать не может.
    assert agent.post(f"{API}/tasks", json={"title": "От агента"}).status_code == 201
    chuzhoe = agent.get(f"{API}/clients")
    assert chuzhoe.status_code == 403 and chuzhoe.json()["error"]["code"] == "permission_denied"


def test_stroka_tokena_tolko_v_otvete_na_vydachu(root_client):
    tok = _vypustit(root_client)
    assert tok["token"].startswith("ocrm_") and tok["token"].startswith(tok["prefix"])
    spisok = root_client.get(TOKENS).json()["items"]
    assert all("token" not in stroka for stroka in spisok)
    with SessionLocal() as db:
        zapis = db.scalar(select(UserToken).where(UserToken.id == tok["id"]))
        assert tok["token"] not in (zapis.token_hash, zapis.prefix)


def test_otzyv_srok_i_chuzhaya_stroka_ne_puskayut(root_client):
    tok = _vypustit(root_client)
    agent = _po_tokenu(tok["token"])
    assert agent.get(f"{API}/tasks").status_code == 200

    assert _po_tokenu(tok["token"] + "x").get(f"{API}/tasks").json()["error"]["code"] == "bad_token"
    # Байт вне ASCII: Starlette читает заголовок как latin-1, и он не должен дойти до хэша.
    latin = TestClient(app, headers={"Authorization": "Bearer ocrm_ÿ".encode("latin-1")})
    assert latin.get(f"{API}/tasks").json()["error"]["code"] == "bad_token"

    with SessionLocal() as db:
        db.get(UserToken, tok["id"]).expires_at = now_utc() - timedelta(minutes=1)
        db.commit()
    assert agent.get(f"{API}/tasks").json()["error"]["code"] == "token_expired"

    assert root_client.post(f"{TOKENS}/{tok['id']}/revoke").json()["state"] == "revoked"
    otvet = agent.get(f"{API}/tasks")
    assert otvet.status_code == 401 and otvet.json()["error"]["code"] == "token_revoked"


def test_tolko_chtenie_ne_pishet(root_client):
    agent = _po_tokenu(_vypustit(root_client, tolko_chtenie=True)["token"])
    assert agent.get(f"{API}/tasks").status_code == 200
    otvet = agent.post(f"{API}/tasks", json={"title": "Нельзя"})
    assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "token_read_only"


@pytest.mark.parametrize(
    ("metod", "put"),
    [
        ("GET", "/tokens"),
        ("POST", "/tokens"),
        ("GET", "/system/backups"),
        ("POST", "/auth/me/password"),
        ("GET", "/settings/api-keys"),
    ],
)
def test_zapretnye_mesta_zakryty_dazhe_dlya_root(root_client, metod, put):
    """Сейф паролей и выдача доступов — только рукой человека, какой бы ни была роль."""
    agent = _po_tokenu(_vypustit(root_client)["token"])
    otvet = agent.request(metod, f"{API}{put}", json={})
    assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "token_not_allowed"
    assert agent.get(f"{API}/auth/me").status_code == 200


def test_seyf_paroley_zakryt_i_vklyuchyonnyy(root_client):
    """Блок «Ключи» по умолчанию выключен — отказ по блоку не доказал бы запрета."""
    bylo = next(m["enabled"] for m in root_client.get(f"{API}/modules").json()["items"] if m["key"] == "keys")
    root_client.post(f"{API}/modules/keys", json={"enabled": True})
    try:
        otvet = _po_tokenu(_vypustit(root_client)["token"]).get(f"{API}/keys")
        assert otvet.json()["error"]["code"] == "token_not_allowed"
    finally:
        root_client.post(f"{API}/modules/keys", json={"enabled": bylo})


@pytest.mark.parametrize("zapret", token_service.ZAKRYTO, ids=lambda z: z[1])
def test_kazhdyy_zapret_nahodit_marshrut(zapret):
    """Переименованный маршрут открылся бы токену молча — запрет стерёг бы пустое место."""
    assert any(
        token_service.pod_zapretom(zapret, metod, marshrut.path)
        for marshrut in api_routes()
        for metod in marshrut.methods
    ), f"запрет {zapret} не закрывает ни одного маршрута"


def test_deystvie_tokenom_v_zhurnale_pomecheno(root_client):
    tok = _vypustit(root_client, name="Claude")
    agent = _po_tokenu(tok["token"])
    klient = agent.post(f"{API}/clients", json={"name": "Клиент агента"}).json()
    sdelka = agent.post(f"{API}/deals", json={"title": "Сделка агента", "client_id": klient["id"]}).json()
    assert agent.patch(f"{API}/deals/{sdelka['id']}", json={"amount": 500000}).status_code == 200
    zapisi = root_client.get(f"{API}/audit", params={"source": "token"}).json()["items"]
    svoya = next(z for z in zapisi if z["entity_label"] == "Сделка агента")
    assert svoya["source_ref"] == f"#{tok['id']} Claude"
    vydacha = root_client.get(f"{API}/audit", params={"search": "Claude"}).json()["items"]
    assert any(z["action"] == "token.created" and z["source"] == "manual" for z in vydacha)


def test_vremennyy_parol_ne_meshaet_tokenu_a_nevkluchyonnyy_sotrudnik_meshaet(
    root_client, role_maker, staff_maker  # noqa: F811
):
    """Сотрудника-агента заводят с временным паролем и никогда им не входят."""
    rol = role_maker("Токен — агент", ["tasks.view"])
    boris = staff_maker("token-boris@test.local", rol["id"])
    user_id = _moy_id(boris)
    agent = _po_tokenu(_vypustit(root_client, user_id)["token"])
    assert root_client.post(f"{API}/staff/{user_id}/reset-password").status_code == 200
    assert boris.get(f"{API}/tasks").json()["error"]["code"] in ("session_invalid", "password_change_required")
    assert agent.get(f"{API}/tasks").status_code == 200

    assert root_client.post(f"{API}/staff/{user_id}/disable").status_code == 200
    assert agent.get(f"{API}/tasks").json()["error"]["code"] == "token_user_inactive"


def test_vydacha_proveryaet_srok_i_sotrudnika(root_client):
    plohoy = root_client.post(TOKENS, json={"name": "Агент", "days": 7})
    assert plohoy.status_code == 422 and plohoy.json()["error"]["code"] == "bad_term"
    net = root_client.post(TOKENS, json={"name": "Агент", "user_id": 987654})
    assert net.status_code == 422 and net.json()["error"]["code"] == "user_not_active"
    bez_sroka = _vypustit(root_client, days=0)
    assert bez_sroka["expires_at"] is None


def test_vydavat_mozhet_tolko_nastroyshchik(manager_client):
    otvet = manager_client.post(TOKENS, json={"name": "Сам себе"})
    assert otvet.status_code == 403


def test_obrashchenie_otmechaetsya(root_client):
    tok = _vypustit(root_client)
    assert tok["last_used_at"] is None
    _po_tokenu(tok["token"]).get(f"{API}/tasks")
    stroka = next(s for s in root_client.get(TOKENS).json()["items"] if s["id"] == tok["id"])
    assert stroka["last_used_at"] is not None


def test_potolok_zaprosov(root_client, monkeypatch):
    """Агент в цикле — настоящая беда: так уже валилось боевое обновление."""
    monkeypatch.setattr(token_service, "limiter", SlidingWindowLimiter(2, 60, name="token-proba"))
    agent = _po_tokenu(_vypustit(root_client)["token"])
    assert agent.get(f"{API}/tasks").status_code == 200
    assert agent.get(f"{API}/tasks").status_code == 200
    otvet = agent.get(f"{API}/tasks")
    assert otvet.status_code == 429 and otvet.json()["error"]["code"] == "token_rate_limited"


def test_opisanie_dlya_mcp_tolko_o_dostupnom(root_client):
    agent = _po_tokenu(_vypustit(root_client)["token"])
    shema = agent.get(f"{API}/system/openapi.json").json()
    assert shema["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert shema["servers"][0]["url"].endswith("/api/v1")
    puti = set(shema["paths"])
    assert "/api/v1/tasks" in puti and "/api/v1/clients" in puti
    assert not any(p.startswith(("/api/v1/tokens", "/api/v1/keys", "/api/v1/site/")) for p in puti)
    assert "/api/v1/auth/me/password" not in puti and "/api/v1/live" not in puti


def test_token_za_kollegu_tolko_ne_shire_sebya(root_client, role_maker, staff_maker):  # noqa: F811
    """Разбор 28.09.2026: «Director» с settings.manage выпускал токен на root и делал root себя."""
    direktor = staff_maker(
        "token-direktor@test.local", role_maker("Токен — директор", ["settings.manage", "tasks.view"])["id"]
    )
    shire = staff_maker("token-shire@test.local", role_maker("Токен — шире", ["tasks.view", "clients.view"])["id"])
    uzhe = staff_maker("token-uzhe@test.local", role_maker("Токен — уже", ["tasks.view"])["id"])

    na_root = direktor.post(TOKENS, json={"name": "x", "user_id": _moy_id(root_client)})
    assert na_root.status_code == 403 and na_root.json()["error"]["code"] == "cannot_modify_root"
    na_shire = direktor.post(TOKENS, json={"name": "x", "user_id": _moy_id(shire)})
    assert na_shire.status_code == 403 and na_shire.json()["error"]["code"] == "cannot_grant_what_you_lack"
    assert direktor.post(TOKENS, json={"name": "x", "user_id": _moy_id(uzhe)}).status_code == 201


def test_sbros_parolya_tolko_ne_shire_sebya(root_client, role_maker, staff_maker):  # noqa: F811
    """Временный пароль приходит сбросившему — это вход под коллегой."""
    kadrovik = staff_maker("sbros-kadry@test.local", role_maker("Сброс — кадры", ["staff.view", "staff.manage"])["id"])
    shire = staff_maker("sbros-shire@test.local", role_maker("Сброс — шире", ["staff.view", "settings.manage"])["id"])
    uzhe = staff_maker("sbros-uzhe@test.local", role_maker("Сброс — уже", ["staff.view"])["id"])

    otkaz = kadrovik.post(f"{API}/staff/{_moy_id(shire)}/reset-password")
    assert otkaz.status_code == 403 and otkaz.json()["error"]["code"] == "cannot_grant_what_you_lack"
    assert kadrovik.post(f"{API}/staff/{_moy_id(uzhe)}/reset-password").status_code == 200


@pytest.mark.parametrize(("metod", "put"), [("POST", "/roles"), ("POST", "/staff/1/role"), ("POST", "/roles/assign/1")])
def test_token_ne_razdayot_prava(root_client, metod, put):
    agent = _po_tokenu(_vypustit(root_client)["token"])
    otvet = agent.request(metod, f"{API}{put}", json={})
    assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "token_not_allowed"
