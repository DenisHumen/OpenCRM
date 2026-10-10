"""Наблюдатель новой почты своего сервера (docs/bloki/32-pochtovyy-server.md).

Поток-демон в каждом процессе, как звонки напоминаний (`zvonki_service`), а круг
делает один — тот, кто взял замок в Redis. Круг: `status` агента у каждого
готового сервера → счётчики ящиков в базе (живой намёк T_MAIL обновляет экраны)
→ заголовки новых писем → Web Push тем, кто видит ящик → зеркало для ленты
клиента (только общие ящики) → копия писем в архив CRM (`mail_archive`). Раз в
10 минут — сверка ящиков с сервером и его здоровье.

Новое письмо узнаётся по росту UIDNEXT при той же UIDVALIDITY: другой
UIDVALIDITY значит «ящик пересоздан», и старые номера сравнивать не с чем —
счёт начинается заново, без пуша о сотне «новых» писем.
"""

from __future__ import annotations

import logging
import secrets
import threading
from email.utils import parseaddr

from core import exceptions as errors
from core import redis_client
from core.services import mail_access, mail_archive, mail_server_service, mail_service, modules_service
from core.services import permissions_service, push_service
from core.utils import now_utc
from database.models import MailAccount, MailServer
from database.repositories import mail_server as repo
from database.session import SessionLocal

logger = logging.getLogger("opencrm.mail")

SHAG = 15
#: Круг один на все процессы. Замок с запасом на медленный сервер, а после круга
#: он держится до следующего шага — иначе соседний процесс начал бы свой сразу.
KLYUCH = f"{redis_client.PREFIX}mail:watch"
ZAMOK_SEKUND = 120
KLYUCH_SVERKI = f"{redis_client.PREFIX}mail:sverka"
SVERKA_SEKUND = 600
#: Сколько новых писем ящика разбирать за круг: сотня разом — это не «пришло
#: письмо», а перенос почты, и пуш о каждом был бы шумом.
NOVYH_ZA_RAZ = 10
#: Метки ошибок, которые ставит сам наблюдатель: снимает он только свои.
METKA_STATUSA = "status: "
METKA_ZDOROVYA = "health: "

_stop = threading.Event()
_potok: threading.Thread | None = None


# --- замки ---


def _vzyat_zamok() -> str | None:
    """Токен замка или None — круг делает другой процесс. Без Redis круг свой."""
    token = secrets.token_hex(8)
    try:
        klient = redis_client.get_client()
        if klient is None:
            return token
        return token if klient.set(KLYUCH, token, nx=True, ex=ZAMOK_SEKUND) else None
    except Exception:  # noqa: BLE001 — Redis лёг: лучше двойной круг, чем ни одного
        return token


def _otpustit(token: str) -> None:
    try:
        klient = redis_client.get_client()
        if klient is None:
            return
        svoy = klient.get(KLYUCH)
        # не-секрет: метка круга отличает наш замок от чужого, ни во что не впускает.
        if (svoy.decode() if isinstance(svoy, bytes) else svoy) == token:
            klient.set(KLYUCH, token, ex=max(1, SHAG - 2))
    except Exception:  # noqa: BLE001 — замок истечёт сам
        pass


def _pora_sverit() -> bool:
    try:
        klient = redis_client.get_client()
        if klient is not None:
            return bool(klient.set(KLYUCH_SVERKI, "1", nx=True, ex=SVERKA_SEKUND))
    except Exception:  # noqa: BLE001
        pass
    return False


# --- круг ---


def _oshibka(server: MailServer, metka: str, tekst: str | None) -> None:
    """Своя ошибка сервера: ставим и снимаем только с нашей меткой, чужую не трогаем."""
    novaya = f"{metka}{tekst}" if tekst else None
    if tekst is None and not (server.last_error or "").startswith(metka):
        return
    if server.last_error != novaya:
        server.last_error = novaya


def _kto_vidit(db) -> dict[int, set[int]]:
    """Сотрудник → видимые ящики. Считается один раз за круг и только при новой почте."""
    itog: dict[int, set[int]] = {}
    for chelovek in mail_server_service.aktivnye_lyudi(db):
        if permissions_service.has(db, chelovek, "mail", "view"):
            itog[chelovek.id] = mail_access.vidimye_ids(db, chelovek)
    return itog


def _soobshchenie(user_id: int, box: MailAccount, pisma: list[dict]) -> push_service.Soobshchenie:
    poslednee = pisma[-1]
    imya, adres = parseaddr(poslednee.get("from") or "")
    tema = (poslednee.get("subject") or "").strip()
    eshchyo = f" (+{len(pisma) - 1})" if len(pisma) > 1 else ""
    return push_service.Soobshchenie(
        user_id=user_id,
        title=(imya or adres or box.address)[:120],
        body=(tema[:200] + eshchyo) if tema else f"{box.address}{eshchyo}",
        tag=f"opencrm-mail-{box.id}",
        url=f"/mail?box={box.id}&uid={poslednee.get('uid')}",
        topic=f"pochta{box.id}",
    )


def obnovit_server(server_id: int) -> dict:
    """Один сервер: счётчики, пуш о новом, зеркало. Своя сессия — сбой не задевает соседей."""
    with SessionLocal() as db:
        server = repo.get_server(db, server_id)
        if server is None:
            return {}
        try:
            data = mail_server_service.vyzvat(server, "status", timeout=30)
        except errors.DomainError as exc:
            _oshibka(server, METKA_STATUSA, f"{exc.code}: {exc.message}")
            db.commit()
            return {"error": exc.code}
        _oshibka(server, METKA_STATUSA, None)

        yashchiki = {b.address.lower(): b for b in repo.yashchiki_servera(db, server.id) if b.is_active}
        novye: list[tuple[MailAccount, int]] = []
        for stroka in data.get("boxes") or []:
            box = yashchiki.get(str(stroka.get("address") or "").lower())
            if box is None:
                continue
            try:
                unseen = int(stroka.get("unseen") or 0)
                uidnext = int(stroka.get("uidnext") or 0)
                uidvalidity = int(stroka.get("uidvalidity") or 0)
            except (TypeError, ValueError):
                continue
            if (
                box.inbox_uidvalidity == uidvalidity
                and box.inbox_uidnext is not None
                and uidnext > box.inbox_uidnext
            ):
                novye.append((box, box.inbox_uidnext))
            # Пишем только перемену: грязный ящик — живой намёк всем, кто смотрит почту.
            if (box.unseen_count, box.inbox_uidnext, box.inbox_uidvalidity) != (unseen, uidnext, uidvalidity):
                box.unseen_count, box.inbox_uidnext, box.inbox_uidvalidity = unseen, uidnext, uidvalidity
        repo.otmetit_proverku(db, [b.id for b in yashchiki.values()], now_utc())

        kto_vidit = _kto_vidit(db) if novye else {}
        for box, bylo in novye:
            try:
                pisma = mail_server_service.vyzvat(
                    server, "headers", {"address": box.address, "since_uid": bylo - 1, "limit": NOVYH_ZA_RAZ}
                ).get("items") or []
            except errors.DomainError as exc:
                logger.warning("mail watcher: headers of %s failed: %s", box.address, exc.message)
                continue
            if not pisma:
                continue
            for user_id, vidit in sorted(kto_vidit.items()):
                if box.id in vidit:
                    push_service.v_ochered_soobshchenie(db, _soobshchenie(user_id, box, pisma))

        novye_ids = [box.id for box, _ in novye]
        # Лента клиента видна всем, кто видит клиента: личный ящик туда не зеркалим,
        # иначе письмо директору читал бы любой менеджер (разбор 10.10.2026).
        zerkalo = [box.id for box, _ in novye if box.is_shared]
        ochered = db.info.pop(push_service.OCHERED, [])
        db.commit()
        # В сеть — после фиксации: чужая служба не должна держать замки нашей базы.
        if ochered:
            push_service.razoslat(db, ochered)
            db.commit()

        for box_id in zerkalo:
            try:
                mail_service.sync_account(db, box_id)
                db.commit()
            except Exception as exc:  # noqa: BLE001 — лента клиента подождёт следующего круга
                db.rollback()
                logger.warning("mail watcher: mirror of mailbox %s failed: %r", box_id, exc)
        return {"new": novye_ids, "pushed": len(ochered)}


def sverit_server(server_id: int) -> dict:
    """Ящики и псевдонимы — с сервером, здоровье — в `last_error`."""
    with SessionLocal() as db:
        server = repo.get_server(db, server_id)
        if server is None:
            return {}
        itog: dict = {}
        try:
            zdorovye = mail_server_service.vyzvat(server, "health", timeout=60)
            sluzhby = zdorovye.get("services") or {}
            lezhat = sorted(k for k, v in sluzhby.items() if not v)
            _oshibka(server, METKA_ZDOROVYA, f"services down: {', '.join(lezhat)}" if lezhat else None)
        except errors.DomainError as exc:
            _oshibka(server, METKA_ZDOROVYA, f"{exc.code}: {exc.message}")
        try:
            itog = mail_server_service.sverit(db, server)
        except errors.DomainError as exc:
            logger.warning("mail watcher: reconcile of %s failed: %s", server.hostname, exc.message)
        db.commit()
        return itog


def krug(*, sverka: bool | None = None) -> dict:
    """Один круг по всем готовым серверам. Тесты зовут его напрямую, без потока и замка."""
    with SessionLocal() as db:
        if not modules_service.is_enabled(db, "mail"):
            return {}
        ids = [s.id for s in repo.gotovye_servery(db)]
    itog = {}
    for server_id in ids:
        try:
            itog[server_id] = obnovit_server(server_id)
        except Exception as exc:  # noqa: BLE001 — один сервер не останавливает остальных
            logger.warning("mail watcher: round of server %s failed: %r", server_id, exc)
    # Ящики с новой почтой — в архив первыми; внешние ящики с архивом — тоже здесь.
    srochno = {box_id for chast in itog.values() for box_id in (chast or {}).get("new") or []}
    try:
        mail_archive.krug(srochno=srochno)
    except Exception as exc:  # noqa: BLE001 — архив подождёт следующего круга
        logger.warning("mail watcher: archive step failed: %r", exc)
    if ids and (sverka if sverka is not None else _pora_sverit()):
        for server_id in ids:
            try:
                sverit_server(server_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mail watcher: reconcile of server %s failed: %r", server_id, exc)
    return itog


def _krug_v_potoke() -> None:
    while not _stop.wait(SHAG):
        token = _vzyat_zamok()
        if token is None:
            continue
        try:
            krug()
        except Exception as exc:  # noqa: BLE001 — поток обязан пережить сбой базы
            print(f"[opencrm] mail watcher: round failed: {exc!r}")
        finally:
            _otpustit(token)


def zapustit() -> None:
    global _potok
    if _potok is not None and _potok.is_alive():
        return
    _stop.clear()
    _potok = threading.Thread(target=_krug_v_potoke, daemon=True, name="mail-watcher")
    _potok.start()


def ostanovit() -> None:
    _stop.set()
