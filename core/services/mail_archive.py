"""Полная копия писем в базе CRM — «архив» (docs/bloki/32-pochtovyy-server.md, «Архив писем в CRM»).

Решение владельца от 10.10.2026: CRM держит у себя копию КАЖДОГО письма каждой
папки ящиков своего сервера (внешних — по флагу `archive`), и копия переживает
удаление письма на сервере и гибель самого VPS. Строка `mail_archive` — то, по
чему ищут и что показывают; само письмо с вложениями — `.eml.gz` в storage.

Наполняет наблюдатель (`mail_watcher.krug`) под своим замком Redis. Сеть — вне
транзакций: папка забирается по IMAP, пишется одной пачкой, коммит — и дальше.
Разбор и чистка HTML — те же, что у веб-почты (`webmail_service`).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.services import mail_access, storage_service, webmail_imap, webmail_service
from core.services.webmail_imap import WebmailError
from core.utils import now_utc
from database.models import MailAccount, MailArchiveFolder, MailArchiveMessage, User
from database.models.mail import message_id_key
from database.repositories import mail_archive as repo
from database.session import SessionLocal

logger = logging.getLogger("opencrm.mail")

#: Каталог в storage: попадает в ночные копии вместе со всем остальным.
KATALOG = "mail-archive"
#: Писем на ящик за круг: первый проход большого ящика идёт кусками по кругам.
PISEM_ZA_KRUG = 50
#: Сколько секунд круга наблюдателя отдано архиву: он делит круг с пушем о новом.
BYUDZHET_SEKUND = 10
#: Ящик без новостей проходится не чаще: вход по TLS на дальний VPS — это секунда.
PAUZA_SEKUND = 120
#: Сверка папки (флаги и удалённое на сервере) — не чаще раза в 10 минут.
SVERKA_SEKUND = 600
#: MEDIUMTEXT — 16 МБ байтов; знак utf8mb4 до четырёх. Целиком письмо — в сыром файле.
MAX_TELO_ZNAKOV = 4_000_000
MAX_ADRESA_ZNAKOV = 15_000
MAX_PER_PAGE = 200
#: Виртуальные папки (RFC 6154: `\\All`, `\\Flagged`) — те же письма второй раз.
_NE_BRAT = {"\\all", "\\flagged", "\\noselect", "\\nonexistent"}


# --- сырые письма в storage ---------------------------------------------------------

def put_fayla(account_id: int, uidvalidity: int, folder: str, uid: int) -> str:
    """Путь от корня storage. Папка — отпечатком: имя от сервера бывает каким угодно, с `../`."""
    otpechatok = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:16]
    return f"{KATALOG}/{int(account_id)}/{int(uidvalidity)}/{otpechatok}/{int(uid)}.eml.gz"


def _polnyy(otnositelnyy: str) -> Path | None:
    if not otnositelnyy:
        return None
    koren = (get_settings().storage_dir / KATALOG).resolve()
    put = (get_settings().storage_dir / otnositelnyy).resolve()
    return put if put.is_relative_to(koren) else None


def _zapisat_fayl(otnositelnyy: str, syroe: bytes) -> None:
    put = _polnyy(otnositelnyy)
    if put is None:
        raise ValueError(otnositelnyy)
    put.parent.mkdir(parents=True, exist_ok=True)
    # Через временный файл: оборванная запись не должна выглядеть целым письмом.
    vremennyy = put.with_name(f"{put.name}.{os.getpid()}.tmp")
    vremennyy.write_bytes(gzip.compress(syroe, mtime=0))
    os.replace(vremennyy, put)


def prochitat_fayl(otnositelnyy: str) -> bytes | None:
    put = _polnyy(otnositelnyy)
    if put is None or not put.is_file():
        return None
    return gzip.decompress(put.read_bytes())


def _ubrat_fayl(otnositelnyy: str) -> None:
    put = _polnyy(otnositelnyy)
    if put is not None:
        put.unlink(missing_ok=True)


# --- одно письмо ---------------------------------------------------------------------

def _flagi(flags) -> str:
    return " ".join(sorted(str(f) for f in flags))[:200]


def _adresa(spisok: list[dict]) -> str:
    return ", ".join(a["email"] for a in spisok if a.get("email"))[:MAX_ADRESA_ZNAKOV]


def _kopiya(
    account_id: int, address: str, folder: str, uidvalidity: int, uid: int,
    syroe: bytes, flags, received_at: datetime | None, size: int,
) -> dict:
    """Строка архива из сырого письма; файл `.eml.gz` ложится тут же."""
    try:
        r = webmail_service.razobrat_pismo(syroe)
        html = webmail_service.ochistit_html(r.html)[0] if r.html else ""
    except Exception:  # noqa: BLE001 — неразборное письмо вставало бы в каждом круге и держало папку
        logger.exception("mail archive: %s %s/%s cannot be parsed, kept raw", address, folder, uid)
        r, html = webmail_service.Razobrannoe(), ""
    put = put_fayla(account_id, uidvalidity, folder, uid)
    if storage_service.has_room_for(len(syroe)):
        _zapisat_fayl(put, syroe)
    else:
        # Места нет — строка всё равно ложится: тема, адреса и тела уже копия.
        logger.warning("mail archive: no disk room for %s %s/%s", address, folder, uid)
        put = ""
    return {
        "account_id": account_id,
        "address": address,
        "folder": folder,
        "uidvalidity": uidvalidity,
        "uid": uid,
        "message_id": message_id_key(r.message_id),
        "subject": r.subject[:500],
        "from_addr": (r.from_.get("email") or "")[:320],
        "to_addrs": _adresa(r.to),
        "cc_addrs": _adresa(r.cc),
        "sent_at": r.date,
        "received_at": received_at,
        "flags": _flagi(flags),
        "size": size or len(syroe),
        "has_attachments": any(not c.inline for c in r.chasti),
        "attachments": json.dumps(
            [
                {"part": c.part, "name": c.name, "size": len(c.data), "content_type": c.content_type,
                 "inline": c.inline}
                for c in r.chasti
            ],
            ensure_ascii=False,
        ),
        "body_text": r.text[:MAX_TELO_ZNAKOV],
        "body_html": html[:MAX_TELO_ZNAKOV],
        "raw_path": put,
        "raw_sha256": hashlib.sha256(syroe).hexdigest(),
    }


# --- проход по ящику -------------------------------------------------------------------

def _zapisat_papku(
    account_id: int, path: str, uidvalidity: int, uidnext: int, last_uid: int,
    kopii: list[dict], na_servere: dict[int, frozenset] | None,
) -> tuple[int, int]:
    """Пачка папки — одной транзакцией. (новых, отмечено удалёнными)."""
    seychas = now_utc()
    with SessionLocal() as db:
        papka = repo.papka(db, account_id, path)
        if papka is None:
            papka = MailArchiveFolder(account_id=account_id, folder=path, uidvalidity=uidvalidity,
                                      last_uid=0, uidnext=0)
            repo.dobavit(db, papka)
        elif papka.uidvalidity != uidvalidity:
            # Папку пересоздали: прежние номера ни на что не указывают, копии остаются удалёнными.
            repo.pometit_staroe_pokolenie(db, account_id, path, uidvalidity, seychas)
            papka.uidvalidity, papka.last_uid = uidvalidity, 0
        est = repo.est_uidy(db, account_id, path, uidvalidity, [k["uid"] for k in kopii])
        novyh = 0
        for kopiya in kopii:
            if kopiya["uid"] not in est:
                repo.dobavit(db, MailArchiveMessage(**kopiya))
                novyh += 1
        papka.last_uid = max(papka.last_uid or 0, last_uid)
        papka.uidnext = uidnext
        papka.synced_at = seychas
        udaleno = 0
        if na_servere is not None:
            ubrat, flagi = [], {}
            for row_id, uid, flags in repo.zhivye(db, account_id, path, uidvalidity, papka.last_uid):
                if uid not in na_servere:
                    ubrat.append(row_id)
                elif _flagi(na_servere[uid]) != flags:
                    flagi[row_id] = _flagi(na_servere[uid])
            repo.pometit_udalennymi(db, ubrat, seychas)
            repo.obnovit_flagi(db, flagi)
            papka.reconciled_at = seychas
            udaleno = len(ubrat)
        db.commit()
    return novyh, udaleno


def _poryadok(papka: webmail_imap.Papka) -> tuple:
    return (papka.path.upper() != webmail_service.INBOX, papka.path.casefold())


def proyti_yashchik(account_id: int, *, do: float | None = None, sverit: bool | None = None) -> dict:
    """Один ящик: новое во всех папках (до `PISEM_ZA_KRUG`), по сроку — сверка.

    `sverit=None` — по сроку каждой папки; True/False — принудительно (тесты, кнопка).
    """
    do = do if do is not None else time.monotonic() + BYUDZHET_SEKUND
    with SessionLocal() as db:
        box = repo.yashchik(db, account_id)
        if box is None or not box.archive:
            return {}
        n = webmail_service.nastroyki(db, box)
        address = box.address
        znaem = {p.folder: (p.uidvalidity, p.last_uid, p.reconciled_at) for p in repo.papki(db, account_id)}
    seychas = now_utc()
    itog = {"new": 0, "removed": 0, "folders": 0, "pending": 0}
    ostalos = PISEM_ZA_KRUG
    with webmail_imap.imap(n) as k:
        spisok = [p for p in k.papki() if not (_NE_BRAT & {f.lower() for f in p.flags})]
        for papka in sorted(spisok, key=_poryadok):
            if time.monotonic() > do:
                itog["pending"] += 1
                continue
            path = papka.path
            vsego = k.vybrat(path)
            uidvalidity, uidnext = k.sostoyanie()
            if not uidvalidity:
                logger.warning("mail archive: %s %s has no UIDVALIDITY", address, path)
                continue
            bylo = znaem.get(path)
            last_uid = bylo[1] if bylo and bylo[0] == uidvalidity else 0
            novye: list[int] = []
            if vsego and (not uidnext or uidnext > last_uid + 1):
                novye = k.uidy_ot(last_uid + 1)
            berem = novye[: max(0, ostalos)]
            opisi = k.opisi(berem)
            kopii, vzyato = [], []
            for uid in berem:
                if time.monotonic() > do:
                    break
                najdeno = k.syroe(uid)
                vzyato.append(uid)
                if najdeno is None:
                    continue  # стёрли между SEARCH и FETCH — брать нечего
                syroe, flags = najdeno
                _, received_at, size = opisi.get(uid, (flags, None, len(syroe)))
                kopii.append(_kopiya(account_id, address, path, uidvalidity, uid, syroe, flags,
                                     received_at, size))
            ostalos -= len(vzyato)
            novyy_last = max([last_uid, *vzyato])
            dochitano = len(vzyato) == len(novye)
            if dochitano and uidnext:
                # Хвост UID без писем (стёрли до нас) больше не спрашиваем.
                novyy_last = max(novyy_last, uidnext - 1)
            if not dochitano:
                itog["pending"] += 1
            pora = sverit if sverit is not None else (
                bylo is None or bylo[0] != uidvalidity or bylo[2] is None
                or (seychas - bylo[2]).total_seconds() >= SVERKA_SEKUND
            )
            na_servere = None
            if pora:
                # После забора тел: письмо, взятое в этот проход, обязано быть в снимке.
                na_servere = {u: f for u, (f, _, _) in k.opisi(None).items()} if vsego else {}
            try:
                novyh, udaleno = _zapisat_papku(account_id, path, uidvalidity, uidnext or 0, novyy_last,
                                                kopii, na_servere)
            except IntegrityError:
                # Второй процесс записал то же самое (Redis лёг, кругов два): возьмём в следующий раз.
                logger.warning("mail archive: %s %s raced with another writer", address, path)
                continue
            itog["new"] += novyh
            itog["removed"] += udaleno
            itog["folders"] += 1
        if spisok:
            with SessionLocal() as db:
                propali = repo.pometit_propavshie_papki(db, account_id, [p.path for p in spisok], now_utc())
                db.commit()
            itog["gone_folders"] = len(propali)
    return itog


def krug(*, srochno: set[int] | frozenset = frozenset(), byudzhet: float = BYUDZHET_SEKUND) -> dict:
    """Шаг круга наблюдателя: ящики с архивом, давно не проходившие и с новой почтой — первыми."""
    with SessionLocal() as db:
        spisok = repo.yashchiki_dlya_arkhiva(db)
    seychas = now_utc()
    do = time.monotonic() + byudzhet
    itog: dict = {}
    for box, synced, hvost in sorted(spisok, key=lambda x: x[0].id not in srochno):
        pora = (
            box.id in srochno or hvost or synced is None
            or (seychas - synced).total_seconds() >= PAUZA_SEKUND
        )
        if not pora:
            continue
        if time.monotonic() > do:
            break
        try:
            itog[box.id] = proyti_yashchik(box.id, do=do)
        except (errors.DomainError, WebmailError) as exc:
            logger.warning("mail archive: %s: %s", box.address, getattr(exc, "message", exc))
            itog[box.id] = {"error": getattr(exc, "code", "mail_imap_failed")}
        except Exception as exc:  # noqa: BLE001 — один ящик не останавливает остальных
            logger.warning("mail archive: %s failed: %r", box.address, exc)
            itog[box.id] = {"error": "failed"}
    return itog


# --- чтение для экрана и API -------------------------------------------------------------

def _vidimye(db: Session, user: User) -> set[int] | None:
    """None — все копии, включая ящики, убранные из CRM: их видит только управляющий без области токена."""
    if mail_access.upravlyaet(db, user) and mail_access.oblast_tokena(db) is None:
        return None
    return {b.id for b in mail_access.vidimye_yashchiki(db, user, s_neaktivnymi=True)}


def _utc(dt: datetime | None) -> datetime | None:
    """В базе время naive UTC: дата с поясом из запроса приводится к нему."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _out(s: MailArchiveMessage) -> dict:
    flags = {f.lower() for f in (s.flags or "").split()}
    return {
        "id": s.id,
        "account_id": s.account_id,
        "address": s.address,
        "folder": s.folder,
        "uid": s.uid,
        "message_id": s.message_id,
        "subject": s.subject,
        "from": s.from_addr,
        "to": [a for a in (s.to_addrs or "").split(", ") if a],
        "cc": [a for a in (s.cc_addrs or "").split(", ") if a],
        "sent_at": _iso(s.sent_at),
        "received_at": _iso(s.received_at),
        "seen": "\\seen" in flags,
        "flagged": "\\flagged" in flags,
        "flags": (s.flags or "").split(),
        "size": s.size,
        "has_attachments": s.has_attachments,
        "removed_at": _iso(s.removed_at),
        "has_raw": bool(s.raw_path),
    }


def spisok(
    db: Session, user: User, *, box: int | None = None, folder: str | None = None, q: str | None = None,
    from_: str | None = None, date_from: datetime | None = None, date_to: datetime | None = None,
    removed: bool | None = None, page: int = 1, per_page: int = 50,
) -> dict:
    page, per_page = max(1, page), max(1, min(per_page, MAX_PER_PAGE))
    date_from, date_to = _utc(date_from), _utc(date_to)
    stroki, vsego = repo.spisok(
        db, vidimye=_vidimye(db, user), box=box, folder=folder, q=(q or "").strip()[:200] or None,
        from_=(from_ or "").strip()[:200] or None, date_from=date_from, date_to=date_to, removed=removed,
        page=page, per_page=per_page,
    )
    return {"items": [_out(s) for s in stroki], "total": vsego, "page": page, "per_page": per_page}


def _dostupnoe(db: Session, user: User, archive_id: int) -> MailArchiveMessage:
    """Копия, если её ящик виден; иначе тот же 404, что у несуществующей."""
    stroka = repo.get(db, archive_id)
    vidimye = _vidimye(db, user)
    if stroka is None or (vidimye is not None and stroka.account_id not in vidimye):
        raise errors.NotFoundError("Archived message not found", code="mail_message_gone")
    return stroka


def pismo(db: Session, user: User, archive_id: int) -> dict:
    s = _dostupnoe(db, user, archive_id)
    try:
        vlozheniya = json.loads(s.attachments or "[]")
    except ValueError:
        vlozheniya = []
    return {**_out(s), "text": s.body_text, "html": s.body_html, "attachments": vlozheniya}


def syroe(db: Session, user: User, archive_id: int) -> bytes:
    s = _dostupnoe(db, user, archive_id)
    data = prochitat_fayl(s.raw_path)
    if data is None:
        raise errors.NotFoundError("The original of this message is not kept", code="mail_message_gone")
    return data


def vlozhenie(db: Session, user: User, archive_id: int, part: str) -> webmail_service.Chast:
    for c in webmail_service.razobrat_pismo(syroe(db, user, archive_id)).chasti:
        if c.part == part:
            return c
    raise errors.NotFoundError("Attachment not found", code="mail_message_gone")


def html_dokument(db: Session, user: User, archive_id: int, *, kartinki: bool = False) -> tuple[str, str]:
    """Страница копии для рамки — та же политика, что у веб-почты; картинки письма — из сырого файла."""
    s = _dostupnoe(db, user, archive_id)
    data = prochitat_fayl(s.raw_path)
    if data is not None:
        r = webmail_service.razobrat_pismo(data)
        chistyy = webmail_service.ochistit_html(r.html, webmail_service._cid_karta(r.chasti))[0] if r.html else ""
    else:
        chistyy = s.body_html or ""
    if kartinki:
        # Только https, как в веб-почте: картинка по http выдала бы адрес смотрящего.
        chistyy = re.sub(r' data-src="(https://[^"]*)"', r' src="\1"', chistyy)
    stranitsa = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer"><base target="_blank">'
        f"<style>{webmail_service.STIL_PISMA}</style></head><body>{chistyy}</body></html>"
    )
    return stranitsa, webmail_service.CSP_PISMA.format(https=" https:" if kartinki else "")


def svodka(db: Session, user: User) -> list[dict]:
    """По ящикам: писем, байт, удалено на сервере, когда проходили — без сети."""
    vidimye = _vidimye(db, user)
    yashchiki = {b.id: b for b in mail_access.vidimye_yashchiki(db, user, s_neaktivnymi=True)}
    stroki = {r[0]: r for r in repo.svodka(db, vidimye)}
    otmetki = repo.otmetki_papok(db, None if vidimye is None else set(vidimye))
    vyhod = []
    ids = set(stroki) | {i for i, b in yashchiki.items() if b.archive}
    for account_id in sorted(ids, key=lambda x: (x is None, x or 0)):
        box = yashchiki.get(account_id) if account_id is not None else None
        _, address, pisem, bayt, udaleno = stroki.get(account_id, (account_id, None, 0, 0, 0))
        synced, reconciled, nedochitano = otmetki.get(account_id, (None, None, 0))
        vyhod.append({
            "account_id": account_id,
            "address": box.address if box else address,
            "title": box.title if box else "",
            "archive": bool(box.archive) if box else False,
            "messages": int(pisem or 0),
            "bytes": int(bayt or 0),
            "removed": int(udaleno or 0),
            "synced_at": _iso(synced),
            "reconciled_at": _iso(reconciled),
            "pending_folders": int(nedochitano or 0),
        })
    return vyhod


# --- управление (mail.manage) ---------------------------------------------------------------

def udalit(db: Session, user: User, archive_id: int) -> None:
    s = _dostupnoe(db, user, archive_id)
    put = s.raw_path
    repo.udalit(db, s)
    db.flush()
    _ubrat_fayl(put)


def ochistit_yashchik(db: Session, account_id: int) -> int:
    """Стереть копию ящика целиком — строки и файлы. Зовёт удаление ящика с keep_data=0 по согласию."""
    puti = repo.puti_yashchika(db, account_id)
    udaleno = repo.udalit_yashchik(db, account_id)
    db.flush()
    for put in puti:
        _ubrat_fayl(put)
    return udaleno


def proverit_yashchik(db: Session, user: User, account_id: int) -> MailAccount:
    """Ящик (и выключенный тоже), если он виден; иначе 404, как несуществующий."""
    for box in mail_access.vidimye_yashchiki(db, user, s_neaktivnymi=True):
        if box.id == account_id:
            return box
    raise errors.NotFoundError("Mailbox not found", code="mail_account_not_found")


def vklyuchit(db: Session, user: User, account_id: int, archive: bool) -> dict:
    """Вести ли копию ящика. Выключение копию не стирает: она остаётся, пока её не удалят."""
    box = proverit_yashchik(db, user, account_id)
    if bool(box.archive) != archive:
        box.archive = archive
        db.flush()
    return {"account_id": box.id, "archive": bool(box.archive)}
