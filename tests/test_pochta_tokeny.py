"""Токен и ящики: все ящики сотрудника или только перечисленные (docs/bloki/30, 32).

Область токена сужает почту поверх прав сотрудника: список ящиков, зеркало писем,
счётчик непрочитанного и управление ящиками видят только «свои» ящики токена.
Пустой список — ни одного ящика, а не все.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.utils import now_utc
from database.models import MailAccount, MailMessage
from database.session import SessionLocal
from tests.conftest import API
from tests.test_mail import mail_on  # noqa: F401 — фикстура
from tests.test_pochta_admin import DOMEN, pochta  # noqa: F401 — фикстура
from web.main import app

TOKENS = f"{API}/tokens"
ADMIN = f"{API}/mail/admin"


def _po_tokenu(raw: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {raw}"})


def _vypustit(root_client, **pola) -> dict:
    otvet = root_client.post(TOKENS, json={"name": "Почтовый агент", **pola})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


@pytest.fixture()
def dva_yashchika(root_client, pochta):  # noqa: F811
    """Два ящика на сервере, у каждого по входящему письму в зеркале."""
    ids = []
    for imya in ("tok-a", "tok-b"):
        otvet = root_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": DOMEN, "local_part": imya})
        assert otvet.status_code == 201, otvet.text
        ids.append(otvet.json()["box"]["id"])
    with SessionLocal() as db:
        for nomer, box_id in enumerate(ids):
            db.get(MailAccount, box_id).unseen_count = nomer + 2
            db.add(MailMessage(
                account_id=box_id, uid=nomer + 1, message_id=f"<tok-{box_id}@b.test>", direction="in",
                subject=f"Письмо ящика {box_id}", from_addr="client@remote.test", to_addrs="x",
                sent_at=now_utc() - timedelta(minutes=nomer),
            ))
        db.commit()
    yield ids


def test_po_umolchaniyu_vse_yashchiki(root_client, dva_yashchika):
    tok = _vypustit(root_client)
    assert tok["mail_scope"] == "all" and tok["mail_boxes"] is None
    agent = _po_tokenu(tok["token"])
    vidno = {b["id"] for b in agent.get(f"{ADMIN}/boxes").json()["items"]}
    assert set(dva_yashchika) <= vidno


def test_token_na_odin_yashchik_vidit_tolko_ego(root_client, dva_yashchika):
    svoy, chuzhoy = dva_yashchika
    tok = _vypustit(root_client, mail_boxes=[svoy])
    assert tok["mail_scope"] == "list" and tok["mail_boxes"] == [svoy]
    v_spiske = next(t for t in root_client.get(TOKENS).json()["items"] if t["id"] == tok["id"])
    assert v_spiske["mail_boxes"] == [svoy] and v_spiske["mail_scope"] == "list"

    agent = _po_tokenu(tok["token"])
    assert [b["id"] for b in agent.get(f"{ADMIN}/boxes").json()["items"]] == [svoy]
    assert agent.get(f"{ADMIN}/boxes/{chuzhoy}/access").json()["error"]["code"] == "mail_account_not_found"
    neprochitannoe = agent.get(f"{API}/mail/unread").json()
    assert neprochitannoe["by_box"] == [{"id": svoy, "unseen": 2}] and neprochitannoe["count"] == 2
    pisma = agent.get(f"{API}/mail/messages").json()
    assert {m["account_id"] for m in pisma["items"]} == {svoy} and pisma["total"] == 1
    chuzhoe = root_client.get(f"{API}/mail/messages", params={"account_id": chuzhoy}).json()["items"][0]
    assert agent.get(f"{API}/mail/messages/{chuzhoe['id']}").status_code == 404
    assert all(s["id"] == svoy for s in agent.get(f"{API}/mail/senders").json()["items"])

    # Свой ящик — управлять можно; заводить новые и псевдонимы — нет.
    assert agent.patch(f"{ADMIN}/boxes/{svoy}", json={"title": "Через токен"}).status_code == 200
    novyy = agent.post(f"{ADMIN}/boxes", json={"server_id": 1, "domain": DOMEN, "local_part": "tok-c"})
    assert novyy.status_code == 403 and novyy.json()["error"]["code"] == "mail_box_forbidden"


def test_pustoy_spisok_ni_odnogo_i_udalyonnyy_ne_otkryvaet_vse(root_client, dva_yashchika):
    pustoy = _po_tokenu(_vypustit(root_client, mail_boxes=[])["token"])
    assert pustoy.get(f"{ADMIN}/boxes").json()["items"] == []
    assert pustoy.get(f"{API}/mail/unread").json() == {"count": 0, "by_box": []}
    assert pustoy.get(f"{API}/mail/messages").json()["total"] == 0

    tok = _vypustit(root_client, mail_boxes=[dva_yashchika[0]])
    assert root_client.delete(f"{ADMIN}/boxes/{dva_yashchika[0]}").status_code == 200
    agent = _po_tokenu(tok["token"])
    assert agent.get(f"{ADMIN}/boxes").json()["items"] == [], "удалённый ящик открыл токену все остальные"
    assert next(t for t in root_client.get(TOKENS).json()["items"] if t["id"] == tok["id"])["mail_boxes"] == []


def test_neizvestnyy_yashchik_ne_vydayotsya(root_client):
    otvet = root_client.post(TOKENS, json={"name": "Кривой", "mail_boxes": [10**8]})
    assert otvet.status_code == 404 and otvet.json()["error"]["code"] == "mail_account_not_found"


def test_bez_tokena_oblast_ne_prilipaet(root_client, dva_yashchika):
    """Область токена живёт в `db.info` запроса и не переезжает к следующему по сессии."""
    _po_tokenu(_vypustit(root_client, mail_boxes=[])["token"]).get(f"{ADMIN}/boxes")
    vidno = {b["id"] for b in root_client.get(f"{ADMIN}/boxes").json()["items"]}
    assert set(dva_yashchika) <= vidno


def test_token_na_odin_yashchik_i_nastroyki_yashchikov(root_client, dva_yashchika):
    """`/mail/accounts*` — та же область токена, что у `/mail/admin`: чужие ящики не видны и
    не правятся, новые не заводятся (разбор 10.10.2026)."""
    vneshniy = root_client.post(f"{API}/mail/accounts", json={"address": "tok-ext@remote.test", "password": "x"})
    assert vneshniy.status_code == 201, vneshniy.text
    svoy, chuzhoy = vneshniy.json()["id"], dva_yashchika[0]
    try:
        agent = _po_tokenu(_vypustit(root_client, mail_boxes=[svoy])["token"])
        assert [a["id"] for a in agent.get(f"{API}/mail/accounts").json()["items"]] == [svoy]
        assert agent.patch(f"{API}/mail/accounts/{svoy}", json={"title": "Через токен"}).status_code == 200
        for otvet in (
            agent.patch(f"{API}/mail/accounts/{chuzhoy}", json={"title": "Чужой"}),
            agent.delete(f"{API}/mail/accounts/{chuzhoy}"),
            agent.post(f"{API}/mail/accounts/{chuzhoy}/check"),
            agent.post(f"{API}/mail/accounts/{chuzhoy}/sync"),
        ):
            assert otvet.status_code == 404, otvet.text
            assert otvet.json()["error"]["code"] == "mail_account_not_found"
        novyy = agent.post(f"{API}/mail/accounts", json={"address": "tok-new@remote.test"})
        assert novyy.status_code == 403 and novyy.json()["error"]["code"] == "mail_box_forbidden"
    finally:
        root_client.delete(f"{API}/mail/accounts/{svoy}")
