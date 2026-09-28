"""Web Push: подписки браузеров, рассылка звонков, кнопки в системном уведомлении.

Разбор — `docs/bloki/31-web-push.md`. Звонок записывает `task_service.tick` в своей
транзакции, а в сеть он уходит ПОСЛЕ фиксации (`zvonki_service.shag`): ждать чужую
службу, держа замки базы, нельзя.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.services import povtor_service, push_shifr
from core.utils import now_utc
from database.models import PushSubscription, Task, User
from database.repositories import push as push_repo
from database.models.user import STATUS_ACTIVE
from database.repositories import users as users_repo

logger = logging.getLogger("opencrm.push")

#: Ключ `db.info`: звонки этой транзакции, которые уйдут в push после фиксации.
OCHERED = "opencrm_push_ochered"
#: Звонок, не доставленный за час, уже не звонок: служба его выбросит сама.
TTL = 3600
#: Кнопки уведомления живут неделю; дальше — открыть напоминание.
DEYSTVIE_DNEY = 7
OTLOZHIT_MINUT = 10

TEKSTY = {
    "ru": {"due": "Пора", "early": "Скоро: {vremya}", "nag": "Всё ещё ждёт", "snooze": "Отложенное — пора",
           "done": "Готово", "later": f"Через {OTLOZHIT_MINUT} минут"},
    "en": {"due": "Due now", "early": "Soon: {vremya}", "nag": "Still waiting", "snooze": "Snoozed — due now",
           "done": "Done", "later": f"In {OTLOZHIT_MINUT} minutes"},
}


@lru_cache(maxsize=4)
def _klyuch(sekret: str):
    return push_shifr.klyuch_iz_sekreta(sekret)


def klyuch():
    return _klyuch(get_settings().secret_key)


def otkrytyy_klyuch() -> str:
    return push_shifr.otkrytyy(klyuch())


def _kontakt() -> str:
    # Служба браузера пишет сюда, если рассылка ей мешает; http-адрес она не примет.
    s = get_settings()
    return s.base_url if s.base_url.startswith("https://") else f"mailto:{s.root_email}"


#: Службы браузеров, которым мы шлём. Любой иной https-адрес сотрудник мог бы
#: подставить сам — и сервер стучался бы POST'ом во внутреннюю сеть (SSRF).
SLUZHBY = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com")
SLUZHBY_POD = (".push.apple.com", ".notify.windows.com")


def sluzhba_brauzera(endpoint: str) -> bool:
    try:
        chast = urlsplit(endpoint)
        port = chast.port
    except ValueError:
        return False
    host = (chast.hostname or "").lower()
    if chast.scheme != "https" or port not in (None, 443) or chast.username or chast.password:
        return False
    return host in SLUZHBY or host.endswith(SLUZHBY_POD)


def _otpechatok(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


# --- подписки ------------------------------------------------------------------


def podpiska_out(p: PushSubscription) -> dict:
    return {
        "id": p.id,
        "nazvanie": p.nazvanie,
        "endpoint_hash": p.endpoint_hash,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "last_ok_at": p.last_ok_at.isoformat() if p.last_ok_at else None,
    }


def podpisat(db: Session, user: User, data: dict) -> PushSubscription:
    """Завести подписку этого браузера. Тот же браузер под другим входом — переезжает."""
    endpoint = (data.get("endpoint") or "").strip()
    klyuchi = data.get("keys") or {}
    if len(endpoint) > 1000 or not sluzhba_brauzera(endpoint):
        raise errors.ValidationError("Push endpoint must be a browser push service", code="push_endpoint")
    try:
        tochka = push_shifr.iz_b64u(klyuchi.get("p256dh") or "")
        auth = push_shifr.iz_b64u(klyuchi.get("auth") or "")
    except ValueError:
        tochka, auth = b"", b""
    if len(tochka) != 65 or tochka[0] != 4 or len(auth) != 16:
        raise errors.ValidationError("Push keys are malformed", code="push_keys")
    otpechatok = _otpechatok(endpoint)
    podpiska = push_repo.po_hashu(db, otpechatok)
    if podpiska is None:
        podpiska = push_repo.dobavit(db, PushSubscription(
            user_id=user.id, endpoint=endpoint, endpoint_hash=otpechatok,
            p256dh=klyuchi["p256dh"], auth=klyuchi["auth"],
            nazvanie=(data.get("nazvanie") or "")[:120],
        ))
    else:
        podpiska.user_id = user.id
        podpiska.p256dh, podpiska.auth = klyuchi["p256dh"], klyuchi["auth"]
        podpiska.nazvanie = (data.get("nazvanie") or podpiska.nazvanie)[:120]
        db.flush()
    return podpiska


def moi(db: Session, user: User) -> list[dict]:
    return [podpiska_out(p) for p in push_repo.dlya(db, [user.id])]


def otpisat(db: Session, user: User, podpiska_id: int) -> None:
    svoya = next((p for p in push_repo.dlya(db, [user.id]) if p.id == podpiska_id), None)
    if svoya is None:
        raise errors.NotFoundError("Subscription not found", code="push_subscription_not_found")
    push_repo.ubrat(db, svoya.id)


# --- подпись кнопок ------------------------------------------------------------


def _hmac(dannye: str) -> str:
    kod = hmac.new(get_settings().secret_key.encode("utf-8"), f"push-deystvie:{dannye}".encode(), hashlib.sha256)
    return push_shifr.b64u(kod.digest()[:18])


def srok_sek(srok: datetime | None) -> int:
    # Наивный UTC из базы: без явного пояса `timestamp()` прочёл бы его как местное.
    return int(srok.replace(tzinfo=timezone.utc).timestamp()) if srok else 0


def deystvie_podpisat(task_id: int, user_id: int, srok: datetime | None, teper: float | None = None) -> str:
    """Кнопки уведомления без сессии: service worker шлёт запрос без cookie и CSRF."""
    do = int((time.time() if teper is None else teper) + DEYSTVIE_DNEY * 86400)
    dannye = f"{task_id}.{user_id}.{srok_sek(srok)}.{do}"
    return f"{dannye}.{_hmac(dannye)}"


@dataclass
class Deystvie:
    task_id: int
    user_id: int
    srok: int


def deystvie_proverit(token: str) -> Deystvie:
    chasti = (token or "").split(".")
    if len(chasti) != 5 or not all(c.isdigit() for c in chasti[:4]):
        raise errors.AuthError("Bad action token", code="push_deystvie_ne_to")
    dannye = ".".join(chasti[:4])
    if not hmac.compare_digest(_hmac(dannye), chasti[4]):
        raise errors.AuthError("Bad action token", code="push_deystvie_ne_to")
    if int(chasti[3]) < time.time():
        raise errors.AuthError("Action token has expired", code="push_deystvie_istyok")
    return Deystvie(int(chasti[0]), int(chasti[1]), int(chasti[2]))


def otlozhit_do() -> datetime:
    return now_utc() + timedelta(minutes=OTLOZHIT_MINUT)


# --- рассылка ------------------------------------------------------------------


def v_ochered(db: Session, task: Task, user_id: int, vid: str) -> None:
    db.info.setdefault(OCHERED, []).append((task.id, task.title, task.vazhnost, task.due_at, user_id, vid, task.poyas))


def _soobshchenie(zvonok, locale: str) -> dict:
    task_id, title, vazhnost, srok, user_id, vid, poyas = zvonok
    tekst = TEKSTY.get(locale, TEKSTY["en"])
    vremya = povtor_service.mestnoe(srok, poyas).strftime("%H:%M") if srok else ""
    return {
        "title": title,
        "body": tekst.get(vid, tekst["due"]).format(vremya=vremya),
        "tag": f"opencrm-napom-{task_id}",
        "url": f"/tasks?open={task_id}",
        "srochno": vazhnost == "urgent",
        "deystvie": deystvie_podpisat(task_id, user_id, srok),
        "knopki": {"done": tekst["done"], "later": tekst["later"]},
    }


def razoslat(db: Session, ochered: list, klient: httpx.Client | None = None) -> int:
    """Отправить звонки из очереди во все браузеры адресатов. Возвращает, сколько доставлено."""
    if not ochered:
        return 0
    podpiski = push_repo.dlya(db, [z[4] for z in ochered])
    if not podpiski:
        return 0
    lyudi = {
        u.id: u for u in users_repo.get_many(db, {p.user_id for p in podpiski}) if u.status == STATUS_ACTIVE
    }
    svoy = klient is None
    klient = klient or httpx.Client(timeout=10)
    dostavleno = 0
    try:
        for zvonok in ochered:
            chelovek = lyudi.get(zvonok[4])
            if chelovek is None:
                continue
            telo = json.dumps(_soobshchenie(zvonok, chelovek.locale), ensure_ascii=False).encode()
            for p in podpiski:
                if p.user_id != chelovek.id:
                    continue
                dostavleno += _otpravit(db, klient, p, telo, zvonok[0], zvonok[2] == "urgent")
    finally:
        if svoy:
            klient.close()
    return dostavleno


def _otpravit(db: Session, klient: httpx.Client, p: PushSubscription, telo: bytes, task_id: int, srochno: bool) -> int:
    if not sluzhba_brauzera(p.endpoint):
        # Заведена до списка служб или вписана в базу мимо — в сеть не идёт.
        push_repo.ubrat(db, p.id)
        return 0
    try:
        otvet = klient.post(
            p.endpoint,
            content=push_shifr.zashifrovat(telo, p.p256dh, p.auth),
            headers={
                "Authorization": push_shifr.podpis_vapid(klyuch(), p.endpoint, _kontakt()),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL),
                "Urgency": "high" if srochno else "normal",
                # Новый звонок того же напоминания вытесняет недоставленный прежний.
                "Topic": f"napom{task_id}",
            },
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("push: %s — %r", p.nazvanie or p.id, exc)
        return 0
    if otvet.status_code in (404, 410):
        # Браузер отписался или подписка протухла — служба больше её не примет.
        push_repo.ubrat(db, p.id)
        return 0
    if otvet.is_success:
        push_repo.dostavleno(db, p.id, now_utc())
        return 1
    logger.warning("push: %s — %s %s", p.nazvanie or p.id, otvet.status_code, otvet.text[:200])
    return 0
