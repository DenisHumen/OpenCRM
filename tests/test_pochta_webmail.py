"""Веб-почта (`/mail/boxes`): ящик целиком по IMAP/SMTP — без сети.

На место сервера встаёт почтовый ящик в памяти (`FakeImap`/`FakeSmtp` через
`webmail_imap.set_*_factory`): папки, UID, флаги, MOVE, APPEND. Проверяется то,
что легко сломать и трудно заметить глазами: чужой ящик виден, HTML письма
несёт скрипт, внешняя картинка грузится сама, скрытая копия уезжает адресатам,
копия в «Отправленные» не легла, отправленное не попало в ленту клиента.
Протокол против настоящего Dovecot проверен живьём (docs/bloki/32, «Веб-почта»).
"""

from __future__ import annotations

import email
import email.policy
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import pytest
from fastapi.testclient import TestClient

from core.security import secretbox
from core.services import mail_server_service, webmail_imap, webmail_service
from core.services.webmail_imap import Papka, Poisk, Svodka, WebmailError
from database.models import MailAccount, MailBoxAccess, MailServer
from database.models.mail_server import MODE_SSH, STATUS_DRAFT, STATUS_READY
from database.session import SessionLocal
from tests.conftest import API, make_manager

BOXES = f"{API}/mail/boxes"
MASTER = "Master-pass-for-tests-1"


# --- почтовый сервер в памяти ---

class FakeServer:
    """Все ящики поддельного сервера: адрес → папки → письма."""

    def __init__(self) -> None:
        self.boxes: dict[str, dict] = {}
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.refuse: set[str] = set()
        self.logins: list[str] = []
        self.clock = 1_000

    def box(self, address: str) -> dict:
        if address not in self.boxes:
            self.boxes[address] = {
                "INBOX": {"flags": set(), "msgs": {}, "next": 1},
                "Sent": {"flags": {"\\sent"}, "msgs": {}, "next": 1},
                "Drafts": {"flags": {"\\drafts"}, "msgs": {}, "next": 1},
                "Trash": {"flags": {"\\trash"}, "msgs": {}, "next": 1},
                "Junk": {"flags": {"\\junk"}, "msgs": {}, "next": 1},
            }
        return self.boxes[address]

    def put(self, address: str, folder: str, raw: bytes, flags=()) -> int:
        papka = self.box(address)[folder]
        uid = papka["next"]
        papka["next"] += 1
        self.clock += 1
        papka["msgs"][uid] = {"raw": raw, "flags": set(flags), "arrival": self.clock}
        return uid


SERVER = FakeServer()


class FakeImap:
    caps = {"SORT", "MOVE", "UIDPLUS", "SPECIAL-USE", "LIST-STATUS"}

    def __init__(self, n: webmail_imap.Nastroyki) -> None:
        SERVER.logins.append(n.login)
        self.address = n.login.split("*")[0]
        self.papki_ = SERVER.box(self.address)
        self.vybrana: str | None = None
        self.tolko_chtenie = True

    def zakryt(self) -> None:
        pass

    def papki(self) -> list[Papka]:
        return [
            Papka(path=p, delimiter=".", flags=frozenset(v["flags"]), total=len(v["msgs"]),
                  unseen=sum(1 for m in v["msgs"].values() if "\\Seen" not in m["flags"]))
            for p, v in self.papki_.items()
        ]

    def _msgs(self) -> dict:
        assert self.vybrana is not None, "команда без SELECT"
        return self.papki_[self.vybrana]["msgs"]

    def vybrat(self, path: str, *, tolko_chtenie: bool = True) -> int:
        if path not in self.papki_:
            raise WebmailError(code="mail_folder_bad", message=f"no folder {path}")
        self.vybrana, self.tolko_chtenie = path, tolko_chtenie
        return len(self.papki_[path]["msgs"])

    def uidy(self, poisk: Poisk) -> list[int]:
        vyhod = []
        for uid, m in self._msgs().items():
            msg = email.message_from_bytes(m["raw"], policy=email.policy.default)
            if poisk.unread is True and "\\Seen" in m["flags"]:
                continue
            if poisk.flagged is True and "\\Flagged" not in m["flags"]:
                continue
            if poisk.q:
                gde = {"from": str(msg["From"]), "subject": str(msg["Subject"])}.get(
                    poisk.pole, m["raw"].decode("utf-8", "replace") + str(msg["Subject"])
                )
                if poisk.q.lower() not in gde.lower():
                    continue
            vyhod.append((m["arrival"], uid))
        return [uid for _, uid in sorted(vyhod, reverse=True)]

    def svodki(self, uids: list[int]) -> list[Svodka]:
        vyhod = []
        for uid in uids:
            m = self._msgs()[uid]
            r = webmail_service.razobrat_pismo(m["raw"])
            vyhod.append(Svodka(uid=uid, flags=frozenset(m["flags"]), size=len(m["raw"]), subject=r.subject,
                                from_=r.from_, to=r.to, date=r.date, message_id=r.message_id,
                                has_attachments=any(not c.inline for c in r.chasti), preview=r.text[:50]))
        return vyhod

    def syroe(self, uid: int):
        m = self._msgs().get(uid)
        return None if m is None else (m["raw"], frozenset(m["flags"]))

    def flagi(self, uids, dobavit=frozenset(), ubrat=frozenset()) -> None:
        assert not self.tolko_chtenie, "флаги в папке, открытой только на чтение"
        for uid in uids:
            if uid in self._msgs():
                self._msgs()[uid]["flags"] |= set(dobavit)
                self._msgs()[uid]["flags"] -= set(ubrat)

    def peremestit(self, uids, kuda: str) -> None:
        if kuda not in self.papki_:
            raise WebmailError(code="mail_folder_bad", message=f"no folder {kuda}")
        for uid in uids:
            m = self._msgs().pop(uid, None)
            if m is not None:
                SERVER.put(self.address, kuda, m["raw"], m["flags"])

    def stert(self, uids) -> None:
        for uid in uids:
            self._msgs().pop(uid, None)

    def dobavit(self, path: str, pismo: bytes, flags) -> int:
        return SERVER.put(self.address, path, pismo, flags)

    def sozdat(self, path: str) -> None:
        self.papki_[path] = {"flags": set(), "msgs": {}, "next": 1}

    def pereimenovat(self, path: str, novyy: str) -> None:
        self.papki_[novyy] = self.papki_.pop(path)

    def udalit_papku(self, path: str) -> None:
        del self.papki_[path]


class FakeSmtp:
    def __init__(self, n: webmail_imap.Nastroyki) -> None:
        self.n = n

    def otpravit(self, ot: str, komu: list[str], pismo: bytes) -> dict[str, str]:
        SERVER.sent.append((ot, list(komu), pismo))
        return {a: "550 no such user" for a in komu if a in SERVER.refuse}

    def zakryt(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_mail():
    SERVER.sent.clear()
    SERVER.refuse.clear()
    webmail_imap.set_imap_factory(FakeImap)
    webmail_imap.set_smtp_factory(FakeSmtp)
    yield
    webmail_imap.reset_imap_factory()
    webmail_imap.reset_smtp_factory()


@pytest.fixture()
def mail_on(root_client: TestClient):
    """Включаем блок, только если он выключен: переключение пишет журнал (см. test_mail.py)."""
    def enabled() -> bool:
        listed = root_client.get(f"{API}/modules").json()["items"]
        return next(item["enabled"] for item in listed if item["key"] == "mail")

    def turn_on() -> None:
        if not enabled():
            assert root_client.post(f"{API}/modules/mail", json={"enabled": True}).status_code == 200

    turn_on()
    yield
    turn_on()


def _token() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture()
def ystroystvo(mail_on, root_client):
    """Сервер и три его ящика: общий, личный с доступом менеджера (без права слать), чужой."""
    t = _token()
    db = SessionLocal()
    try:
        server = MailServer(
            mode=MODE_SSH, status=STATUS_READY, hostname=f"mail.c{t}.test", imap_host="10.255.0.1",
            master_user="opencrm",
            master_password_encrypted=secretbox.encrypt(MASTER, mail_server_service.MASTER_PURPOSE),
        )
        db.add(server)
        db.flush()

        def box(local: str, shared: bool) -> MailAccount:
            b = MailAccount(title=f"{local} {t}", address=f"{local}-{t}@c{t}.test", imap_host="", smtp_host="",
                            login="", server_id=server.id, is_shared=shared)
            db.add(b)
            db.flush()
            return b

        shared, personal, foreign = box("shared", True), box("personal", False), box("foreign", False)
        db.commit()
        ids = {"server": server.id, "shared": shared.id, "personal": personal.id, "foreign": foreign.id,
               "t": t, "addr": {"shared": shared.address, "personal": personal.address,
                                "foreign": foreign.address}}
    finally:
        db.close()
    yield ids
    db = SessionLocal()
    try:
        for key in ("shared", "personal", "foreign"):
            b = db.get(MailAccount, ids[key])
            if b is not None:
                db.delete(b)
        s = db.get(MailServer, ids["server"])
        if s is not None:
            db.delete(s)
        db.commit()
    finally:
        db.close()


def _dat_dostup(account_id: int, user_id: int, can_send: bool) -> None:
    db = SessionLocal()
    try:
        db.add(MailBoxAccess(account_id=account_id, user_id=user_id, can_send=can_send))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def menedzher(root_client, ystroystvo):
    client = make_manager(root_client, f"webmail-{ystroystvo['t']}@test.local")
    me = client.get(f"{API}/auth/me").json()
    user_id = (me.get("user") or me)["id"]
    _dat_dostup(ystroystvo["personal"], user_id, can_send=False)
    return client


def pismo(
    subject: str,
    *,
    ot: str = "Alice <alice@example.test>",
    komu: str = "office@example.test",
    text: str = "hello",
    html: str | None = None,
    vlozheniya: list[tuple[str, str, bytes]] = (),
    inline: list[tuple[str, str, bytes]] = (),
    message_id: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = ot, komu, subject
    msg["Date"] = formatdate()
    msg["Message-ID"] = message_id or make_msgid(domain="example.test")
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
        for cid, tip, data in inline:
            osn, _, pod = tip.partition("/")
            msg.get_payload()[1].add_related(data, maintype=osn, subtype=pod, cid=f"<{cid}>")
    for imya, tip, data in vlozheniya:
        osn, _, pod = tip.partition("/")
        msg.add_attachment(data, maintype=osn, subtype=pod, filename=imya)
    return msg.as_bytes(policy=email.policy.SMTP)


# --- кто какие ящики видит ---

def test_spisok_yashchikov_bez_seti_i_po_dostupu(menedzher, root_client, ystroystvo):
    """Список ящиков — из базы: сервер в этот момент может лежать, а меню рисоваться обязано."""
    def ne_hodit(n):
        raise AssertionError("GET /mail/boxes пошёл в сеть")

    webmail_imap.set_imap_factory(ne_hodit)
    ids = {b["id"]: b for b in menedzher.get(BOXES).json()}
    assert ystroystvo["shared"] in ids and ystroystvo["personal"] in ids
    assert ystroystvo["foreign"] not in ids, "личный ящик без строки доступа виден менеджеру"
    assert ids[ystroystvo["shared"]]["can_send"] is True
    assert ids[ystroystvo["personal"]]["can_send"] is False, "доступ без права отправки"
    assert ids[ystroystvo["personal"]]["is_shared"] is False
    vse = {b["id"] for b in root_client.get(BOXES).json()}
    assert {ystroystvo["shared"], ystroystvo["personal"], ystroystvo["foreign"]} <= vse


def test_chuzhoy_yashchik_otvechaet_404(menedzher, ystroystvo):
    """Невидимый ящик неотличим от несуществующего — ни папок, ни писем, ни отправки."""
    chuzhoy = ystroystvo["foreign"]
    SERVER.put(ystroystvo["addr"]["foreign"], "INBOX", pismo("secret"))
    for otvet in (
        menedzher.get(f"{BOXES}/{chuzhoy}/folders"),
        menedzher.get(f"{BOXES}/{chuzhoy}/messages"),
        menedzher.get(f"{BOXES}/{chuzhoy}/messages/1"),
        menedzher.get(f"{BOXES}/{chuzhoy}/messages/1/raw"),
        menedzher.post(f"{BOXES}/{chuzhoy}/messages/flags", json={"uids": [1], "seen": True}),
        menedzher.post(f"{BOXES}/{chuzhoy}/send", json={"to": ["x@example.test"], "text": "x"}),
        menedzher.get(f"{BOXES}/999999999/folders"),
    ):
        assert otvet.status_code == 404, otvet.text
        assert otvet.json()["error"]["code"] == "mail_account_not_found"
    assert not SERVER.sent


def test_otpravka_bez_prava_slat_iz_yashchika(menedzher, ystroystvo):
    otvet = menedzher.post(f"{BOXES}/{ystroystvo['personal']}/send",
                           json={"to": ["x@example.test"], "text": "x"})
    assert otvet.status_code == 403, otvet.text
    assert otvet.json()["error"]["code"] == "mail_box_forbidden"
    assert not SERVER.sent


def test_vhod_masterom_na_svoy_server(root_client, ystroystvo):
    """Ящик своего сервера открывается мастером: `ящик*мастер`, пароля ящика CRM не знает."""
    SERVER.logins.clear()
    assert root_client.get(f"{BOXES}/{ystroystvo['shared']}/folders").status_code == 200
    assert SERVER.logins == [f"{ystroystvo['addr']['shared']}*opencrm"]


def test_server_ne_gotov(root_client, ystroystvo):
    db = SessionLocal()
    try:
        db.get(MailServer, ystroystvo["server"]).status = STATUS_DRAFT
        db.commit()
    finally:
        db.close()
    otvet = root_client.get(f"{BOXES}/{ystroystvo['shared']}/folders")
    assert otvet.status_code == 409
    assert otvet.json()["error"]["code"] == "mail_server_not_ready"


# --- папки и письма ---

def test_papki_sluzhebnye_i_schyotchiki(root_client, ystroystvo):
    adres = ystroystvo["addr"]["shared"]
    SERVER.put(adres, "INBOX", pismo("one"))
    SERVER.put(adres, "INBOX", pismo("two"), {"\\Seen"})
    papki = root_client.get(f"{BOXES}/{ystroystvo['shared']}/folders").json()
    assert [p["special"] for p in papki] == ["inbox", "drafts", "sent", "junk", "trash"]
    po_vidu = {p["special"]: p for p in papki}
    assert po_vidu["inbox"]["path"] == "INBOX" and po_vidu["inbox"]["total"] == 2
    assert po_vidu["inbox"]["unseen"] == 1
    assert {"sent", "drafts", "trash", "junk"} <= set(po_vidu)
    assert papki[0]["special"] == "inbox", "входящие — первыми"


def test_spisok_pisem_novye_sverhu_poisk_i_filtry(root_client, ystroystvo):
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    t = ystroystvo["t"]
    starye = SERVER.put(adres, "INBOX", pismo(f"old {t}", ot="Bob <bob@example.test>"), {"\\Seen"})
    novoe = SERVER.put(adres, "INBOX", pismo(f"new {t}", vlozheniya=[("a.txt", "text/plain", b"x")]))
    otmechennoe = SERVER.put(adres, "INBOX", pismo(f"flag {t}"), {"\\Seen", "\\Flagged"})

    spisok = root_client.get(f"{BOXES}/{box}/messages").json()
    assert [m["uid"] for m in spisok["items"]] == [otmechennoe, novoe, starye]
    assert spisok["total"] == 3 and spisok["page"] == 1
    po_uid = {m["uid"]: m for m in spisok["items"]}
    assert po_uid[novoe]["has_attachments"] is True and po_uid[novoe]["seen"] is False
    assert po_uid[otmechennoe]["flagged"] is True
    assert set(po_uid[starye]) >= {"uid", "subject", "from", "to", "date", "seen", "flagged", "answered",
                                    "has_attachments", "size", "preview"}

    stranitsa = root_client.get(f"{BOXES}/{box}/messages?per_page=2&page=2").json()
    assert [m["uid"] for m in stranitsa["items"]] == [starye] and stranitsa["total"] == 3

    neprochitannye = root_client.get(f"{BOXES}/{box}/messages?unread=true").json()
    assert [m["uid"] for m in neprochitannye["items"]] == [novoe]
    pomechennye = root_client.get(f"{BOXES}/{box}/messages?flagged=true").json()
    assert [m["uid"] for m in pomechennye["items"]] == [otmechennoe]
    ot_boba = root_client.get(f"{BOXES}/{box}/messages", params={"q": "from:bob@"}).json()
    assert [m["uid"] for m in ot_boba["items"]] == [starye]
    po_teme = root_client.get(f"{BOXES}/{box}/messages", params={"q": f"subject:new {t}"}).json()
    assert [m["uid"] for m in po_teme["items"]] == [novoe]

    net_papki = root_client.get(f"{BOXES}/{box}/messages?folder=Nope")
    assert net_papki.status_code == 422 and net_papki.json()["error"]["code"] == "mail_folder_bad"


def test_html_pisma_ochishchen_i_kartinki_ne_gruzyatsya(root_client, ystroystvo):
    """Скрипт, обработчики событий и javascript:-ссылки вычищены; внешняя картинка не грузится сама."""
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    html = (
        '<div onclick="steal()" style="color:red;background:url(https://evil.test/bg.png)">Hi'
        '<script>alert(document.cookie)</script>'
        '<a href="javascript:alert(1)">bad</a> <a href="https://good.test/x">good</a>'
        '<img src="https://tracker.test/pixel.gif?id=42" onerror="alert(2)">'
        '<img src="cid:logo123"><iframe src="https://evil.test"></iframe>'
        '<form action="https://evil.test"><input name="p"></form></div>'
    )
    uid = SERVER.put(adres, "INBOX", pismo("html", html=html, inline=[("logo123", "image/png", png)]))

    otvet = root_client.get(f"{BOXES}/{box}/messages/{uid}")
    assert otvet.status_code == 200, otvet.text
    h = otvet.json()["html"]
    for zapreshchyonnoe in ("<script", "alert(", "onclick", "onerror", "javascript:", "<iframe", "<form",
                            "url(", "evil.test"):
        assert zapreshchyonnoe not in h, f"{zapreshchyonnoe!r} осталось в HTML: {h}"
    assert 'data-src="https://tracker.test/pixel.gif?id=42"' in h
    assert ' src="https://tracker.test' not in h, "внешняя картинка грузится без спроса"
    assert 'src="data:image/png;base64,' in h, "cid:-картинка не встроена"
    assert 'href="https://good.test/x"' in h and 'rel="noopener noreferrer"' in h and 'target="_blank"' in h
    assert "color:red" in h, "безопасный стиль письма потерян"
    assert otvet.json()["remote_images"] is True
    assert "\\Seen" in SERVER.box(adres)["INBOX"]["msgs"][uid]["flags"], "открытое письмо не прочитано"


def test_stranitsa_pisma_so_svoey_politikoy(root_client, ystroystvo):
    """Рамка письма грузит его страницей со своей CSP: рамка с `srcDoc` наследовала
    политику CRM, и картинки по кнопке не показывались (проба 09.10.2026)."""
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    html = '<p>Hi</p><img src="https://tracker.test/p.gif"><img src="http://plain.test/q.gif"><script>x()</script>'
    uid = SERVER.put(adres, "INBOX", pismo("stranitsa", html=html))

    bez = root_client.get(f"{BOXES}/{box}/messages/{uid}/html")
    assert bez.status_code == 200, bez.text
    politika = bez.headers["content-security-policy"]
    assert "default-src 'none'" in politika and "img-src data:;" in politika
    assert "allow-scripts" not in politika and "allow-same-origin" not in politika
    assert bez.headers["x-frame-options"] == "SAMEORIGIN"
    assert "<script" not in bez.text and ' src="https://tracker.test' not in bez.text
    assert "\\Seen" not in SERVER.box(adres)["INBOX"]["msgs"][uid]["flags"], "страница письма ставит прочитанное"

    s_kartinkami = root_client.get(f"{BOXES}/{box}/messages/{uid}/html?images=1")
    assert "img-src data: https:;" in s_kartinkami.headers["content-security-policy"]
    assert ' src="https://tracker.test/p.gif"' in s_kartinkami.text
    assert ' src="http://plain.test' not in s_kartinkami.text, "картинка по http выдаёт адрес смотрящего"


def test_vlozhenie_i_iskhodnik_skachivayutsya(root_client, ystroystvo):
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    dannye = b"%PDF-1.4\n" + bytes(range(256)) * 10
    uid = SERVER.put(adres, "INBOX", pismo("doc", vlozheniya=[("счёт №1.pdf", "application/pdf", dannye)]))
    pismo_ = root_client.get(f"{BOXES}/{box}/messages/{uid}").json()
    [vl] = pismo_["attachments"]
    assert vl["name"] == "счёт №1.pdf" and vl["size"] == len(dannye) and vl["inline"] is False

    fayl = root_client.get(f"{BOXES}/{box}/messages/{uid}/attachments/{vl['part']}")
    assert fayl.status_code == 200 and fayl.content == dannye
    assert fayl.headers["content-disposition"].startswith("attachment;")
    assert "filename*=utf-8''" in fayl.headers["content-disposition"]
    assert fayl.headers["content-security-policy"] == "sandbox"
    assert fayl.headers["x-content-type-options"] == "nosniff"

    assert root_client.get(f"{BOXES}/{box}/messages/{uid}/attachments/9").status_code == 404
    syroe = root_client.get(f"{BOXES}/{box}/messages/{uid}/raw")
    assert syroe.status_code == 200 and syroe.content == SERVER.box(adres)["INBOX"]["msgs"][uid]["raw"]
    assert syroe.headers["content-type"].startswith("message/rfc822")
    assert root_client.get(f"{BOXES}/{box}/messages/4242").json()["error"]["code"] == "mail_message_gone"


def test_flagi_perenos_i_udalenie_cherez_korzinu(root_client, ystroystvo):
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    papki = SERVER.box(adres)
    uid = SERVER.put(adres, "INBOX", pismo("move me"))

    otvet = root_client.post(f"{BOXES}/{box}/messages/flags", json={"uids": [uid], "seen": True, "flagged": True})
    assert otvet.status_code == 200, otvet.text
    assert {"\\Seen", "\\Flagged"} <= papki["INBOX"]["msgs"][uid]["flags"]
    root_client.post(f"{BOXES}/{box}/messages/flags", json={"uids": [uid], "seen": False})
    assert papki["INBOX"]["msgs"][uid]["flags"] == {"\\Flagged"}

    assert root_client.post(f"{BOXES}/{box}/folders", json={"name": "Clients"}).status_code == 201
    otvet = root_client.post(f"{BOXES}/{box}/messages/move", json={"uids": [uid], "to": "Clients"})
    assert otvet.status_code == 200, otvet.text
    assert not papki["INBOX"]["msgs"] and len(papki["Clients"]["msgs"]) == 1
    v_papke = next(iter(papki["Clients"]["msgs"]))
    plokho = root_client.post(f"{BOXES}/{box}/messages/move",
                              json={"folder": "Clients", "uids": [v_papke], "to": "Nope"})
    assert plokho.status_code == 422 and plokho.json()["error"]["code"] == "mail_folder_bad"

    otvet = root_client.post(f"{BOXES}/{box}/messages/delete", json={"folder": "Clients", "uids": [v_papke]})
    assert otvet.json()["to"] == "Trash"
    assert not papki["Clients"]["msgs"] and len(papki["Trash"]["msgs"]) == 1, "удаление не через Корзину"
    v_korzine = next(iter(papki["Trash"]["msgs"]))
    otvet = root_client.post(f"{BOXES}/{box}/messages/delete", json={"folder": "Trash", "uids": [v_korzine]})
    assert otvet.json()["to"] is None and not papki["Trash"]["msgs"], "из Корзины — насовсем"


def test_papki_svoi_i_sluzhebnye(root_client, ystroystvo):
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    otvet = root_client.post(f"{BOXES}/{box}/folders", json={"name": "Проекты"})
    assert otvet.status_code == 201 and otvet.json()["path"] == "Проекты"
    otvet = root_client.post(f"{BOXES}/{box}/folders", json={"name": "2026", "parent": "Проекты"})
    assert otvet.json()["path"] == "Проекты.2026"
    otvet = root_client.patch(f"{BOXES}/{box}/folders", json={"path": "Проекты.2026", "new_name": "2027"})
    assert otvet.status_code == 200 and otvet.json()["path"] == "Проекты.2027"
    assert "Проекты.2027" in SERVER.box(adres)

    for zapros in (
        root_client.patch(f"{BOXES}/{box}/folders", json={"path": "Sent", "new_name": "x"}),
        root_client.delete(f"{BOXES}/{box}/folders", params={"path": "INBOX"}),
        root_client.delete(f"{BOXES}/{box}/folders", params={"path": "Trash"}),
    ):
        assert zapros.status_code == 422 and zapros.json()["error"]["code"] == "mail_folder_protected"
    for plokhoe in ("INBOX", "a.b", 'x"y', "*"):
        otvet = root_client.post(f"{BOXES}/{box}/folders", json={"name": plokhoe})
        assert otvet.json()["error"]["code"] == "mail_folder_bad", plokhoe
    assert root_client.delete(f"{BOXES}/{box}/folders", params={"path": "Проекты.2027"}).status_code == 200
    assert "Проекты.2027" not in SERVER.box(adres)


# --- отправка ---

def _zagruzit(client, imya: str, dannye: bytes, tip: str = "application/pdf") -> dict:
    otvet = client.post(f"{BOXES}/uploads", files={"file": (imya, dannye, tip)})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def test_otpravka_kopiya_v_sent_i_lenta_klienta(root_client, ystroystvo):
    """Письмо уходит с вложением, скрытая копия — только в конверте, копия ложится в
    «Отправленные», а в ленте клиента появляется запись — как у кнопки в карточке."""
    t, box, adres = ystroystvo["t"], ystroystvo["shared"], ystroystvo["addr"]["shared"]
    klient = root_client.post(f"{API}/clients", json={"name": f"Webmail {t}", "email": f"buyer-{t}@example.test"})
    assert klient.status_code == 201, klient.text
    klient_id = klient.json()["id"]
    dannye = b"invoice-" + t.encode()
    zagr = _zagruzit(root_client, "invoice.pdf", dannye)

    otvet = root_client.post(f"{BOXES}/{box}/send", json={
        "to": [f"buyer-{t}@example.test"], "bcc": [f"boss-{t}@example.test"],
        "subject": f"Offer {t}", "text": "Please see the offer", "html": "<p>Please see <b>the offer</b>"
        "<script>x()</script></p>", "attachments": [zagr["id"]],
    })
    assert otvet.status_code == 200, otvet.text
    itog = otvet.json()
    assert itog["ok"] is True and itog["refused"] == {} and itog["saved_to_sent"] is True
    assert itog["message_id"].endswith(f"@c{t}.test>"), "Message-ID не на домене ящика"

    [(ot, komu, syroe)] = SERVER.sent
    assert ot == adres and set(komu) == {f"buyer-{t}@example.test", f"boss-{t}@example.test"}
    ushlo = email.message_from_bytes(syroe, policy=email.policy.default)
    assert ushlo["Bcc"] is None, "скрытая копия видна адресатам"
    assert [c.get_filename() for c in ushlo.iter_attachments()] == ["invoice.pdf"]
    assert "<script" not in syroe.decode("utf-8", "replace")

    [kopiya] = SERVER.box(adres)["Sent"]["msgs"].values()
    sohranennoe = email.message_from_bytes(kopiya["raw"], policy=email.policy.default)
    assert sohranennoe["Message-ID"] == itog["message_id"] and "\\Seen" in kopiya["flags"]
    assert sohranennoe["Bcc"] == f"boss-{t}@example.test", "в своей копии скрытая копия должна остаться"

    lenta = root_client.get(f"{API}/clients/{klient_id}/notes?per_page=200").json()["items"]
    assert any(n["kind"] == "email" and f"Offer {t}" in n["body"] for n in lenta), "письма нет в ленте клиента"
    zerkalo = root_client.get(f"{API}/mail/messages", params={"client_id": klient_id}).json()["items"]
    assert [m["subject"] for m in zerkalo] == [f"Offer {t}"] and zerkalo[0]["has_attachments"] is True

    povtor = root_client.post(f"{BOXES}/{box}/send", json={"to": ["x@example.test"], "text": "x",
                                                            "attachments": [zagr["id"]]})
    assert povtor.json()["error"]["code"] == "mail_upload_missing", "загрузка пережила отправку"


def test_otpravka_proveryaet_adresatov_do_smtp(root_client, ystroystvo):
    box = ystroystvo["shared"]
    for telo, kod in (
        ({"to": [], "text": "x"}, "no_recipients"),
        ({"to": ["not-an-address"], "text": "x"}, "bad_recipient"),
        ({"to": [f"a{i}@example.test" for i in range(51)], "text": "x"}, "too_many_recipients"),
        ({"to": ["a@example.test"], "text": "  "}, "body_required"),
        ({"to": ["a@example.test"], "text": "x", "attachments": ["0" * 32]}, "mail_upload_missing"),
        ({"to": ["a@example.test"], "text": "x", "attachments": ["../../etc/passwd"]}, "mail_upload_missing"),
    ):
        otvet = root_client.post(f"{BOXES}/{box}/send", json=telo)
        assert otvet.status_code == 422 and otvet.json()["error"]["code"] == kod, (telo, otvet.text)
    assert not SERVER.sent


def test_chastichnyy_otkaz_ne_oshibka(root_client, ystroystvo):
    box = ystroystvo["shared"]
    SERVER.refuse.add("ghost@example.test")
    otvet = root_client.post(f"{BOXES}/{box}/send",
                             json={"to": ["ghost@example.test", "real@example.test"], "text": "hi"})
    assert otvet.status_code == 200
    assert otvet.json()["refused"] == {"ghost@example.test": "550 no such user"}


def test_otvet_i_peresylka(root_client, ystroystvo):
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    iskhodnoe_id = make_msgid(domain="example.test")
    uid = SERVER.put(adres, "INBOX", pismo("question", message_id=iskhodnoe_id,
                                           vlozheniya=[("plan.pdf", "application/pdf", b"PLAN")]))
    otvet = root_client.post(f"{BOXES}/{box}/send", json={
        "to": ["alice@example.test"], "subject": "Re: question", "text": "answer",
        "reply_to": {"folder": "INBOX", "uid": uid}})
    assert otvet.status_code == 200, otvet.text
    ushlo = email.message_from_bytes(SERVER.sent[-1][2], policy=email.policy.default)
    assert ushlo["In-Reply-To"] == iskhodnoe_id and iskhodnoe_id in ushlo["References"]
    assert "\\Answered" in SERVER.box(adres)["INBOX"]["msgs"][uid]["flags"]

    otvet = root_client.post(f"{BOXES}/{box}/send", json={
        "to": ["bob@example.test"], "subject": "Fwd: question", "text": "see below",
        "forward_of": {"folder": "INBOX", "uid": uid}})
    assert otvet.status_code == 200, otvet.text
    ushlo = email.message_from_bytes(SERVER.sent[-1][2], policy=email.policy.default)
    assert [(c.get_filename(), c.get_content()) for c in ushlo.iter_attachments()] == [("plan.pdf", b"PLAN")]


def test_otvet_i_peresylka_iz_drugogo_yashchika(root_client, ystroystvo):
    """Ответ из ящика B на письмо ящика A читает письмо A, а не письмо B с тем же UID:
    иначе наружу уходили чужие вложения и чужая цепочка (разбор 10.10.2026)."""
    a, b = ystroystvo["addr"]["personal"], ystroystvo["addr"]["shared"]
    id_a = make_msgid(domain="a.example.test")
    uid_a = SERVER.put(a, "INBOX", pismo("from A", message_id=id_a, vlozheniya=[("a.pdf", "application/pdf", b"AAA")]))
    uid_b = SERVER.put(b, "INBOX", pismo("from B", vlozheniya=[("b.pdf", "application/pdf", b"BBB")]))
    assert uid_a == uid_b, "проверка нужна на совпавших UID"
    ssylka = {"folder": "INBOX", "uid": uid_a, "box_id": ystroystvo["personal"]}

    otvet = root_client.post(f"{BOXES}/{ystroystvo['shared']}/send", json={
        "to": ["x@example.test"], "subject": "Fwd: from A", "text": "see", "reply_to": ssylka, "forward_of": ssylka})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["warning"] is None
    ushlo = email.message_from_bytes(SERVER.sent[-1][2], policy=email.policy.default)
    assert [(c.get_filename(), c.get_content()) for c in ushlo.iter_attachments()] == [("a.pdf", b"AAA")]
    assert ushlo["In-Reply-To"] == id_a
    assert {"\\Answered", "$Forwarded"} <= SERVER.box(a)["INBOX"]["msgs"][uid_a]["flags"]
    assert not {"\\Answered", "$Forwarded"} & SERVER.box(b)["INBOX"]["msgs"][uid_b]["flags"]


def test_iskhodnoe_iz_nevidimogo_yashchika_ne_chitaetsya(menedzher, ystroystvo):
    """`box_id` чужого ящика — тот же 404, что и сам ящик: ссылка не обходит список доступа."""
    uid = SERVER.put(ystroystvo["addr"]["foreign"], "INBOX",
                     pismo("secret", vlozheniya=[("secret.pdf", "application/pdf", b"S")]))
    ssylka = {"folder": "INBOX", "uid": uid, "box_id": ystroystvo["foreign"]}
    for put in ("send", "drafts"):
        for pole in ("reply_to", "forward_of"):
            otvet = menedzher.post(f"{BOXES}/{ystroystvo['shared']}/{put}",
                                   json={"to": ["x@example.test"], "text": "x", pole: ssylka})
            assert otvet.status_code == 404, (put, pole, otvet.text)
            assert otvet.json()["error"]["code"] == "mail_account_not_found"
    assert not SERVER.sent and not SERVER.box(ystroystvo["addr"]["shared"])["Drafts"]["msgs"]


def test_iskhodnoe_propalo_otvet_i_chernovik_ne_padayut(root_client, ystroystvo):
    """Исходное удалили, пока писали ответ: черновик сохраняется, письмо уходит — без
    цепочки и вложений исходного, с предупреждением. Отказ терял набранный текст."""
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    papki = SERVER.box(adres)
    uid = SERVER.put(adres, "INBOX", pismo("question", vlozheniya=[("plan.pdf", "application/pdf", b"PLAN")]))
    ssylka = {"folder": "INBOX", "uid": uid}
    pervyy = root_client.post(f"{BOXES}/{box}/drafts",
                              json={"to": [], "text": "v1", "reply_to": ssylka, "forward_of": ssylka})
    assert pervyy.status_code == 201 and pervyy.json()["warning"] is None, pervyy.text

    papki["INBOX"]["msgs"].pop(uid)
    vtoroy = root_client.post(f"{BOXES}/{box}/drafts", json={
        "to": [], "text": "v2", "reply_to": ssylka, "forward_of": ssylka, "draft_uid": pervyy.json()["uid"]})
    assert vtoroy.status_code == 201, vtoroy.text
    assert vtoroy.json()["warning"] == "mail_original_gone"
    assert list(papki["Drafts"]["msgs"]) == [vtoroy.json()["uid"]]

    otvet = root_client.post(f"{BOXES}/{box}/send", json={
        "to": ["a@example.test"], "subject": "Re: question", "text": "answer", "reply_to": ssylka,
        "forward_of": {"folder": "Gone", "uid": 7}, "draft_uid": vtoroy.json()["uid"]})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["warning"] == "mail_original_gone" and otvet.json()["saved_to_sent"] is True
    ushlo = email.message_from_bytes(SERVER.sent[-1][2], policy=email.policy.default)
    assert ushlo["In-Reply-To"] is None and not list(ushlo.iter_attachments())
    assert not papki["Drafts"]["msgs"], "ушедшее письмо осталось в черновиках"


def test_vlozheniya_bez_povtorov_i_summa_do_chteniya(root_client, ystroystvo, monkeypatch):
    """Один id пятьдесят раз — одно вложение; сумма сверяется по диску, файлы не читаются."""
    box = ystroystvo["shared"]
    odna = _zagruzit(root_client, "one.pdf", b"123456")
    otvet = root_client.post(f"{BOXES}/{box}/send",
                             json={"to": ["a@example.test"], "text": "x", "attachments": [odna["id"]] * 50})
    assert otvet.status_code == 200, otvet.text
    ushlo = email.message_from_bytes(SERVER.sent[-1][2], policy=email.policy.default)
    assert [c.get_filename() for c in ushlo.iter_attachments()] == ["one.pdf"]

    ids = [_zagruzit(root_client, f"{n}.pdf", b"123456")["id"] for n in range(2)]
    monkeypatch.setattr(webmail_service, "MAX_PISMO_BAYT", 10)
    chitat = webmail_service.Path.read_bytes

    def ne_chitat(self):
        assert "pochta" not in self.parts, "вложение прочитано до проверки суммы"
        return chitat(self)

    monkeypatch.setattr(webmail_service.Path, "read_bytes", ne_chitat)
    otvet = root_client.post(f"{BOXES}/{box}/send", json={"to": ["a@example.test"], "text": "x", "attachments": ids})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "mail_upload_too_big", otvet.text


def test_kvota_zagruzok_sotrudnika(menedzher, monkeypatch):
    monkeypatch.setattr(webmail_service, "KVOTA_ZAGRUZOK_BAYT", 10)
    pervaya = _zagruzit(menedzher, "a.txt", b"123456", "text/plain")
    otvet = menedzher.post(f"{BOXES}/uploads", files={"file": ("b.txt", b"123456", "text/plain")})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "mail_upload_quota", otvet.text
    me = menedzher.get(f"{API}/auth/me").json()
    papka = webmail_service._papka_zagruzok((me.get("user") or me)["id"])
    assert sorted(p.name for p in papka.iterdir()) == [pervaya["id"], f"{pervaya['id']}.json"], "обрывок остался"


def test_bolshoe_pismo_i_tekst_iz_html(root_client, ystroystvo):
    """Письмо больше мегабайта уходит и сохраняется; `text` при `html` не нужен."""
    from web import middleware
    from web.api.routes import mail_boxes

    assert middleware.MAX_JSON_PISMA >= 2 * mail_boxes.MAX_TELO, "потолок тела не сходится с MAX_TELO"
    box = ystroystvo["shared"]
    html = "<p>" + "Длинная цитата. " * 80_000 + "</p>"
    for put, kod in (("drafts", 201), ("send", 200)):
        otvet = root_client.post(f"{BOXES}/{box}/{put}", json={"to": ["a@example.test"], "subject": "big", "html": html})
        assert otvet.status_code == kod, (put, otvet.text[:300])
    ushlo = email.message_from_bytes(SERVER.sent[-1][2], policy=email.policy.default)
    assert "Длинная цитата." in ushlo.get_body(preferencelist=("plain",)).get_content()
    # Прочим ручкам — прежний мегабайт.
    chuzhaya = root_client.post(f"{API}/clients", json={"name": "x" * 2_000_000})
    assert chuzhaya.status_code == 413 and chuzhaya.json()["error"]["code"] == "body_too_large"


def test_spisok_yashchikov_otdayot_uidnext(root_client, ystroystvo):
    """Новое письмо экран узнаёт по UIDNEXT: непрочитанных больше и от чужой отметки."""
    db = SessionLocal()
    try:
        box = db.get(MailAccount, ystroystvo["shared"])
        box.inbox_uidnext, box.inbox_uidvalidity = 42, 1_700_000_000
        db.commit()
    finally:
        db.close()
    yashchiki = {b["id"]: b for b in root_client.get(BOXES).json()}
    assert yashchiki[ystroystvo["shared"]]["inbox_uidnext"] == 42
    assert yashchiki[ystroystvo["shared"]]["inbox_uidvalidity"] == 1_700_000_000, "смену UIDVALIDITY не отличить"
    assert yashchiki[ystroystvo["personal"]]["inbox_uidnext"] is None


def test_lichnyy_yashchik_ne_pishet_v_lentu_klienta(root_client, ystroystvo):
    """Ленту клиента видят все, кто видит клиента; личную переписку — только допущенные."""
    t = ystroystvo["t"]
    klient = root_client.post(f"{API}/clients", json={"name": f"Private {t}", "email": f"vip-{t}@example.test"})
    assert klient.status_code == 201, klient.text
    otvet = root_client.post(f"{BOXES}/{ystroystvo['personal']}/send",
                             json={"to": [f"vip-{t}@example.test"], "subject": f"Discount {t}", "text": "secret"})
    assert otvet.status_code == 200, otvet.text
    lenta = root_client.get(f"{API}/clients/{klient.json()['id']}/notes?per_page=200").json()["items"]
    assert not any(f"Discount {t}" in n["body"] for n in lenta), "личное письмо легло в общую ленту клиента"
    zerkalo = root_client.get(f"{API}/mail/messages", params={"client_id": klient.json()["id"]}).json()["items"]
    assert [m["subject"] for m in zerkalo] == [f"Discount {t}"], "в зеркале ящика письма нет"


def test_chernovik_zamenyaetsya_i_snimaetsya_otpravkoy(root_client, ystroystvo):
    adres, box = ystroystvo["addr"]["shared"], ystroystvo["shared"]
    chernoviki = SERVER.box(adres)["Drafts"]["msgs"]
    pervyy = root_client.post(f"{BOXES}/{box}/drafts", json={"to": [], "subject": "draft", "text": "v1"})
    assert pervyy.status_code == 201, pervyy.text
    uid1 = pervyy.json()["uid"]
    assert {"\\Draft", "\\Seen"} <= chernoviki[uid1]["flags"]
    vtoroy = root_client.post(f"{BOXES}/{box}/drafts", json={
        "to": ["a@example.test"], "bcc": ["hidden@example.test"], "subject": "draft", "text": "v2",
        "draft_uid": uid1})
    uid2 = vtoroy.json()["uid"]
    assert list(chernoviki) == [uid2], "старый черновик не снят"
    otkryt = root_client.get(f"{BOXES}/{box}/messages/{uid2}", params={"folder": "Drafts"}).json()
    assert otkryt["bcc"] == [{"name": "", "email": "hidden@example.test"}], "скрытая копия черновика потерялась"
    assert otkryt["text"].strip() == "v2"
    otvet = root_client.post(f"{BOXES}/{box}/send",
                             json={"to": ["a@example.test"], "subject": "draft", "text": "v3", "draft_uid": uid2})
    assert otvet.status_code == 200 and not chernoviki, "отправленный черновик остался"


def test_zagruzka_predel_i_chuzhaya(root_client, ystroystvo, monkeypatch, menedzher):
    monkeypatch.setattr(webmail_service, "MAX_VLOZHENIE_BAYT", 10)
    otvet = root_client.post(f"{BOXES}/uploads", files={"file": ("big.bin", b"x" * 11, "application/octet-stream")})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "mail_upload_too_big"
    monkeypatch.setattr(webmail_service, "MAX_VLOZHENIE_BAYT", 25 * 1024 * 1024)
    zagr = _zagruzit(root_client, "../../secret name.txt", b"abc", "text/plain")
    assert zagr["name"] == "secret name.txt" and zagr["size"] == 3
    # Загрузка — своя у каждого: чужой id не прикрепить.
    chuzhaya = menedzher.post(f"{BOXES}/{ystroystvo['shared']}/send",
                              json={"to": ["a@example.test"], "text": "x", "attachments": [zagr["id"]]})
    assert chuzhaya.json()["error"]["code"] == "mail_upload_missing"


def test_blok_vyklyuchen(root_client, ystroystvo):
    assert root_client.post(f"{API}/modules/mail", json={"enabled": False}).status_code == 200
    try:
        otvet = root_client.get(BOXES)
        assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "module_disabled"
    finally:
        assert root_client.post(f"{API}/modules/mail", json={"enabled": True}).status_code == 200


# --- разбор ответов настоящего Dovecot (снято с лаборатории 09.10.2026) ---

FETCH_DOVECOT = [(
    b'1 (UID 1 FLAGS (\\Seen \\Recent) RFC822.SIZE 4107 INTERNALDATE "09-Oct-2026 18:21:04 +0000" '
    b'PREVIEW "Hello world bad good" ENVELOPE ("Fri, 09 Oct 2026 18:21:04 +0000" '
    b'"C-live 2878ad08 =?utf-8?b?0J/RgNC40LLQtdGC?=" (("U1 Office" NIL "u1" "gamma.test")) '
    b'(("U1 Office" NIL "u1" "gamma.test")) (("U1 Office" NIL "u1" "gamma.test")) ((NIL NIL "u2" "gamma.test")) '
    b'NIL ((NIL NIL "c-live" "gamma.test")) NIL "<179157006410.1.11740240056729968464@gamma.test>") '
    b'BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 22 1 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "quoted-printable" 192 3 NIL NIL NIL NIL) "alternative" '
    b'("boundary" "===============4835754489776492337==") NIL NIL NIL)("application" "pdf" NIL NIL NIL '
    b'"base64" 2850 NIL ("attachment" ("filename" {17}',
    "счёт №1.pdf".encode(),
), b')) NIL NIL) "mixed" ("boundary" "===============5817752940555899904==") NIL NIL NIL))']


def test_razbor_fetch_dovecot():
    [attrs] = webmail_imap.fetch_atributy(FETCH_DOVECOT)
    s = webmail_imap.svodka_iz_fetch(attrs)
    assert s.uid == 1 and s.size == 4107 and "\\Seen" in s.flags
    assert s.subject == "C-live 2878ad08 Привет"
    assert s.from_ == {"name": "U1 Office", "email": "u1@gamma.test"}
    assert s.to == [{"name": "", "email": "u2@gamma.test"}]
    assert s.message_id == "<179157006410.1.11740240056729968464@gamma.test>"
    assert s.has_attachments is True and s.preview == "Hello world bad good"
    assert s.date is not None and s.date.isoformat() == "2026-10-09T18:21:04"


def test_razbor_list_i_imena_papok():
    znach = webmail_imap.razobrat([b'(\\Sent) "." Sent', b'(\\HasNoChildren) "." "&BB8EQAQ+BDUEOgRCBEs-.2026"',
                                   b'() "/" {4}', ])
    assert znach[0] == ["\\Sent"] and znach[1] == b"." and znach[2] == "Sent"
    assert webmail_imap.mutf7_decode(webmail_imap._str(znach[5])) == "Проекты.2026"
    for imya in ("Проекты", "a&b", "日本語 папка", "plain"):
        assert webmail_imap.mutf7_decode(webmail_imap.mutf7_encode(imya)) == imya
    assert webmail_imap.mutf7_encode("Проекты") == "&BB8EQAQ+BDUEOgRCBEs-"
    assert webmail_imap.imya_papki('a"b\\c') == '"a\\"b\\\\c"'


def test_otpechatok_sertifikata():
    """Отпечаток сверяется постоянным временем и без двоеточий/регистра."""
    class Sock:
        def getpeercert(self, binary_form=False):
            return b"cert-der"

        def close(self):
            pass

    import hashlib
    import ssl

    pin = hashlib.sha256(b"cert-der").hexdigest()
    webmail_imap.proverit_pin(Sock(), pin.upper())
    webmail_imap.proverit_pin(Sock(), ":".join(pin[i:i + 2] for i in range(0, 64, 2)))
    with pytest.raises(ssl.SSLError):
        webmail_imap.proverit_pin(Sock(), "00" * 32)
