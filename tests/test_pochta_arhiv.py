"""Архив писем в CRM: полная копия ящиков в базе и storage (docs/bloki/32, «Архив писем в CRM»).

Сервер — почтовый ящик в памяти на месте `webmail_imap` (как в `test_pochta_webmail.py`),
но со своими UIDVALIDITY/UIDNEXT и INTERNALDATE: на них и держится архив. Проверяется
то, что ломается молча: письмо забрали дважды, удалённое на сервере пропало из CRM,
пересозданная папка смешалась со старой, сырой файл не совпал с письмом, личный ящик
виден чужому, письма личного ящика ушли в ленту клиента.
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from config.settings import get_settings
from core.security import secretbox
from core.services import mail_archive, mail_host, mail_server_service, mail_service, mail_watcher, webmail_imap
from core.services.webmail_imap import Papka
from database.models import MailAccount, MailArchiveFolder, MailArchiveMessage, MailBoxAccess, MailServer
from database.models.mail_server import MODE_SSH, STATUS_DRAFT, STATUS_READY
from database.session import SessionLocal
from tests.conftest import API
from tests.test_mail import mail_on  # noqa: F401 — фикстура
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры
from web.main import app

ARHIV = f"{API}/mail/archive"
DOMEN = "arh-f3.test"


# --- почтовый сервер в памяти -------------------------------------------------------

class Server:
    def __init__(self) -> None:
        self.boxes: dict[str, dict] = {}
        self.validity = 1000

    def box(self, address: str) -> dict:
        if address not in self.boxes:
            self.boxes[address] = {}
            for path, flags in (("INBOX", set()), ("Sent", {"\\sent"}), ("Trash", {"\\trash"}),
                                ("Проекты/Клиент", set()), ("All Mail", {"\\all"})):
                self._papka(address, path, flags)
        return self.boxes[address]

    def _papka(self, address: str, path: str, flags: set) -> None:
        self.validity += 1
        self.boxes[address][path] = {"flags": flags, "msgs": {}, "next": 1, "validity": self.validity}

    def put(self, address: str, folder: str, raw: bytes, flags=()) -> int:
        papka = self.box(address)[folder]
        uid = papka["next"]
        papka["next"] += 1
        papka["msgs"][uid] = {"raw": raw, "flags": set(flags), "date": datetime(2026, 10, 1, 12, 0, uid % 60)}
        if folder != "All Mail" and "All Mail" in self.box(address):
            self.put(address, "All Mail", raw, flags)
        return uid

    def pereso3dat(self, address: str, folder: str) -> None:
        flags = self.box(address)[folder]["flags"]
        self._papka(address, folder, flags)


SERVER = Server()
VHODY: list[str] = []


class FakeImap:
    caps = {"SORT", "MOVE", "UIDPLUS", "SPECIAL-USE", "LIST-STATUS"}

    def __init__(self, n: webmail_imap.Nastroyki) -> None:
        VHODY.append(n.login)
        self.papki_ = SERVER.box(n.login.split("*")[0])
        self.vybrana: str | None = None

    def zakryt(self) -> None:
        pass

    def papki(self) -> list[Papka]:
        return [Papka(path=p, delimiter="/", flags=frozenset(v["flags"]), total=len(v["msgs"]))
                for p, v in self.papki_.items()]

    def vybrat(self, path: str, *, tolko_chtenie: bool = True) -> int:
        assert tolko_chtenie, "архив открыл папку на запись"
        self.vybrana = path
        return len(self.papki_[path]["msgs"])

    def _p(self) -> dict:
        return self.papki_[self.vybrana]

    def sostoyanie(self) -> tuple[int, int]:
        return self._p()["validity"], self._p()["next"]

    def uidy_ot(self, ot: int) -> list[int]:
        return sorted(u for u in self._p()["msgs"] if u >= ot)

    def opisi(self, uids=None) -> dict:
        msgs = self._p()["msgs"]
        return {u: (frozenset(m["flags"]), m["date"], len(m["raw"]))
                for u, m in msgs.items() if uids is None or u in uids}

    def syroe(self, uid: int):
        m = self._p()["msgs"].get(uid)
        return None if m is None else (m["raw"], frozenset(m["flags"]))

    def flagi(self, *a, **k) -> None:
        raise AssertionError("архив не трогает флаги на сервере")


def pismo(tema: str, *, ot: str = "client@remote.test", komu: str = "box@x", html: str | None = None,
          vlozhenie: bytes | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = ot
    msg["To"] = komu
    msg["Subject"] = tema
    msg["Date"] = formatdate(1_790_000_000)
    msg["Message-ID"] = make_msgid(domain="remote.test")
    msg.set_content(f"Текст: {tema}")
    if html:
        msg.add_alternative(html, subtype="html")
    if vlozhenie is not None:
        msg.add_attachment(vlozhenie, maintype="application", subtype="pdf", filename="счёт.pdf")
    # \r\n — как письмо лежит на сервере; копия обязана совпасть байт в байт.
    return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


@pytest.fixture(autouse=True)
def fake_imap():
    webmail_imap.set_imap_factory(FakeImap)
    yield
    webmail_imap.reset_imap_factory()


@pytest.fixture()
def server_id(root_client, mail_on):  # noqa: F811
    with SessionLocal() as db:
        server = MailServer(
            title="F3", mode=MODE_SSH, status=STATUS_READY, hostname=f"mail.{DOMEN}", imap_host=f"mail.{DOMEN}",
            master_user="opencrm",
            master_password_encrypted=secretbox.encrypt("Master-pass-f3", mail_server_service.MASTER_PURPOSE),
        )
        db.add(server)
        db.commit()
        sid = server.id
    try:
        yield sid
    finally:
        with SessionLocal() as db:
            ids = list(db.scalars(select(MailAccount.id).where(MailAccount.address.like(f"%@{DOMEN}"))))
            db.execute(delete(MailArchiveMessage).where(MailArchiveMessage.address.like(f"%@{DOMEN}")))
            if ids:
                db.execute(delete(MailAccount).where(MailAccount.id.in_(ids)))
            db.execute(delete(MailServer).where(MailServer.id == sid))
            db.commit()


def _yashchik(server_id: int, imya: str, *, shared: bool = True, users: tuple[int, ...] = ()) -> int:
    address = f"{imya}@{DOMEN}"
    SERVER.boxes.pop(address, None)
    with SessionLocal() as db:
        box = MailAccount(title=imya, address=address, server_id=server_id, is_shared=shared)
        db.add(box)
        db.flush()
        for user_id in users:
            db.add(MailBoxAccess(account_id=box.id, user_id=user_id))
        db.commit()
        return box.id


def _stroki(box_id: int) -> list[MailArchiveMessage]:
    with SessionLocal() as db:
        return list(db.scalars(
            select(MailArchiveMessage).where(MailArchiveMessage.account_id == box_id)
            .order_by(MailArchiveMessage.folder, MailArchiveMessage.uid, MailArchiveMessage.id)
        ))


def _moy_id(client) -> int:
    return client.get(f"{API}/auth/me").json()["id"]


# --- первый проход, догон, повтор --------------------------------------------------------

def test_pervyy_prohod_beryot_vse_papki_a_syroe_sovpadaet_bayt_v_bayt(root_client, server_id):
    box_id = _yashchik(server_id, "arh-one")
    address = f"arh-one@{DOMEN}"
    syroe = pismo("Счёт за октябрь", html='<p onclick="x()">Привет<script>alert(1)</script></p>',
                  vlozhenie=b"%PDF-1.4 f3")
    SERVER.put(address, "INBOX", syroe, {"\\Seen"})
    SERVER.put(address, "INBOX", pismo("Второе"))
    SERVER.put(address, "Sent", pismo("Ответ", ot=address, komu="client@remote.test"), {"\\Seen"})
    SERVER.put(address, "Проекты/Клиент", pismo("В папке"))

    itog = mail_archive.proyti_yashchik(box_id)
    assert itog["new"] == 4 and itog["pending"] == 0
    stroki = _stroki(box_id)
    assert sorted((s.folder, s.uid) for s in stroki) == [
        ("INBOX", 1), ("INBOX", 2), ("Sent", 1), ("Проекты/Клиент", 1)
    ], "виртуальная «All Mail» — те же письма второй раз, а папка не по-латински потерялась"
    pervoe = next(s for s in stroki if s.folder == "INBOX" and s.uid == 1)
    assert pervoe.subject == "Счёт за октябрь" and pervoe.from_addr == "client@remote.test"
    assert pervoe.flags == "\\Seen" and pervoe.has_attachments and pervoe.removed_at is None
    assert pervoe.received_at == datetime(2026, 10, 1, 12, 0, 1) and pervoe.sent_at is not None
    assert pervoe.raw_sha256 == hashlib.sha256(syroe).hexdigest()
    fayl = get_settings().storage_dir / pervoe.raw_path
    assert gzip.decompress(fayl.read_bytes()) == syroe, "сырая копия не совпала с письмом"
    with SessionLocal() as db:
        html = db.get(MailArchiveMessage, pervoe.id).body_html
    assert "Привет" in html and "<script" not in html and "onclick" not in html

    # Повторный круг ничего не задваивает и в сеть за телами не ходит.
    assert mail_archive.proyti_yashchik(box_id, sverit=False)["new"] == 0
    assert len(_stroki(box_id)) == 4

    # API: список, письмо, сырое, вложение.
    spisok = root_client.get(ARHIV, params={"box": box_id}).json()
    assert spisok["total"] == 4 and {i["folder"] for i in spisok["items"]} == {"INBOX", "Sent", "Проекты/Клиент"}
    odno = root_client.get(f"{ARHIV}/{pervoe.id}").json()
    assert odno["subject"] == "Счёт за октябрь" and odno["seen"] is True
    assert [a["name"] for a in odno["attachments"]] == ["счёт.pdf"]
    raw = root_client.get(f"{ARHIV}/{pervoe.id}/raw")
    assert raw.status_code == 200 and raw.content == syroe
    chast = odno["attachments"][0]["part"]
    vl = root_client.get(f"{ARHIV}/{pervoe.id}/attachments/{chast}")
    assert vl.status_code == 200 and vl.content == b"%PDF-1.4 f3"
    stranitsa = root_client.get(f"{ARHIV}/{pervoe.id}/html")
    assert "Привет" in stranitsa.text and "default-src 'none'" in stranitsa.headers["content-security-policy"]
    poisk = root_client.get(ARHIV, params={"box": box_id, "q": "октябрь"}).json()
    assert [i["id"] for i in poisk["items"]] == [pervoe.id]
    stats = {s["account_id"]: s for s in root_client.get(f"{ARHIV}/stats").json()}
    assert stats[box_id]["messages"] == 4 and stats[box_id]["synced_at"] and stats[box_id]["pending_folders"] == 0


def test_bolshoy_yashchik_beryotsya_kuskami_i_dogonyaetsya(root_client, server_id):
    box_id = _yashchik(server_id, "arh-big")
    address = f"arh-big@{DOMEN}"
    for i in range(mail_archive.PISEM_ZA_KRUG + 7):
        SERVER.put(address, "INBOX", pismo(f"Письмо {i}"))
    pervyy = mail_archive.proyti_yashchik(box_id, sverit=False)
    assert pervyy["new"] == mail_archive.PISEM_ZA_KRUG and pervyy["pending"] == 1
    with SessionLocal() as db:
        nedochitano = next(b for b, _, hvost in mail_archive.repo.yashchiki_dlya_arkhiva(db) if b.id == box_id)
    assert nedochitano is not None
    vtoroy = mail_archive.proyti_yashchik(box_id, sverit=False)
    assert vtoroy["new"] == 7 and vtoroy["pending"] == 0
    assert [s.uid for s in _stroki(box_id)] == list(range(1, mail_archive.PISEM_ZA_KRUG + 8))
    # Новое письмо — догон с последнего UID, а не с начала.
    SERVER.put(address, "INBOX", pismo("Свежее"))
    assert mail_archive.proyti_yashchik(box_id, sverit=False)["new"] == 1
    assert len(_stroki(box_id)) == mail_archive.PISEM_ZA_KRUG + 8


def test_udalyonnoe_na_servere_ostayotsya_s_otmetkoy_a_flagi_obnovlyayutsya(root_client, server_id):
    box_id = _yashchik(server_id, "arh-del")
    address = f"arh-del@{DOMEN}"
    uids = [SERVER.put(address, "INBOX", pismo(f"Д{i}")) for i in range(3)]
    mail_archive.proyti_yashchik(box_id)
    inbox = SERVER.box(address)["INBOX"]["msgs"]
    del inbox[uids[0]]
    inbox[uids[1]]["flags"] = {"\\Seen", "\\Flagged"}

    itog = mail_archive.proyti_yashchik(box_id, sverit=True)
    assert itog["removed"] == 1
    po_uid = {s.uid: s for s in _stroki(box_id) if s.folder == "INBOX"}
    assert set(po_uid) == set(uids), "копия удалённого письма пропала из CRM"
    assert po_uid[uids[0]].removed_at is not None and po_uid[uids[1]].removed_at is None
    assert po_uid[uids[1]].flags == "\\Flagged \\Seen"
    assert root_client.get(f"{ARHIV}/{po_uid[uids[0]].id}/raw").status_code == 200
    udalyonnye = root_client.get(ARHIV, params={"box": box_id, "removed": True}).json()
    assert [i["id"] for i in udalyonnye["items"]] == [po_uid[uids[0]].id]

    # Папку убрали с сервера целиком — её письма тоже остаются, с отметкой.
    SERVER.put(address, "Проекты/Клиент", pismo("в папке"))
    mail_archive.proyti_yashchik(box_id, sverit=False)
    del SERVER.box(address)["Проекты/Клиент"]
    mail_archive.proyti_yashchik(box_id, sverit=False)
    v_papke = [s for s in _stroki(box_id) if s.folder == "Проекты/Клиент"]
    assert len(v_papke) == 1 and v_papke[0].removed_at is not None
    with SessionLocal() as db:
        assert mail_archive.repo.papka(db, box_id, "Проекты/Клиент") is None


def test_smena_uidvalidity_nachinaet_papku_zanovo(root_client, server_id):
    box_id = _yashchik(server_id, "arh-uv")
    address = f"arh-uv@{DOMEN}"
    SERVER.put(address, "INBOX", pismo("старое 1"))
    SERVER.put(address, "INBOX", pismo("старое 2"))
    mail_archive.proyti_yashchik(box_id)
    SERVER.pereso3dat(address, "INBOX")
    SERVER.put(address, "INBOX", pismo("новое 1"))

    assert mail_archive.proyti_yashchik(box_id)["new"] == 1
    inbox = [s for s in _stroki(box_id) if s.folder == "INBOX"]
    starye = [s for s in inbox if s.subject.startswith("старое")]
    novye = [s for s in inbox if s.subject.startswith("новое")]
    assert len(starye) == 2 and all(s.removed_at is not None for s in starye)
    assert len(novye) == 1 and novye[0].uid == 1 and novye[0].removed_at is None
    assert novye[0].uidvalidity != starye[0].uidvalidity
    assert novye[0].raw_path != starye[0].raw_path, "новое письмо затёрло файл старого с тем же UID"


# --- кто видит -------------------------------------------------------------------------

def test_lichnyy_yashchik_ne_viden_chuzhomu_a_udalyaet_tolko_upravlyayushchiy(
    root_client, server_id, role_maker, staff_maker  # noqa: F811
):
    rol = role_maker("Архив F3 — читатель", ["mail.view"])
    svoy = staff_maker("arh-f3-svoy@test.local", rol["id"])
    chuzhoy = staff_maker("arh-f3-chuzhoy@test.local", rol["id"])
    lichnyy = _yashchik(server_id, "arh-boss", shared=False, users=(_moy_id(svoy),))
    obshchiy = _yashchik(server_id, "arh-team")
    SERVER.put(f"arh-boss@{DOMEN}", "INBOX", pismo("Лично директору"))
    SERVER.put(f"arh-team@{DOMEN}", "INBOX", pismo("Всем"))
    mail_archive.proyti_yashchik(lichnyy)
    mail_archive.proyti_yashchik(obshchiy)
    lichnoe = _stroki(lichnyy)[0]

    assert chuzhoy.get(f"{ARHIV}/{lichnoe.id}").status_code == 404
    assert chuzhoy.get(f"{ARHIV}/{lichnoe.id}/raw").status_code == 404
    vidit = {i["account_id"] for i in chuzhoy.get(ARHIV).json()["items"]}
    assert lichnyy not in vidit and obshchiy in vidit
    assert lichnyy not in {s["account_id"] for s in chuzhoy.get(f"{ARHIV}/stats").json()}
    assert svoy.get(f"{ARHIV}/{lichnoe.id}").status_code == 200

    assert svoy.delete(f"{ARHIV}/{lichnoe.id}").status_code == 403
    put = get_settings().storage_dir / lichnoe.raw_path
    assert put.is_file()
    assert root_client.delete(f"{ARHIV}/{lichnoe.id}").status_code == 200
    assert not put.exists() and _stroki(lichnyy) == []


def test_token_s_oblastyu_vidit_tolko_svoy_yashchik(root_client, server_id):
    a = _yashchik(server_id, "arh-tok-a")
    b = _yashchik(server_id, "arh-tok-b")
    for box_id, imya in ((a, "arh-tok-a"), (b, "arh-tok-b")):
        SERVER.put(f"{imya}@{DOMEN}", "INBOX", pismo(f"для {imya}"))
        mail_archive.proyti_yashchik(box_id)
    tok = root_client.post(f"{API}/tokens", json={"name": "Архив F3", "mail_boxes": [a]})
    assert tok.status_code == 201, tok.text
    agent = TestClient(app, headers={"Authorization": f"Bearer {tok.json()['token']}"})
    assert {i["account_id"] for i in agent.get(ARHIV).json()["items"]} == {a}
    chuzhoe = _stroki(b)[0]
    assert agent.get(f"{ARHIV}/{chuzhoe.id}").status_code == 404
    assert [s["account_id"] for s in agent.get(f"{ARHIV}/stats").json()] == [a]


def test_kopiya_perezhivaet_udalenie_yashchika_iz_crm(root_client, server_id):
    box_id = _yashchik(server_id, "arh-gone")
    SERVER.put(f"arh-gone@{DOMEN}", "INBOX", pismo("Документ"))
    mail_archive.proyti_yashchik(box_id)
    with SessionLocal() as db:
        db.execute(delete(MailAccount).where(MailAccount.id == box_id))
        db.commit()
    with SessionLocal() as db:
        ostalos = list(db.scalars(select(MailArchiveMessage).where(MailArchiveMessage.address == f"arh-gone@{DOMEN}")))
        assert len(ostalos) == 1 and ostalos[0].account_id is None
        assert db.scalar(select(MailArchiveFolder).where(MailArchiveFolder.account_id == box_id)) is None
    assert root_client.get(f"{ARHIV}/{ostalos[0].id}/raw").status_code == 200


# --- что архивируется ------------------------------------------------------------------

def test_arhiv_po_umolchaniyu_u_svoego_servera_a_vneshniy_po_vyboru(root_client, server_id):
    svoy = _yashchik(server_id, "arh-own")
    with SessionLocal() as db:
        vneshniy = MailAccount(title="ext", address=f"arh-ext@{DOMEN}", imap_host="imap.remote.test")
        chernovik = MailServer(title="draft", mode=MODE_SSH, status=STATUS_DRAFT, hostname="mail.draft.test")
        db.add_all([vneshniy, chernovik])
        db.flush()
        na_chernovike = MailAccount(title="d", address=f"arh-draft@{DOMEN}", server_id=chernovik.id)
        db.add(na_chernovike)
        db.commit()
        ids = (vneshniy.id, na_chernovike.id, chernovik.id)
    try:
        with SessionLocal() as db:
            assert db.get(MailAccount, svoy).archive is True and db.get(MailAccount, ids[0]).archive is False
            berutsya = {b.id for b, _, _ in mail_archive.repo.yashchiki_dlya_arkhiva(db)}
        assert svoy in berutsya and ids[0] not in berutsya and ids[1] not in berutsya
        assert root_client.put(f"{ARHIV}/boxes/{ids[0]}", json={"archive": True}).json()["archive"] is True
        with SessionLocal() as db:
            assert ids[0] in {b.id for b, _, _ in mail_archive.repo.yashchiki_dlya_arkhiva(db)}
    finally:
        with SessionLocal() as db:
            db.execute(delete(MailServer).where(MailServer.id == ids[2]))
            db.commit()


def test_ochistit_kopiyu_yashchika(root_client, server_id):
    box_id = _yashchik(server_id, "arh-purge")
    SERVER.put(f"arh-purge@{DOMEN}", "INBOX", pismo("стереть"))
    mail_archive.proyti_yashchik(box_id)
    put = get_settings().storage_dir / _stroki(box_id)[0].raw_path
    otvet = root_client.delete(f"{ARHIV}/boxes/{box_id}")
    assert otvet.status_code == 200 and otvet.json()["deleted"] == 1
    assert _stroki(box_id) == [] and not put.exists()


# --- наблюдатель ------------------------------------------------------------------------

class FakeHost:
    def __init__(self) -> None:
        self.status: dict[str, dict] = {}

    def vyzvat(self, op: str, args: dict | None = None, *, timeout: float = 60) -> dict:
        if op == "status":
            return {"boxes": [{"address": a, "messages": 0, **st} for a, st in self.status.items()]}
        if op == "headers":
            return {"items": [{"uid": 5, "from": "c@remote.test", "subject": "s", "date": ""}]}
        raise mail_host.MailHostError("mail_agent_failed", op)


def test_nablyudatel_ne_zerkalit_lichnyy_yashchik_v_lentu_i_zovyot_arhiv(root_client, server_id, monkeypatch):
    """Разбор 10.10.2026: письмо директору через 15 с оказывалось в ленте клиента у всех менеджеров."""
    host = FakeHost()
    mail_host.set_host_factory(lambda server: host)
    lichnyy = _yashchik(server_id, "arh-w-boss", shared=False)
    obshchiy = _yashchik(server_id, "arh-w-team")
    zerkalo: list[int] = []
    arhiv: list[set] = []
    monkeypatch.setattr(mail_service, "sync_account", lambda db, box_id: zerkalo.append(box_id) or {})
    monkeypatch.setattr(mail_archive, "krug", lambda **kw: arhiv.append(set(kw.get("srochno") or ())) or {})
    monkeypatch.setattr(mail_watcher.push_service, "razoslat", lambda db, ochered, klient=None: 0)
    try:
        for imya in ("arh-w-boss", "arh-w-team"):
            host.status[f"{imya}@{DOMEN}"] = {"unseen": 0, "uidnext": 5, "uidvalidity": 9}
        mail_watcher.krug(sverka=False)
        for imya in ("arh-w-boss", "arh-w-team"):
            host.status[f"{imya}@{DOMEN}"] = {"unseen": 1, "uidnext": 6, "uidvalidity": 9}
        mail_watcher.krug(sverka=False)
    finally:
        mail_host.reset_host_factory()
    assert obshchiy in zerkalo
    assert lichnyy not in zerkalo, "письма личного ящика ушли в ленту клиента"
    assert {lichnyy, obshchiy} <= arhiv[-1], "ящики с новой почтой не попали в архив первыми"


def test_nerazbornoe_pismo_ne_derzhit_papku(root_client, server_id, monkeypatch):
    """Письмо, на котором падает разбор, ложится сырым — а не останавливает папку навсегда."""
    box_id = _yashchik(server_id, "arh-broken")
    address = f"arh-broken@{DOMEN}"
    plohoe = pismo("сломанное")
    SERVER.put(address, "INBOX", plohoe)
    SERVER.put(address, "INBOX", pismo("целое"))
    nastoyashchiy = mail_archive.webmail_service.razobrat_pismo

    def razbor(syroe: bytes):
        if syroe == plohoe:
            raise ValueError("кривая кодировка")
        return nastoyashchiy(syroe)

    monkeypatch.setattr(mail_archive.webmail_service, "razobrat_pismo", razbor)
    assert mail_archive.proyti_yashchik(box_id)["new"] == 2
    stroki = {s.uid: s for s in _stroki(box_id)}
    assert stroki[1].subject == "" and stroki[1].raw_path, "сырое неразборного письма потеряно"
    assert gzip.decompress((get_settings().storage_dir / stroki[1].raw_path).read_bytes()) == plohoe
    assert stroki[2].subject == "целое"
