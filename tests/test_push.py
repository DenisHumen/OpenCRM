"""Web Push (docs/bloki/31-web-push.md): подписки, рассылка, кнопки уведомления, связь с планировщиком."""

import json
import struct
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from core.services import push_service, push_shifr, zvonki_service
from database.models import PushSubscription, Task
from database.session import SessionLocal
from tests.conftest import API
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры
from web.main import app

PUSH = f"{API}/push"


class Brauzer:
    """Браузер с парой ключей: подписывается и расшифровывает, как настоящий."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.klyuch = ec.generate_private_key(push_shifr.KRIVAYA)
        self.auth = b"0123456789abcdef"

    def podpiska(self) -> dict:
        tochka = self.klyuch.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return {"endpoint": self.endpoint, "keys": {"p256dh": push_shifr.b64u(tochka), "auth": push_shifr.b64u(self.auth)}, "nazvanie": "Chrome · Windows"}

    def prochest(self, telo: bytes) -> dict:
        sol, dlina = telo[:16], telo[20]
        as_tochka = telo[21 : 21 + dlina]
        ua_tochka = self.klyuch.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        obshchiy = self.klyuch.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(push_shifr.KRIVAYA, as_tochka))
        hkdf = lambda salt, ikm, info, n: HKDF(hashes.SHA256(), n, salt, info).derive(ikm)  # noqa: E731
        ikm = hkdf(self.auth, obshchiy, b"WebPush: info\x00" + ua_tochka + as_tochka, 32)
        cek = hkdf(sol, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(sol, ikm, b"Content-Encoding: nonce\x00", 12)
        assert struct.unpack("!I", telo[16:20])[0] == push_shifr.ZAPIS
        tekst = AESGCM(cek).decrypt(nonce, telo[21 + dlina :], None)
        assert tekst.endswith(b"\x02")
        return json.loads(tekst[:-1])


def _cherez(**sdvig) -> str:
    return (datetime.now(timezone.utc) + timedelta(**sdvig)).isoformat()


def _napominanie(client, **pola) -> dict:
    otvet = client.post(f"{API}/tasks", json={"title": "Позвонить поставщику", **pola})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def _moy_id(client) -> int:
    return client.get(f"{API}/auth/me").json()["id"]


def test_podpiska_proveryaet_adres_i_klyuchi(root_client):
    b = Brauzer("https://fcm.googleapis.com/fcm/send/p1").podpiska()
    net_https = root_client.post(f"{PUSH}/subscriptions", json={**b, "endpoint": "http://fcm.googleapis.com/fcm/send/p1"})
    assert net_https.json()["error"]["code"] == "push_endpoint"
    ne_klyuch = root_client.post(f"{PUSH}/subscriptions", json={**b, "keys": {"p256dh": "AAAA", "auth": "AAAA"}})
    assert ne_klyuch.json()["error"]["code"] == "push_keys"
    assert root_client.get(f"{PUSH}/key").json()["key"] == push_service.otkrytyy_klyuch()


@pytest.mark.parametrize(
    "adres",
    [
        "https://127.0.0.1/p",
        "https://localhost/p",
        "https://10.0.0.130/api/v1/system/backups/restore",
        "https://crm.example.com/p",
        "https://fcm.googleapis.com.evil.test/p",
        "https://fcm.googleapis.com@evil.test/p",
        "https://fcm.googleapis.com:8443/p",
    ],
)
def test_podpiska_tolko_na_sluzhbu_brauzera(root_client, adres):
    """SSRF: адрес подписки задаёт сотрудник, а POST на него шлёт сервер."""
    b = Brauzer(adres).podpiska()
    otvet = root_client.post(f"{PUSH}/subscriptions", json=b)
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "push_endpoint"


def test_podpiski_vsekh_brauzerov_prinimayutsya():
    for adres in (
        "https://fcm.googleapis.com/fcm/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://web.push.apple.com/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
    ):
        assert push_service.sluzhba_brauzera(adres), adres


def test_podpiska_svoya_i_pereezzhaet_s_brauzerom(root_client, role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Push — напоминания", ["tasks.view", "tasks.create", "tasks.edit"])
    anna = staff_maker("push-anna@test.local", rol["id"])
    b = Brauzer("https://updates.push.services.mozilla.com/wpush/v2/pereezd").podpiska()
    moya = root_client.post(f"{PUSH}/subscriptions", json=b)
    assert moya.status_code == 201, moya.text
    assert anna.delete(f"{PUSH}/subscriptions/{moya.json()['id']}").json()["error"]["code"] == "push_subscription_not_found"
    # Тот же браузер, вход другого сотрудника: звонки теперь ему, прежнему — нет.
    assert anna.post(f"{PUSH}/subscriptions", json=b).json()["id"] == moya.json()["id"]
    assert all(p["id"] != moya.json()["id"] for p in root_client.get(f"{PUSH}/subscriptions").json()["items"])
    assert anna.delete(f"{PUSH}/subscriptions/{moya.json()['id']}").status_code == 200
    assert anna.get(f"{PUSH}/subscriptions").json()["items"] == []


def test_rassylka_shifruet_podpisyvaet_i_ubiraet_mertvye(root_client):
    zhivoy = Brauzer("https://fcm.googleapis.com/fcm/send/zhivoy")
    mertvyy = Brauzer("https://web.push.apple.com/mertvyy")
    for b in (zhivoy, mertvyy):
        assert root_client.post(f"{PUSH}/subscriptions", json=b.podpiska()).status_code == 201
    task = _napominanie(root_client, due_at=_cherez(minutes=30), vazhnost="urgent")
    zaprosy: list[httpx.Request] = []

    def sluzhba(zapros: httpx.Request) -> httpx.Response:
        zaprosy.append(zapros)
        return httpx.Response(410 if "mertvyy" in str(zapros.url) else 201)

    with SessionLocal() as db:
        t = db.get(Task, task["id"])
        ochered = [(t.id, t.title, t.vazhnost, t.due_at, _moy_id(root_client), "early", t.poyas)]
        with httpx.Client(transport=httpx.MockTransport(sluzhba)) as klient:
            assert push_service.razoslat(db, ochered, klient) == 1
        db.commit()

    k_zhivomu = next(z for z in zaprosy if "zhivoy" in str(z.url))
    assert k_zhivomu.headers["Content-Encoding"] == "aes128gcm"
    assert k_zhivomu.headers["Urgency"] == "high" and k_zhivomu.headers["TTL"] == str(push_service.TTL)
    assert k_zhivomu.headers["Authorization"].startswith("vapid t=")
    soobshchenie = zhivoy.prochest(k_zhivomu.content)
    assert soobshchenie["title"] == "Позвонить поставщику"
    assert soobshchenie["tag"] == f"opencrm-napom-{task['id']}" and soobshchenie["srochno"] is True
    assert push_service.deystvie_proverit(soobshchenie["deystvie"]).task_id == task["id"]

    with SessionLocal() as db:
        adresa = {p.endpoint: p for p in db.query(PushSubscription).all()}
    assert mertvyy.endpoint not in adresa, "служба ответила 410 — подписку надо убрать"
    assert adresa[zhivoy.endpoint].last_ok_at is not None


def test_rassylka_ne_pishet_v_bazu_mezhdu_zaprosami(root_client):
    """Разбор 28.09.2026: отметка доставки посреди рассылки держала замок строки подписки
    через таймауты всех следующих служб. Запись — только после сети."""
    from sqlalchemy import event

    from database.session import engine

    mertvyy = Brauzer("https://web.push.apple.com/zamok-mertvyy")
    zhivoy = Brauzer("https://fcm.googleapis.com/fcm/send/zamok-zhivoy")
    for b in (mertvyy, zhivoy):
        assert root_client.post(f"{PUSH}/subscriptions", json=b.podpiska()).status_code == 201
    task = _napominanie(root_client, due_at=_cherez(minutes=30))
    zapisi: list[str] = []
    zaprosy: list[str] = []

    def slushat(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(("UPDATE", "DELETE")) and "push_subscriptions" in statement:
            zapisi.append(statement)

    def sluzhba(zapros: httpx.Request) -> httpx.Response:
        assert zapisi == [], "запись в подписки раньше, чем кончилась сеть"
        zaprosy.append(str(zapros.url))
        return httpx.Response(410 if "mertvyy" in str(zapros.url) else 201)

    event.listen(engine, "before_cursor_execute", slushat)
    try:
        with SessionLocal() as db:
            t = db.get(Task, task["id"])
            zvonok = (t.id, t.title, t.vazhnost, t.due_at, _moy_id(root_client), "due", t.poyas)
            with httpx.Client(transport=httpx.MockTransport(sluzhba)) as klient:
                push_service.razoslat(db, [zvonok, zvonok], klient)
            db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", slushat)
    assert zapisi, "мёртвая подписка должна уйти, живая — получить отметку"
    assert sum("zamok-mertvyy" in z for z in zaprosy) == 1, "мёртвой подписке второй звонок не шлют"
    assert sum("zamok-zhivoy" in z for z in zaprosy) == 2
    with SessionLocal() as db:
        adresa = {p.endpoint: p for p in db.query(PushSubscription).all()}
    assert mertvyy.endpoint not in adresa and adresa[zhivoy.endpoint].last_ok_at is not None


def test_chuzhoy_adres_v_baze_v_set_ne_idyot(root_client):
    """Строка, вписанная мимо проверки (или до неё), не превращается в запрос сервера."""
    b = Brauzer("https://fcm.googleapis.com/fcm/send/podmena")
    otvet = root_client.post(f"{PUSH}/subscriptions", json=b.podpiska())
    with SessionLocal() as db:
        db.get(PushSubscription, otvet.json()["id"]).endpoint = "https://127.0.0.1:8000/api/v1/x"
        db.commit()
    task = _napominanie(root_client, due_at=_cherez(minutes=30))
    zaprosy: list[httpx.Request] = []
    with SessionLocal() as db:
        t = db.get(Task, task["id"])
        ochered = [(t.id, t.title, t.vazhnost, t.due_at, _moy_id(root_client), "due", t.poyas)]
        with httpx.Client(transport=httpx.MockTransport(lambda z: zaprosy.append(z) or httpx.Response(201))) as k:
            push_service.razoslat(db, ochered, k)
        db.commit()
    assert not any("127.0.0.1" in str(z.url) for z in zaprosy)
    with SessionLocal() as db:
        assert db.get(PushSubscription, otvet.json()["id"]) is None


def test_shag_planirovshchika_shlyot_push_posle_fiksatsii(root_client, monkeypatch):
    task = _napominanie(root_client, due_at=_cherez(seconds=-30))
    poslano: list = []
    monkeypatch.setattr(push_service, "razoslat", lambda db, ochered, klient=None: poslano.extend(ochered) or 0)
    zvonki_service.shag()
    assert any(z[0] == task["id"] and z[5] == "due" for z in poslano)


def _deystvie(token: str, deystvie: str):
    # Как service worker: ни cookie, ни CSRF.
    return TestClient(app).post(f"{PUSH}/action", json={"token": token, "deystvie": deystvie})


def test_knopka_gotovo_zakryvaet_tolko_svoy_raz(root_client):
    """У повторяющегося закрытый раз двигает срок: вторым нажатием закрылся бы завтрашний."""
    task = _napominanie(root_client, due_at=_cherez(minutes=5), povtor="FREQ=DAILY")
    with SessionLocal() as db:
        srok = db.get(Task, task["id"]).due_at
    token = push_service.deystvie_podpisat(task["id"], _moy_id(root_client), srok)
    assert _deystvie(token, "done").status_code == 200
    povtor = _deystvie(token, "done")
    assert povtor.status_code == 409 and povtor.json()["error"]["code"] == "zvonok_ustarel"
    with SessionLocal() as db:
        assert db.get(Task, task["id"]).sdelano_raz == 1


def test_knopka_otlozhit(root_client):
    task = _napominanie(root_client, due_at=_cherez(minutes=5))
    with SessionLocal() as db:
        srok = db.get(Task, task["id"]).due_at
    otvet = _deystvie(push_service.deystvie_podpisat(task["id"], _moy_id(root_client), srok), "later")
    assert otvet.status_code == 200 and otvet.json()["deystvie"] == "later"
    karta = root_client.get(f"{API}/tasks/{task['id']}").json()
    assert karta["done_at"] is None


@pytest.mark.parametrize("porcha", ["podpis", "srok_istyok", "chuzhoy_nomer"])
def test_poddelnaya_ili_istyokshaya_knopka_ne_rabotaet(root_client, porcha):
    task = _napominanie(root_client, due_at=_cherez(minutes=5))
    with SessionLocal() as db:
        srok = db.get(Task, task["id"]).due_at
    token = push_service.deystvie_podpisat(task["id"], _moy_id(root_client), srok)
    if porcha == "podpis":
        token = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    elif porcha == "srok_istyok":
        token = push_service.deystvie_podpisat(task["id"], _moy_id(root_client), srok, teper=0)
    else:
        chasti = token.split(".")
        token = ".".join([str(task["id"] + 1)] + chasti[1:])
    otvet = _deystvie(token, "done")
    assert otvet.status_code == 401, otvet.text
    assert root_client.get(f"{API}/tasks/{task['id']}").json()["done_at"] is None


def test_otklyuchenie_ubiraet_ustroystva_i_rassylka_ih_ne_zhdyot(root_client, role_maker, staff_maker):  # noqa: F811
    """Уволенный не получает push ни через свои подписки, ни через оставшиеся с прошлых увольнений."""
    rol = role_maker("Push — уволенный", ["tasks.view", "tasks.edit"])
    anna = staff_maker("push-uvolen@test.local", rol["id"])
    anna_id = _moy_id(anna)
    b = Brauzer("https://fcm.googleapis.com/fcm/send/uvolen")
    podpiska = anna.post(f"{PUSH}/subscriptions", json=b.podpiska()).json()
    task = _napominanie(root_client, due_at=_cherez(minutes=30))

    assert root_client.post(f"{API}/staff/{anna_id}/disable").status_code == 200
    with SessionLocal() as db:
        assert db.get(PushSubscription, podpiska["id"]) is None, "отключение оставило устройство"
        # Подписка, оставшаяся с увольнения до правки: рассылка обязана её пропустить.
        db.add(PushSubscription(user_id=anna_id, endpoint=b.endpoint, endpoint_hash="x" * 64,
                                p256dh=b.podpiska()["keys"]["p256dh"], auth=b.podpiska()["keys"]["auth"]))
        db.commit()
    zaprosy: list[httpx.Request] = []
    with SessionLocal() as db:
        t = db.get(Task, task["id"])
        ochered = [(t.id, t.title, t.vazhnost, t.due_at, anna_id, "due", t.poyas)]
        with httpx.Client(transport=httpx.MockTransport(lambda z: zaprosy.append(z) or httpx.Response(201))) as k:
            assert push_service.razoslat(db, ochered, k) == 0
        db.commit()
    assert zaprosy == []
