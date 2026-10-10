"""Ящики своего почтового сервера: создание, пароль, адрес, квота, доступ, псевдонимы,
сверка и наблюдатель новой почты (docs/bloki/32-pochtovyy-server.md).

Сети нет: агент на сервере подменён `FakeHost` через `mail_host.set_host_factory` —
почтовый сервер, живущий в словаре. Проверяется не docker-mailserver, а то, что легко
сломать незаметно: пароль ящика в базе и журнале, права, порядок «агент → база» и
пуш о новом письме только тем, кто ящик видит.
"""

import re

import httpx
import pytest
from sqlalchemy import delete, select

from core.services import mail_host, mail_server_service, mail_watcher, push_service
from database.models import AuditEvent, MailAccount, MailAlias, MailDomain, MailServer, PushSubscription
from database.models.mail_server import MODE_LOCAL, STATUS_INSTALLING, STATUS_READY
from database.session import SessionLocal
from tests.conftest import API
from tests.test_mail import mail_on  # noqa: F401 — фикстура
from tests.test_push import Brauzer
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры

ADMIN = f"{API}/mail/admin"
DOMEN = "beta-b.test"
DOMEN2 = "beta-b2.test"


class FakeHost:
    """docker-mailserver в словаре: ящики, псевдонимы, счётчики входящих."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.aliases: set[tuple[str, str]] = set()
        self.status: dict[str, dict] = {}
        self.headers: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, tuple[str, str]] = {}

    def vyzvat(self, op: str, args: dict | None = None, *, timeout: float = 60) -> dict:
        args = dict(args or {})
        self.calls.append((op, args))
        if op in self.fail:
            raise mail_host.MailHostError(*self.fail[op])
        if op == "account_add":
            if args["address"] in self.accounts:
                raise mail_host.MailHostError("mail_box_exists", "exists")
            self.accounts[args["address"]] = {"password": args["password"], "quota_mb": args.get("quota_mb")}
            return {}
        if op == "account_password":
            self.accounts[args["address"]]["password"] = args["password"]
            return {}
        if op == "account_quota":
            self.accounts[args["address"]]["quota_mb"] = args["quota_mb"]
            return {}
        if op == "account_delete":
            if args["address"] not in self.accounts:
                raise mail_host.MailHostError("mail_box_not_on_server", "mailbox not found")
            self.accounts.pop(args["address"])
            self.aliases = {(s, d) for s, d in self.aliases if d != args["address"]}
            return {"kept": "x" if args["keep_data"] else ""}
        if op == "account_rename":
            stary = self.accounts.pop(args["old"])
            self.accounts[args["new"]] = {**stary, "password": args["password"]}
            self.aliases = {(s, args["new"] if d == args["old"] else d) for s, d in self.aliases}
            if args["keep_alias"]:
                self.aliases.add((args["old"], args["new"]))
            return {}
        if op == "alias_add":
            self.aliases.add((args["source"], args["destination"]))
            return {}
        if op == "alias_delete":
            self.aliases.discard((args["source"], args["destination"]))
            return {}
        if op == "accounts":
            return {
                "accounts": [{"address": a, "quota_mb": v.get("quota_mb"), "used_kb": None}
                             for a, v in sorted(self.accounts.items())],
                "aliases": [{"source": s, "destination": d} for s, d in sorted(self.aliases)],
                "domains": [DOMEN],
                "masters": ["opencrm"],
            }
        if op == "status":
            return {"boxes": [{"address": a, "messages": 0, **st} for a, st in self.status.items()]}
        if op == "headers":
            return {"items": [h for h in self.headers.get(args["address"], []) if h["uid"] > args["since_uid"]]}
        if op == "health":
            return {"services": {"postfix": True, "dovecot": True, "rspamd": True}, "cert": {}, "queue": 0}
        raise mail_host.MailHostError("mail_agent_failed", f"unknown op {op}")

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


@pytest.fixture()
def pochta(root_client, mail_on):  # noqa: F811
    """Готовый сервер с двумя доменами и подменённым агентом. Убирает за собой всё своё."""
    fake = FakeHost()
    mail_host.set_host_factory(lambda server: fake)
    with SessionLocal() as db:
        server = MailServer(
            title="B", mode=MODE_LOCAL, status=STATUS_READY, hostname=f"mail.{DOMEN}",
            imap_host="opencrm-mail", container="opencrm-mail",
        )
        db.add(server)
        db.flush()
        db.add_all([MailDomain(server_id=server.id, domain=DOMEN), MailDomain(server_id=server.id, domain=DOMEN2)])
        db.commit()
        server_id = server.id
    fake.server_id = server_id
    try:
        yield fake
    finally:
        mail_host.reset_host_factory()
        with SessionLocal() as db:
            db.execute(delete(MailAccount).where(
                MailAccount.address.like(f"%@{DOMEN}") | MailAccount.address.like(f"%@{DOMEN2}")
            ))
            db.execute(delete(MailServer).where(MailServer.id == server_id))
            db.commit()


def _sozdat(client, fake, **pola) -> dict:
    otvet = client.post(f"{ADMIN}/boxes", json={"server_id": fake.server_id, "domain": DOMEN, **pola})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def _moy_id(client) -> int:
    return client.get(f"{API}/auth/me").json()["id"]


def _zhurnal(box_id: int) -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(db.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "mailbox", AuditEvent.entity_id == box_id)
            .order_by(AuditEvent.id)
        ))


# --- создание ---


def test_yashchik_s_parolem_odin_raz_i_bez_parolya_v_baze(root_client, pochta):
    otvet = _sozdat(root_client, pochta, local_part="Sales", quota_mb=500, title="Отдел продаж")
    parol = otvet["password"]
    box = otvet["box"]
    assert box["address"] == f"sales@{DOMEN}" and box["kind"] == "server" and box["quota_mb"] == 500
    assert len(parol) == 16 and not set(parol) & set("0O1lI")
    assert pochta.accounts[box["address"]] == {"password": parol, "quota_mb": 500}

    spisok = root_client.get(f"{ADMIN}/boxes").json()["items"]
    stroka = next(b for b in spisok if b["id"] == box["id"])
    assert parol not in str(spisok) and "password" not in stroka
    with SessionLocal() as db:
        zapis = db.get(MailAccount, box["id"])
        assert zapis.password_encrypted is None and zapis.server_id == pochta.server_id
        assert zapis.imap_host == "opencrm-mail" and zapis.is_shared is False
    zhurnal = _zhurnal(box["id"])
    assert [z.action for z in zhurnal] == ["mailbox.created"]
    assert all(parol not in (z.value_after or "") for z in zhurnal)


def test_svoy_parol_proveryaetsya_do_servera(root_client, pochta):
    for plohoy in ("short", "office-box", "OFFICE-BOX"):
        otvet = root_client.post(f"{ADMIN}/boxes", json={
            "server_id": pochta.server_id, "domain": DOMEN, "local_part": "office-box", "password": plohoy,
        })
        assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "mail_password_weak", plohoy
    svoy = _sozdat(root_client, pochta, local_part="office-box", password="Svoy-parol-2026")
    assert svoy["password"] == "Svoy-parol-2026"
    assert pochta.ops() == ["account_add"], "отказ проверки дошёл до сервера"


@pytest.mark.parametrize("imya", ["-bad", "bad.", "a..b", "a+b", "Ящик", "x" * 65])
def test_krivoe_imya_otvergaetsya(root_client, pochta, imya):
    otvet = root_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": DOMEN, "local_part": imya})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "mail_local_part_bad"
    assert pochta.calls == []


def test_sluchaynoe_imya_i_podskazka_bez_seti(root_client, pochta):
    podskazka = root_client.get(f"{ADMIN}/suggest", params={"domain": DOMEN}).json()
    assert podskazka["address"] == f"{podskazka['local_part']}@{DOMEN}" and len(podskazka["password"]) == 16
    assert re.fullmatch(r"[a-z]+\.[a-z]+\d{2,3}|[a-z0-9]{10}", podskazka["local_part"])
    assert root_client.get(f"{ADMIN}/suggest").json()["address"] is None
    assert pochta.calls == [], "подсказка ходила в сеть"
    box = _sozdat(root_client, pochta, random=True)["box"]
    assert re.fullmatch(rf"([a-z]+\.[a-z]+\d{{2,3}}|[a-z0-9]{{10}})@{re.escape(DOMEN)}", box["address"])


def test_zanyatyy_adres_i_chuzhoy_domen(root_client, pochta):
    _sozdat(root_client, pochta, local_part="dup")
    povtor = root_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": DOMEN, "local_part": "DUP"})
    assert povtor.status_code == 409 and povtor.json()["error"]["code"] == "mail_box_exists"
    chuzhoy = root_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": "gmail.com", "local_part": "x1"})
    assert chuzhoy.status_code == 422 and chuzhoy.json()["error"]["code"] == "mail_domain_bad"
    assert pochta.ops() == ["account_add"]


def test_server_ne_gotov_i_agent_nedostupen(root_client, pochta):
    pochta.fail["account_add"] = ("mail_agent_unreachable", "connection refused")
    otvet = root_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": DOMEN, "local_part": "down"})
    assert otvet.status_code == 503 and otvet.json()["error"]["code"] == "mail_agent_unreachable"
    with SessionLocal() as db:
        assert db.scalar(select(MailAccount).where(MailAccount.address == f"down@{DOMEN}")) is None
        db.get(MailServer, pochta.server_id).status = STATUS_INSTALLING
        db.commit()
    pochta.fail.clear()
    ne_gotov = root_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": DOMEN, "local_part": "down"})
    assert ne_gotov.status_code == 409 and ne_gotov.json()["error"]["code"] == "mail_server_not_ready"


def test_menedzher_bez_manage_ne_upravlyaet(manager_client, pochta):
    for otvet in (
        manager_client.get(f"{ADMIN}/boxes"),
        manager_client.post(f"{ADMIN}/boxes", json={"server_id": pochta.server_id, "domain": DOMEN, "local_part": "m"}),
        manager_client.get(f"{ADMIN}/suggest"),
    ):
        assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "permission_denied"
    assert pochta.calls == []


# --- правка ---


def test_parol_kvota_i_nazvanie(root_client, pochta):
    box = _sozdat(root_client, pochta, local_part="edit")["box"]
    novyy = root_client.post(f"{ADMIN}/boxes/{box['id']}/password", json={"generate": True}).json()["password"]
    assert pochta.accounts[box["address"]]["password"] == novyy and len(novyy) == 16
    svoy = root_client.post(f"{ADMIN}/boxes/{box['id']}/password", json={"password": "Drugoy-parol-77"})
    assert svoy.json() == {"password": "Drugoy-parol-77"}

    assert root_client.patch(f"{ADMIN}/boxes/{box['id']}", json={"quota_mb": 300}).json()["quota_mb"] == 300
    assert pochta.accounts[box["address"]]["quota_mb"] == 300
    snyata = root_client.patch(f"{ADMIN}/boxes/{box['id']}", json={"quota_mb": None, "title": "Правка"}).json()
    assert snyata["quota_mb"] is None and snyata["title"] == "Правка"
    assert pochta.accounts[box["address"]]["quota_mb"] is None
    plohaya = root_client.patch(f"{ADMIN}/boxes/{box['id']}", json={"quota_mb": 0})
    assert plohaya.json()["error"]["code"] == "mail_quota_bad"

    zhurnal = _zhurnal(box["id"])
    assert [z.action for z in zhurnal].count("mailbox.password_changed") == 2
    assert all("Drugoy-parol-77" not in f"{z.value_before}{z.value_after}" for z in zhurnal)


def test_vneshnemu_yashchiku_parol_ne_menyaetsya(root_client, pochta):
    vneshniy = root_client.post(f"{API}/mail/accounts", json={
        "address": f"ext@{DOMEN}", "imap_host": "imap.x.test", "smtp_host": "smtp.x.test", "password": "pw-external-1",
    }).json()
    otvet = root_client.post(f"{ADMIN}/boxes/{vneshniy['id']}/password", json={"generate": True})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "mail_box_not_on_server"
    # Название и «общий» у внешнего правятся: доступ к нему раздаётся тем же экраном.
    assert root_client.patch(f"{ADMIN}/boxes/{vneshniy['id']}", json={"is_shared": False}).json()["kind"] == "external"


def test_pereimenovanie_vedyot_psevdonimy_i_ostavlyaet_dostup(root_client, pochta, manager_client):
    box = _sozdat(root_client, pochta, local_part="old", users=[{"user_id": _moy_id(manager_client), "can_send": True}])["box"]
    alias = root_client.post(f"{ADMIN}/aliases", json={
        "server_id": pochta.server_id, "source": f"info@{DOMEN}", "destination": box["address"],
    }).json()
    otvet = root_client.post(f"{ADMIN}/boxes/{box['id']}/rename", json={
        "local_part": "new", "domain": DOMEN2, "keep_alias": True, "password": "Novyy-parol-123",
    })
    assert otvet.status_code == 200, otvet.text
    novyy = otvet.json()["box"]
    assert novyy["id"] == box["id"] and novyy["address"] == f"new@{DOMEN2}"
    assert otvet.json()["password"] == "Novyy-parol-123"
    assert f"old@{DOMEN}" not in pochta.accounts and f"new@{DOMEN2}" in pochta.accounts
    assert [a["user_id"] for a in novyy["access"]] == [_moy_id(manager_client)]
    psevdonimy = {(a["source"], a["destination"]) for a in root_client.get(f"{ADMIN}/aliases").json()["items"]}
    assert (f"info@{DOMEN}", f"new@{DOMEN2}") in psevdonimy, "псевдоним остался вести в старый адрес"
    assert (f"old@{DOMEN}", f"new@{DOMEN2}") in psevdonimy
    assert alias["id"] in {a["id"] for a in root_client.get(f"{ADMIN}/aliases").json()["items"]}
    assert "mailbox.renamed" in [z.action for z in _zhurnal(box["id"])]


def test_udalenie_s_pochtoy_na_diske(root_client, pochta):
    box = _sozdat(root_client, pochta, local_part="gone")["box"]
    root_client.post(f"{ADMIN}/aliases", json={
        "server_id": pochta.server_id, "source": f"gone-alias@{DOMEN}", "destination": box["address"],
    })
    otvet = root_client.delete(f"{ADMIN}/boxes/{box['id']}", params={"keep_data": 1})
    assert otvet.status_code == 200, otvet.text
    assert ("account_delete", {"address": box["address"], "keep_data": True}) in pochta.calls
    with SessionLocal() as db:
        assert db.get(MailAccount, box["id"]) is None
        assert db.scalar(select(MailAlias).where(MailAlias.destination == box["address"])) is None
    assert root_client.delete(f"{ADMIN}/boxes/{box['id']}").json()["error"]["code"] == "mail_account_not_found"
    zhurnal = _zhurnal(box["id"])
    assert zhurnal[-1].action == "mailbox.deleted" and "kept on disk" in zhurnal[-1].entity_label


def test_udalenie_yashchika_kotorogo_uzhe_net_na_servere(root_client, pochta):
    box = _sozdat(root_client, pochta, local_part="ghost")["box"]
    pochta.accounts.pop(box["address"])
    assert root_client.delete(f"{ADMIN}/boxes/{box['id']}").status_code == 200


# --- доступ ---


def test_lichnyy_yashchik_vidyat_tolko_dopushchennye(root_client, pochta, role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Почта B — чтение и письмо", ["mail.view", "mail.create"])
    anna = staff_maker("pochta-b-anna@test.local", rol["id"])
    boris = staff_maker("pochta-b-boris@test.local", rol["id"])
    box = _sozdat(root_client, pochta, local_part="private")["box"]
    with SessionLocal() as db:
        db.get(MailAccount, box["id"]).unseen_count = 4
        db.commit()

    def vidit(klient) -> bool:
        return any(b["id"] == box["id"] for b in klient.get(f"{API}/mail/unread").json()["by_box"])

    assert not vidit(anna) and not vidit(boris)
    dostup = root_client.put(f"{ADMIN}/boxes/{box['id']}/access", json=[{"user_id": _moy_id(anna), "can_send": False}])
    assert dostup.status_code == 200 and dostup.json() == [{"user_id": _moy_id(anna), "name": dostup.json()[0]["name"], "can_send": False}]
    assert vidit(anna) and not vidit(boris)
    assert anna.get(f"{API}/mail/unread").json()["count"] >= 4
    # Видит, но писать от ящика нельзя — в отправителях его нет, отправка отказывает.
    assert all(s["id"] != box["id"] for s in anna.get(f"{API}/mail/senders").json()["items"])
    otkaz = anna.post(f"{API}/mail/send", json={"to": ["x@remote.test"], "body": "y", "account_id": box["id"]})
    assert otkaz.status_code == 403 and otkaz.json()["error"]["code"] == "mail_box_forbidden"
    chuzhoy = boris.post(f"{API}/mail/send", json={"to": ["x@remote.test"], "body": "y", "account_id": box["id"]})
    assert chuzhoy.status_code == 404

    root_client.put(f"{ADMIN}/boxes/{box['id']}/access", json=[{"user_id": _moy_id(anna), "can_send": True}])
    assert any(s["id"] == box["id"] for s in anna.get(f"{API}/mail/senders").json()["items"])
    assert root_client.get(f"{ADMIN}/boxes/{box['id']}/access").json()[0]["can_send"] is True
    neizvestnyy = root_client.put(f"{ADMIN}/boxes/{box['id']}/access", json=[{"user_id": 10**8}])
    assert neizvestnyy.status_code == 404 and neizvestnyy.json()["error"]["code"] == "user_not_found"
    assert "mailbox.access_changed" in [z.action for z in _zhurnal(box["id"])]


def test_nastroyki_dlya_telefona(root_client, pochta):
    box = _sozdat(root_client, pochta, local_part="phone")["box"]
    nastroyki = root_client.get(f"{ADMIN}/boxes/{box['id']}/client-settings").json()
    assert nastroyki["imap"] == {"host": f"mail.{DOMEN}", "port": 993, "security": "ssl"}
    assert nastroyki["submission"]["port"] == 587 and nastroyki["login"] == box["address"]


# --- псевдонимы ---


def test_psevdonimy(root_client, pochta):
    box = _sozdat(root_client, pochta, local_part="target")["box"]
    telo = {"server_id": pochta.server_id, "source": f"hello@{DOMEN}", "destination": box["address"]}
    alias = root_client.post(f"{ADMIN}/aliases", json=telo)
    assert alias.status_code == 201, alias.text
    assert (f"hello@{DOMEN}", box["address"]) in pochta.aliases
    assert root_client.post(f"{ADMIN}/aliases", json=telo).json()["error"]["code"] == "mail_alias_exists"
    na_yashchik = root_client.post(f"{ADMIN}/aliases", json={**telo, "source": box["address"], "destination": "a@b.test"})
    assert na_yashchik.json()["error"]["code"] == "mail_alias_bad"
    chuzhoy = root_client.post(f"{ADMIN}/aliases", json={**telo, "source": "hello@gmail.com"})
    assert chuzhoy.json()["error"]["code"] == "mail_domain_bad"
    assert root_client.delete(f"{ADMIN}/aliases/{alias.json()['id']}").status_code == 200
    assert (f"hello@{DOMEN}", box["address"]) not in pochta.aliases
    assert root_client.delete(f"{ADMIN}/aliases/{alias.json()['id']}").json()["error"]["code"] == "mail_alias_not_found"


# --- сверка с сервером ---


def test_sverka_podtyagivaet_yashchiki_i_psevdonimy(root_client, pochta):
    est = _sozdat(root_client, pochta, local_part="known")["box"]
    pochta.accounts[f"found@{DOMEN}"] = {"password": "x", "quota_mb": 200}
    pochta.aliases.add((f"sales@{DOMEN}", f"found@{DOMEN}"))
    pochta.accounts.pop(est["address"])
    with SessionLocal() as db:
        itog = mail_server_service.sverit(db, db.get(MailServer, pochta.server_id))
        db.commit()
    assert itog["added"] == [f"found@{DOMEN}"] and itog["missing"] == [est["address"]]
    assert itog["aliases"]["added"] == 1
    spisok = {b["address"]: b for b in root_client.get(f"{ADMIN}/boxes").json()["items"]}
    assert spisok[f"found@{DOMEN}"]["quota_mb"] == 200 and spisok[f"found@{DOMEN}"]["is_shared"] is False
    assert est["address"] in spisok, "ящик без пары на сервере стёрт молча"


# --- наблюдатель новой почты ---


def test_nablyudatel_schitaet_i_shlyot_push_tem_kto_vidit(root_client, pochta, monkeypatch, role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Почта B — пуш", ["mail.view"])
    vidit = staff_maker("pochta-b-push@test.local", rol["id"])
    ne_vidit = staff_maker("pochta-b-nopush@test.local", rol["id"])
    box = _sozdat(root_client, pochta, local_part="inbox", users=[{"user_id": _moy_id(vidit), "can_send": False}])["box"]
    poslano: list = []
    monkeypatch.setattr(push_service, "razoslat", lambda db, ochered, klient=None: poslano.extend(ochered) or 0)

    pochta.status[box["address"]] = {"unseen": 1, "uidnext": 5, "uidvalidity": 77}
    mail_watcher.obnovit_server(pochta.server_id)
    assert poslano == [], "первый опрос — точка отсчёта, а не сто новых писем"
    with SessionLocal() as db:
        zapis = db.get(MailAccount, box["id"])
        assert (zapis.unseen_count, zapis.inbox_uidnext, zapis.inbox_uidvalidity) == (1, 5, 77)
        assert zapis.status_checked_at is not None

    pochta.status[box["address"]] = {"unseen": 3, "uidnext": 7, "uidvalidity": 77}
    pochta.headers[box["address"]] = [
        {"uid": 4, "from": "Old <old@x.test>", "subject": "старое", "date": ""},
        {"uid": 5, "from": "Ivan <ivan@client.test>", "subject": "Счёт", "date": ""},
        {"uid": 6, "from": "Ivan <ivan@client.test>", "subject": "Счёт 2", "date": ""},
    ]
    mail_watcher.obnovit_server(pochta.server_id)
    zapros = next(a for op, a in pochta.calls if op == "headers")
    assert zapros["since_uid"] == 4, "новые — с прежнего UIDNEXT включительно"
    komu = {s.user_id: s for s in poslano if isinstance(s, push_service.Soobshchenie)}
    assert _moy_id(vidit) in komu and _moy_id(root_client) in komu
    assert _moy_id(ne_vidit) not in komu, "пуш ушёл тому, кто ящика не видит"
    s = komu[_moy_id(vidit)]
    assert s.title == "Ivan" and s.body == "Счёт 2 (+1)"
    assert s.tag == f"opencrm-mail-{box['id']}" and s.url == f"/mail?box={box['id']}&uid=6"
    with SessionLocal() as db:
        assert db.get(MailAccount, box["id"]).unseen_count == 3

    # Ящик пересоздан (другая UIDVALIDITY): счёт заново, без пуша.
    poslano.clear()
    pochta.status[box["address"]] = {"unseen": 0, "uidnext": 50, "uidvalidity": 78}
    mail_watcher.obnovit_server(pochta.server_id)
    assert poslano == []


def test_nablyudatel_pishet_oshibku_servera_i_snimaet_svoyu(root_client, pochta):
    pochta.fail["status"] = ("mail_agent_unreachable", "timeout")
    mail_watcher.obnovit_server(pochta.server_id)
    with SessionLocal() as db:
        assert db.get(MailServer, pochta.server_id).last_error.startswith("status: mail_agent_unreachable")
    pochta.fail.clear()
    mail_watcher.obnovit_server(pochta.server_id)
    with SessionLocal() as db:
        assert db.get(MailServer, pochta.server_id).last_error is None


def test_krug_molchit_pri_vyklyuchennom_bloke(root_client, pochta):
    assert root_client.post(f"{API}/modules/mail", json={"enabled": False}).status_code == 200
    try:
        assert mail_watcher.krug(sverka=False) == {}
        assert pochta.calls == []
    finally:
        assert root_client.post(f"{API}/modules/mail", json={"enabled": True}).status_code == 200


def test_push_o_pisme_shifruetsya_i_vytesnyaet_proshloe(root_client):
    """Обобщённая очередь: готовое сообщение уходит той же дорогой, что и звонок."""
    b = Brauzer("https://fcm.googleapis.com/fcm/send/pochta-b")
    assert root_client.post(f"{API}/push/subscriptions", json=b.podpiska()).status_code == 201
    zaprosy: list[httpx.Request] = []
    soobshchenie = push_service.Soobshchenie(
        user_id=_moy_id(root_client), title="Ivan", body="Счёт", tag="opencrm-mail-7",
        url="/mail?box=7&uid=3", topic="pochta7",
    )
    with SessionLocal() as db:
        with httpx.Client(transport=httpx.MockTransport(lambda z: zaprosy.append(z) or httpx.Response(201))) as k:
            push_service.razoslat(db, [soobshchenie], k)
        db.commit()
    moy = next(z for z in zaprosy if "pochta-b" in str(z.url))
    assert moy.headers["Topic"] == "pochta7" and moy.headers["Urgency"] == "normal"
    assert b.prochest(moy.content) == {
        "title": "Ivan", "body": "Счёт", "tag": "opencrm-mail-7", "url": "/mail?box=7&uid=3", "srochno": False,
    }
    with SessionLocal() as db:
        db.execute(delete(PushSubscription).where(PushSubscription.endpoint == b.endpoint))
        db.commit()
