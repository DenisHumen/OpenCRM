"""Веб-почта: папки, письма, вложения, отправка из ящика (docs/bloki/32-pochtovyy-server.md).

Письма живут на почтовом сервере, а не в базе: экран спрашивает сервер по IMAP
каждый раз и показывает то же, что увидел бы телефон. Своё в базе только одно —
ушедшее письмо, как и раньше, ложится в зеркало `mail_messages` и, у общего
ящика, в ленту клиента (`mail_service.zapisat_iskhodyashchee`): переписка с
клиентом не заводит второй истории.

Кто какой ящик видит — `mail_access`; невидимый ящик отвечает 404, как
несуществующий.
"""

from __future__ import annotations

import base64
import email
import email.policy
import html as html_lib
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path, PurePosixPath, PureWindowsPath

import nh3
from sqlalchemy.orm import Session

from config.settings import BASE_DIR
from core import exceptions as errors
from core.security import secretbox
from core.services import mail_access, mail_server_service, mail_service, webmail_imap
from core.services.webmail_imap import Nastroyki, Papka, Poisk, Svodka, WebmailError
from core.utils import now_utc
from database.models import MailAccount, User
from database.models.mail_server import STATUS_DRAFT, STATUS_INSTALLING
from database.repositories import mail as mail_repo
from database.repositories import webmail as webmail_repo

logger = logging.getLogger("opencrm.mail")

#: Вложение — не больше 25 МБ, письмо целиком — 35 МБ: чаще не примет сервер адресата.
MAX_VLOZHENIE_BAYT = 25 * 1024 * 1024
MAX_PISMO_BAYT = 35 * 1024 * 1024
#: Картинка из тела письма (cid:) встраивается в страницу, только если невелика.
MAX_CID_BAYT = 2 * 1024 * 1024
#: Загрузка, к которой так и не написали письмо, живёт сутки.
ZAGRUZKA_ZHIVYOT_SEKUND = 24 * 3600
#: Несданные загрузки одного сотрудника: без потолка диск `data/` забивался
#: файлами по 25 МБ, живущими сутки (разбор 10.10.2026).
KVOTA_ZAGRUZOK_BAYT = 200 * 1024 * 1024
#: Предупреждение в ответе: исходного письма ответа или пересылки уже нет.
ISKHODNOGO_NET = "mail_original_gone"
MAX_PER_PAGE = 200
#: Сколько писем можно отметить или переложить одним запросом.
MAX_UIDS = 1000
MAX_IMYA_PAPKI = 100

INBOX = "INBOX"
#: Служебные папки по SPECIAL-USE (RFC 6154); `\\All` у Dovecot — виртуальная, не показываем особой.
_SPECIAL = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\archive": "archive",
}
#: Сервер без SPECIAL-USE: узнаём служебные по привычным именам.
_PO_IMENI = {
    "sent": "sent", "sent messages": "sent", "sent items": "sent", "sent mail": "sent",
    "drafts": "drafts", "draft": "drafts",
    "trash": "trash", "deleted items": "trash", "deleted messages": "trash", "bin": "trash",
    "junk": "junk", "spam": "junk", "junk e-mail": "junk",
    "archive": "archive", "archives": "archive",
}
_PORYADOK = {"inbox": 0, "drafts": 1, "sent": 2, "archive": 3, "junk": 4, "trash": 5}
_ID_ZAGRUZKI = re.compile(r"^[0-9a-f]{32}$")
_TIP = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


# --- куда входить -----------------------------------------------------------

def nastroyki(db: Session, box: MailAccount) -> Nastroyki:
    """Ящик своего сервера — мастером по имени сервера; внешний — своими полями."""
    if box.server_id is not None:
        server = webmail_repo.server(db, box.server_id)
        if (
            server is None
            or server.status in (STATUS_DRAFT, STATUS_INSTALLING)
            or not server.master_password_encrypted
            or not (server.imap_host or server.hostname)
        ):
            # Тот же ответ, что у управления ящиками (`mail_server_service`): 409, «подождите».
            raise errors.ConflictError("Mail server is not ready", code="mail_server_not_ready")
        try:
            parol = secretbox.decrypt(
                server.master_password_encrypted, mail_server_service.MASTER_PURPOSE
            )
        except secretbox.SecretBoxError as exc:
            raise errors.ConflictError(
                "Mail server master password cannot be decrypted with the current OPENCRM_SECRET_KEY",
                code="mail_server_not_ready",
            ) from exc
        host = server.imap_host or server.hostname
        return Nastroyki(
            address=box.address,
            imap_host=host,
            imap_port=server.imap_port,
            smtp_host=host,
            smtp_port=server.smtp_port,
            login=f"{box.address}*{server.master_user}",
            password=parol,
            tls_name=server.hostname,
            tls_pin=server.tls_pin,
        )
    return Nastroyki(
        address=box.address,
        imap_host=box.imap_host,
        imap_port=box.imap_port,
        smtp_host=box.smtp_host,
        smtp_port=box.smtp_port,
        login=box.login or box.address,
        password=mail_service.account_password(box),
        imap_ssl=box.imap_ssl,
        smtp_ssl=box.smtp_ssl,
    )


def _otkaz(exc: WebmailError) -> errors.DomainError:
    logger.warning("веб-почта: %s %s", exc.code, exc.message)
    return errors.ValidationError(exc.message, code=exc.code)


@contextmanager
def _imap(db: Session, box: MailAccount):
    n = nastroyki(db, box)
    try:
        with webmail_imap.imap(n) as klient:
            yield klient
    except WebmailError as exc:
        raise _otkaz(exc) from exc


# --- ящики и папки ----------------------------------------------------------

def yashchiki(db: Session, user: User) -> list[dict]:
    """Ящики сотрудника для левой колонки — из базы, без сети."""
    return [
        {
            "id": box.id,
            "address": box.address,
            "title": box.title,
            "server_id": box.server_id,
            "unseen": box.unseen_count or 0,
            # Новое письмо экран узнаёт по росту UIDNEXT: непрочитанных становится
            # больше и от «пометить непрочитанным» у коллеги.
            "inbox_uidnext": box.inbox_uidnext,
            "inbox_uidvalidity": box.inbox_uidvalidity,
            "can_send": mail_access.mozhet_pisat(db, user, box),
            "is_shared": bool(box.is_shared),
        }
        for box in mail_access.vidimye_yashchiki(db, user)
    ]


def _po_flagu(papka: Papka) -> str | None:
    if papka.path.upper() == INBOX:
        return "inbox"
    for flag in papka.flags:
        if flag in _SPECIAL:
            return _SPECIAL[flag]
    return None


def osobaya(papka: Papka, spisok: list[Papka]) -> str | None:
    """Служебная ли папка. По имени — только если такой же по флагу на сервере нет."""
    vid = _po_flagu(papka)
    if vid:
        return vid
    po_imeni = _PO_IMENI.get(papka.path.split(papka.delimiter)[-1].strip().lower())
    if po_imeni and not any(_po_flagu(q) == po_imeni for q in spisok):
        return po_imeni
    return None


def _vidimye_papki(spisok: list[Papka]) -> list[Papka]:
    papki = [p for p in spisok if not ({"\\noselect", "\\nonexistent"} & set(p.flags))]
    papki.sort(key=lambda p: (_PORYADOK.get(osobaya(p, spisok) or "", 9), p.path.casefold()))
    return papki


def _papka_out(p: Papka, spisok: list[Papka]) -> dict:
    vid = osobaya(p, spisok)
    return {
        "name": "Inbox" if vid == "inbox" else p.path.split(p.delimiter)[-1],
        "path": p.path,
        "special": vid,
        "total": p.total,
        "unseen": p.unseen,
        "delimiter": p.delimiter,
    }


def _papka_vida(spisok: list[Papka], vid: str) -> Papka | None:
    for p in _vidimye_papki(spisok):
        if osobaya(p, spisok) == vid:
            return p
    return None


def papki(db: Session, user: User, account_id: int) -> list[dict]:
    box = mail_access.proverit(db, user, account_id)
    with _imap(db, box) as k:
        spisok = k.papki()
    return [_papka_out(p, spisok) for p in _vidimye_papki(spisok)]


def _chistoe_imya(imya: str) -> str:
    imya = " ".join((imya or "").split())
    if not imya or len(imya) > MAX_IMYA_PAPKI or imya.upper() == INBOX or any(c in imya for c in "*%\\\""):
        raise errors.ValidationError("Bad folder name", code="mail_folder_bad")
    return imya


def _naiti(spisok: list[Papka], path: str) -> Papka:
    for p in spisok:
        if p.path == path:
            return p
    raise errors.ValidationError("Folder not found", code="mail_folder_bad")


def _ne_sluzhebnaya(spisok: list[Papka], path: str) -> Papka:
    papka = _naiti(spisok, path)
    if osobaya(papka, spisok) is not None:
        raise errors.ValidationError("System folders cannot be changed", code="mail_folder_protected")
    return papka


def sozdat_papku(db: Session, user: User, account_id: int, imya: str, parent: str | None = None) -> dict:
    box = mail_access.proverit(db, user, account_id)
    imya = _chistoe_imya(imya)
    with _imap(db, box) as k:
        spisok = k.papki()
        razdel = spisok[0].delimiter if spisok else "/"
        if razdel in imya:
            raise errors.ValidationError("Bad folder name", code="mail_folder_bad")
        if parent:
            razdel = _naiti(spisok, parent).delimiter
        path = f"{parent}{razdel}{imya}" if parent else imya
        if any(p.path == path for p in spisok):
            raise errors.ValidationError("Folder already exists", code="mail_folder_bad")
        k.sozdat(path)
    return {"name": imya, "path": path, "special": None, "total": 0, "unseen": 0, "delimiter": razdel}


def pereimenovat_papku(db: Session, user: User, account_id: int, path: str, novoe: str) -> dict:
    box = mail_access.proverit(db, user, account_id)
    novoe = _chistoe_imya(novoe)
    with _imap(db, box) as k:
        spisok = k.papki()
        papka = _ne_sluzhebnaya(spisok, path)
        if papka.delimiter in novoe:
            raise errors.ValidationError("Bad folder name", code="mail_folder_bad")
        roditel, _, _ = path.rpartition(papka.delimiter)
        novyy = f"{roditel}{papka.delimiter}{novoe}" if roditel else novoe
        if novyy != path:
            if any(p.path == novyy for p in spisok):
                raise errors.ValidationError("Folder already exists", code="mail_folder_bad")
            k.pereimenovat(path, novyy)
    return {"name": novoe, "path": novyy, "special": None, "total": papka.total,
            "unseen": papka.unseen, "delimiter": papka.delimiter}


def udalit_papku(db: Session, user: User, account_id: int, path: str) -> None:
    box = mail_access.proverit(db, user, account_id)
    with _imap(db, box) as k:
        spisok = k.papki()
        _ne_sluzhebnaya(spisok, path)
        k.udalit_papku(path)


# --- список писем -----------------------------------------------------------

def _poisk(q: str | None, unread: bool | None, flagged: bool | None) -> Poisk:
    """`from:имя` и `subject:слово` ищут в своём поле, остальное — по всему письму."""
    q = (q or "").strip()
    pole = "text"
    for pristavka, nazvanie in (("from:", "from"), ("subject:", "subject")):
        if q.lower().startswith(pristavka):
            q, pole = q[len(pristavka):].strip(), nazvanie
            break
    return Poisk(q=q[:200], pole=pole, unread=unread, flagged=flagged)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _svodka_out(s: Svodka) -> dict:
    flags = {f.lower() for f in s.flags}
    return {
        "uid": s.uid,
        "subject": s.subject,
        "from": s.from_,
        "to": s.to,
        "date": _iso(s.date),
        "seen": "\\seen" in flags,
        "flagged": "\\flagged" in flags,
        "answered": "\\answered" in flags,
        "draft": "\\draft" in flags,
        "has_attachments": s.has_attachments,
        "size": s.size,
        "preview": s.preview,
    }


def pisma(
    db: Session,
    user: User,
    account_id: int,
    *,
    folder: str = INBOX,
    page: int = 1,
    per_page: int = 50,
    q: str | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
) -> dict:
    box = mail_access.proverit(db, user, account_id)
    page, per_page = max(1, page), max(1, min(per_page, MAX_PER_PAGE))
    with _imap(db, box) as k:
        k.vybrat(folder)
        uids = k.uidy(_poisk(q, unread, flagged))
        kusok = uids[(page - 1) * per_page : page * per_page]
        svodki = k.svodki(kusok)
    return {
        "items": [_svodka_out(s) for s in svodki],
        "total": len(uids),
        "page": page,
        "per_page": per_page,
        "folder": folder,
    }


# --- письмо целиком ---------------------------------------------------------

@dataclass
class Chast:
    part: str
    name: str
    content_type: str
    data: bytes
    inline: bool
    content_id: str = ""
    vlozhenie: bool = True


@dataclass
class Razobrannoe:
    subject: str = ""
    from_: dict = field(default_factory=lambda: {"name": "", "email": ""})
    to: list[dict] = field(default_factory=list)
    cc: list[dict] = field(default_factory=list)
    #: Есть только в своих черновиках и копиях отправленного — для правки черновика.
    bcc: list[dict] = field(default_factory=list)
    reply_to: list[dict] = field(default_factory=list)
    date: datetime | None = None
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    text: str = ""
    html: str = ""
    chasti: list[Chast] = field(default_factory=list)


def _adresa(msg: EmailMessage, imya: str) -> list[dict]:
    try:
        znacheniya = msg.get_all(imya) or []
        pary = getaddresses([str(z) for z in znacheniya])
    except Exception:  # noqa: BLE001 — кривой заголовок не повод не показать письмо
        return []
    return [{"name": n, "email": a.lower()} for n, a in pary if a]


def _zagolovok(msg: EmailMessage, imya: str) -> str:
    try:
        return " ".join(str(msg.get(imya) or "").split())
    except Exception:  # noqa: BLE001
        return ""


def _tekst_chasti(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 — неизвестная кодировка: показываем как есть
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _obhod(msg: EmailMessage, nomer: str = ""):
    if msg.get_content_maintype() == "multipart" and msg.is_multipart():
        for i, sub in enumerate(msg.iter_parts(), 1):
            yield from _obhod(sub, f"{nomer}.{i}" if nomer else str(i))
    else:
        yield (nomer or "1"), msg


def _imya_fayla(imya: str | None, zapasnoe: str = "file") -> str:
    imya = PureWindowsPath(PurePosixPath(imya or "").name).name
    imya = "".join(c for c in imya if c >= " " and c not in '"\\/:*?<>|').strip(" .")
    return imya[:200] or zapasnoe


def _bayty_chasti(part: EmailMessage) -> bytes:
    if part.get_content_type() == "message/rfc822":
        vnutri = part.get_payload()
        if isinstance(vnutri, list) and vnutri:
            return vnutri[0].as_bytes(policy=email.policy.SMTP)
    return part.get_payload(decode=True) or b""


def razobrat_pismo(syroe: bytes) -> Razobrannoe:
    msg = email.message_from_bytes(syroe, policy=email.policy.default)
    r = Razobrannoe(
        subject=_zagolovok(msg, "Subject"),
        to=_adresa(msg, "To"),
        cc=_adresa(msg, "Cc"),
        bcc=_adresa(msg, "Bcc"),
        reply_to=_adresa(msg, "Reply-To"),
        date=webmail_imap.data_utc(_zagolovok(msg, "Date")),
        message_id=_zagolovok(msg, "Message-ID"),
        in_reply_to=_zagolovok(msg, "In-Reply-To"),
        references=_zagolovok(msg, "References"),
    )
    otkogo = _adresa(msg, "From")
    if otkogo:
        r.from_ = otkogo[0]
    for nomer, part in _obhod(msg):
        tip = part.get_content_type()
        disp = part.get_content_disposition()
        imya = part.get_filename()
        if disp != "attachment" and not imya and tip == "text/plain" and not r.text:
            r.text = _tekst_chasti(part)
            continue
        if disp != "attachment" and not imya and tip == "text/html" and not r.html:
            r.html = _tekst_chasti(part)
            continue
        cid = str(part.get("Content-ID") or "").strip().strip("<>").strip()
        rasshirenie = mimetypes.guess_extension(tip) or ".bin"
        r.chasti.append(
            Chast(
                part=nomer,
                name=_imya_fayla(imya, f"part-{nomer}{rasshirenie}"),
                content_type=tip,
                data=_bayty_chasti(part),
                inline=disp == "inline" or (disp is None and bool(cid)),
                content_id=cid,
            )
        )
    return r


# HTML письма: то, что показывается в <iframe sandbox> без скриптов. Разметка
# вычищается здесь, на сервере: рамка — второй рубеж, а не первый.
_TEGI = set(nh3.ALLOWED_TAGS) | {"font", "center"}
_ATRIBUTY = {tag: set(attrs) for tag, attrs in nh3.ALLOWED_ATTRIBUTES.items()}
_ATRIBUTY["*"] = {"style", "align", "valign", "bgcolor", "width", "height", "border",
                  "cellpadding", "cellspacing", "dir", "title", "lang"}
_ATRIBUTY["font"] = {"color", "face", "size"}
#: Стили — без url(): фон с картинкой грузится мимо кнопки «показать картинки».
_STILI = {
    "color", "background-color", "font", "font-family", "font-size", "font-style", "font-weight",
    "font-variant", "text-align", "text-decoration", "text-transform", "text-indent",
    "letter-spacing", "line-height", "white-space", "word-break", "word-wrap", "overflow-wrap",
    "vertical-align", "direction", "display", "width", "height", "max-width", "min-width",
    "max-height", "min-height", "margin", "margin-top", "margin-right", "margin-bottom",
    "margin-left", "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left", "border-color",
    "border-style", "border-width", "border-collapse", "border-spacing", "border-radius",
    "list-style-type", "table-layout", "float", "clear",
}
_SSYLKI = ("http://", "https://", "mailto:", "tel:", "#")
_VNESHNYAYA = "#opencrm-ext:"


def ochistit_html(html: str, cid: dict[str, str] | None = None) -> tuple[str, bool]:
    """Чистый HTML письма и признак «есть внешние картинки».

    Внешняя картинка не грузится сама: `src` уезжает в `data-src`, и показ — по
    кнопке. Иначе любое открытие письма — отметка отправителю «прочитано, вот
    адрес». Картинки из самого письма (`cid:`) встраиваются как `data:`.
    """
    cid = cid or {}
    vneshnie = False

    def filtr(tag: str, attr: str, value: str) -> str | None:
        nonlocal vneshnie
        nizhe = value.strip().lower()
        if tag == "img" and attr == "src":
            if nizhe.startswith("cid:"):
                return cid.get(value.strip()[4:].strip("<>"))
            if nizhe.startswith(("http://", "https://")):
                vneshnie = True
                return _VNESHNYAYA + value.strip()
            if nizhe.startswith("data:image/"):
                return value
            return None
        if attr in ("href", "cite"):
            return value if nizhe.startswith(_SSYLKI) else None
        return value

    chistyy = nh3.clean(
        html or "",
        tags=_TEGI,
        attributes=_ATRIBUTY,
        url_schemes={"http", "https", "mailto", "tel", "cid", "data"},
        attribute_filter=filtr,
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        filter_style_properties=_STILI,
        strip_comments=True,
        clean_content_tags={"script", "style", "title", "head"},
    )
    # Кавычка в значении атрибута после nh3 всегда `&quot;`, поэтому граница `"` надёжна.
    chistyy = re.sub(r' src="' + re.escape(_VNESHNYAYA) + r'([^"]*)"', r' data-src="\1"', chistyy)
    return chistyy, vneshnie


def _tekst_iz_html(html: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", html or "")
    s = html_lib.unescape(nh3.clean(s, tags=set(), attributes={}, clean_content_tags={"script", "style"}))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in s.splitlines())).strip()


def _cid_karta(chasti: list[Chast]) -> dict[str, str]:
    karta = {}
    for c in chasti:
        if c.content_id and c.content_type.startswith("image/") and len(c.data) <= MAX_CID_BAYT:
            karta[c.content_id] = f"data:{c.content_type};base64," + base64.b64encode(c.data).decode()
    return karta


def _syroe_pismo(k, folder: str, uid: int, *, prochitat: bool = False) -> tuple[bytes, frozenset[str]]:
    k.vybrat(folder, tolko_chtenie=not prochitat)
    najdeno = k.syroe(uid)
    if najdeno is None:
        raise errors.NotFoundError("Message not found", code="mail_message_gone")
    return najdeno


def pismo(db: Session, user: User, account_id: int, uid: int, folder: str = INBOX) -> dict:
    """Письмо целиком; открыть — значит прочитать (\\Seen)."""
    box = mail_access.proverit(db, user, account_id)
    with _imap(db, box) as k:
        syroe, flags = _syroe_pismo(k, folder, uid, prochitat=True)
        if "\\seen" not in {f.lower() for f in flags}:
            k.flagi([uid], dobavit={"\\Seen"})
            flags = flags | {"\\Seen"}
    r = razobrat_pismo(syroe)
    chistyy, vneshnie = ochistit_html(r.html, _cid_karta(r.chasti)) if r.html else ("", False)
    client = mail_repo.find_client_by_email(db, r.from_["email"])
    return {
        "uid": uid,
        "folder": folder,
        "subject": r.subject,
        "from": r.from_,
        "to": r.to,
        "cc": r.cc,
        "bcc": r.bcc,
        "reply_to": r.reply_to,
        "date": _iso(r.date),
        "message_id": r.message_id,
        "in_reply_to": r.in_reply_to,
        "references": r.references,
        "text": r.text if r.text or not r.html else _tekst_iz_html(r.html),
        "html": chistyy,
        "remote_images": vneshnie,
        "attachments": [
            {"part": c.part, "name": c.name, "size": len(c.data), "content_type": c.content_type,
             "inline": c.inline}
            for c in r.chasti
        ],
        "flags": sorted(flags),
        "client": {"id": client.id, "name": client.name} if client else None,
    }


#: Политика страницы письма. Своя, а не политика приложения: рамка с `srcdoc`
#: наследует `img-src` CRM, и картинки по кнопке не показались бы вовсе. Здесь —
#: ни скриптов, ни форм, ни подключений; внешние картинки — только по `kartinki`.
CSP_PISMA = (
    "default-src 'none'; img-src data:{https}; style-src 'unsafe-inline'; font-src data:; "
    "frame-ancestors 'self'; form-action 'none'; base-uri 'none'; "
    "sandbox allow-popups allow-popups-to-escape-sandbox"
)
STIL_PISMA = (
    "html{color-scheme:light}body{margin:0;padding:18px 20px;"
    "font:14px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif;overflow-wrap:anywhere}"
    "img{max-width:100%;height:auto}table{max-width:100%}pre{white-space:pre-wrap}"
    "blockquote{margin:0 0 0 4px;padding-left:12px;border-left:2px solid}"
)


def html_dokument(
    db: Session, user: User, account_id: int, uid: int, folder: str = INBOX, *, kartinki: bool = False
) -> tuple[str, str]:
    """Страница письма для рамки и её политика. Читает без «прочитано»: это делает `pismo`."""
    box = mail_access.proverit(db, user, account_id)
    with _imap(db, box) as k:
        syroe, _ = _syroe_pismo(k, folder, uid)
    r = razobrat_pismo(syroe)
    chistyy, _ = ochistit_html(r.html, _cid_karta(r.chasti)) if r.html else ("", False)
    if kartinki:
        # Только https: картинка по http выдала бы адрес смотрящего и всей сети по дороге.
        chistyy = re.sub(r' data-src="(https://[^"]*)"', r' src="\1"', chistyy)
    stranitsa = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer"><base target="_blank">'
        f"<style>{STIL_PISMA}</style></head><body>{chistyy}</body></html>"
    )
    return stranitsa, CSP_PISMA.format(https=" https:" if kartinki else "")


def bezopasnyy_tip(tip: str) -> str:
    """Тип из чужого письма — в заголовок ответа, только если это и правда `тип/подтип`."""
    tip = (tip or "").strip().lower()
    return tip if _TIP.match(tip) else "application/octet-stream"


def vlozhenie(db: Session, user: User, account_id: int, uid: int, part: str, folder: str = INBOX) -> Chast:
    box = mail_access.proverit(db, user, account_id)
    with _imap(db, box) as k:
        syroe, _ = _syroe_pismo(k, folder, uid)
    for c in razobrat_pismo(syroe).chasti:
        if c.part == part:
            return c
    raise errors.NotFoundError("Attachment not found", code="mail_message_gone")


def iskhodnik(db: Session, user: User, account_id: int, uid: int, folder: str = INBOX) -> bytes:
    box = mail_access.proverit(db, user, account_id)
    with _imap(db, box) as k:
        syroe, _ = _syroe_pismo(k, folder, uid)
    return syroe


# --- флаги, перенос, удаление -------------------------------------------------

def _uidy(uids: list[int]) -> list[int]:
    chistye = sorted({int(u) for u in uids if int(u) > 0})
    if not chistye or len(chistye) > MAX_UIDS:
        raise errors.ValidationError(f"Pass 1 to {MAX_UIDS} messages")
    return chistye


def flagi(
    db: Session, user: User, account_id: int, folder: str, uids: list[int],
    *, seen: bool | None = None, flagged: bool | None = None,
) -> dict:
    box = mail_access.proverit(db, user, account_id)
    uids = _uidy(uids)
    dobavit, ubrat = set(), set()
    for flag, znach in (("\\Seen", seen), ("\\Flagged", flagged)):
        if znach is True:
            dobavit.add(flag)
        elif znach is False:
            ubrat.add(flag)
    with _imap(db, box) as k:
        k.vybrat(folder, tolko_chtenie=False)
        k.flagi(uids, dobavit=dobavit, ubrat=ubrat)
    return {"ok": True, "count": len(uids)}


def peremestit(db: Session, user: User, account_id: int, folder: str, uids: list[int], kuda: str) -> dict:
    box = mail_access.proverit(db, user, account_id)
    uids = _uidy(uids)
    with _imap(db, box) as k:
        spisok = k.papki()
        _naiti(spisok, kuda)
        if kuda != folder:
            k.vybrat(folder, tolko_chtenie=False)
            k.peremestit(uids, kuda)
    return {"ok": True, "count": len(uids), "to": kuda}


def udalit(db: Session, user: User, account_id: int, folder: str, uids: list[int]) -> dict:
    """В Корзину; из Корзины — насовсем. Корзины нет — заводим её, а не стираем молча."""
    box = mail_access.proverit(db, user, account_id)
    uids = _uidy(uids)
    with _imap(db, box) as k:
        spisok = k.papki()
        korzina = _papka_vida(spisok, "trash")
        k.vybrat(folder, tolko_chtenie=False)
        if korzina is not None and korzina.path == folder:
            k.stert(uids)
            return {"ok": True, "count": len(uids), "to": None}
        kuda = korzina.path if korzina is not None else _papka_ili_novaya(k, spisok, "trash", "Trash")
        k.peremestit(uids, kuda)
    return {"ok": True, "count": len(uids), "to": kuda}


# --- загрузки вложений --------------------------------------------------------

def _koren_zagruzok() -> Path:
    koren = os.environ.get("OPENCRM_DATA_DIR")
    return (Path(koren) if koren else BASE_DIR / "data") / "tmp" / "pochta"


def _papka_zagruzok(user_id: int) -> Path:
    return _koren_zagruzok() / str(int(user_id))


def ubrat_starye_zagruzki(seychas: float | None = None) -> int:
    """Снести загрузки старше суток. Зовётся по ходу дела: отдельного дворника нет."""
    seychas = seychas or time.time()
    ubrano = 0
    koren = _koren_zagruzok()
    if not koren.is_dir():
        return 0
    for put in koren.glob("*/*"):
        try:
            if seychas - put.stat().st_mtime > ZAGRUZKA_ZHIVYOT_SEKUND:
                put.unlink()
                ubrano += 1
        except OSError:
            continue
    return ubrano


def _zanyato(papka: Path) -> int:
    """Сколько места уже держат загрузки сотрудника (файлы, не их описания)."""
    vsego = 0
    for put in papka.glob("*"):
        if put.suffix == ".json":
            continue
        try:
            vsego += put.stat().st_size
        except OSError:
            continue
    return vsego


def zagruzit(user: User, imya: str | None, content_type: str | None, potok) -> dict:
    """Файл к будущему письму: во временный каталог сотрудника, не в базу."""
    ubrat_starye_zagruzki()
    papka = _papka_zagruzok(user.id)
    papka.mkdir(parents=True, exist_ok=True)
    zanyato = _zanyato(papka)
    upload_id = secrets.token_hex(16)
    put = papka / upload_id
    razmer = 0
    try:
        with open(put, "wb") as f:
            while True:
                kusok = potok.read(1024 * 1024)
                if not kusok:
                    break
                razmer += len(kusok)
                if razmer > MAX_VLOZHENIE_BAYT:
                    raise errors.ValidationError(
                        f"File is too big (max {MAX_VLOZHENIE_BAYT // (1024 * 1024)} MB)",
                        code="mail_upload_too_big",
                    )
                if zanyato + razmer > KVOTA_ZAGRUZOK_BAYT:
                    raise errors.ValidationError(
                        f"Too many unsent attachments (max {KVOTA_ZAGRUZOK_BAYT // (1024 * 1024)} MB): "
                        "send or discard a draft first",
                        code="mail_upload_quota",
                    )
                f.write(kusok)
    except BaseException:
        put.unlink(missing_ok=True)
        raise
    imya = _imya_fayla(imya)
    tip = (content_type or "").split(";")[0].strip().lower()
    if not _TIP.match(tip) or tip == "application/octet-stream":
        tip = mimetypes.guess_type(imya)[0] or "application/octet-stream"
    (papka / f"{upload_id}.json").write_text(
        json.dumps({"name": imya, "size": razmer, "content_type": tip}), encoding="utf-8"
    )
    return {"id": upload_id, "name": imya, "size": razmer}


def _zagruzki(user: User, ids: list[str]) -> list[tuple[str, str, bytes, Path]]:
    """Вложения письма. Повторы — один раз, сумма — по диску ДО чтения: один id
    пятьдесят раз поднимал в память 1,25 ГБ раньше проверки размера (разбор 10.10.2026)."""
    naydeno, vsego = [], 0
    for upload_id in dict.fromkeys(str(i) for i in ids or []):
        # Сначала вид id, потом путь: из `../..` путь собирать нельзя вовсе.
        if not _ID_ZAGRUZKI.match(upload_id):
            raise errors.ValidationError("Attachment upload not found", code="mail_upload_missing")
        put = _papka_zagruzok(user.id) / upload_id
        meta = put.with_name(f"{upload_id}.json")
        if not put.is_file() or not meta.is_file():
            raise errors.ValidationError("Attachment upload not found", code="mail_upload_missing")
        try:
            opisanie = json.loads(meta.read_text(encoding="utf-8"))
            vsego += put.stat().st_size
        except (OSError, ValueError) as exc:
            raise errors.ValidationError("Attachment upload not found", code="mail_upload_missing") from exc
        if vsego > MAX_PISMO_BAYT:
            raise errors.ValidationError(
                f"Attachments are too big (max {MAX_PISMO_BAYT // (1024 * 1024)} MB per message)",
                code="mail_upload_too_big",
            )
        naydeno.append((opisanie, put))
    return [
        (o.get("name") or "file", o.get("content_type") or "application/octet-stream", put.read_bytes(), put)
        for o, put in naydeno
    ]


# --- отправка и черновики ----------------------------------------------------

def _odna_stroka(text: str) -> str:
    """Без переводов строк: перевод в заголовке дописал бы в письмо чужие заголовки."""
    return " ".join(str(text or "").split())


def _chistye_adresa(adresa: list[str] | None) -> list[str]:
    return [_odna_stroka(a) for a in adresa or [] if a and a.strip()]


@dataclass
class _Ssylka:
    """Исходное письмо ответа или пересылки: в каком ящике лежит и что в нём."""

    box: MailAccount
    folder: str
    uid: int
    #: None — письма уже нет: его переложили или удалили, пока писали ответ.
    pismo: Razobrannoe | None


def _prochitat(k, folder: str, uid: int) -> Razobrannoe | None:
    try:
        syroe, _ = _syroe_pismo(k, folder, uid)
    except errors.NotFoundError:
        return None
    except WebmailError as exc:
        # Папку с исходным удалили или переименовали — то же «письма нет».
        if exc.code != "mail_folder_bad":
            raise
        return None
    return razobrat_pismo(syroe)


def _iskhodnoe(db: Session, user: User, box: MailAccount, k, ssylka: dict | None) -> _Ssylka | None:
    """Исходное — из его ящика (`box_id`), после проверки, что сотрудник этот ящик видит.

    Тот же UID в ящике отправки — другое письмо: ответ уносил чужую цепочку, а
    пересылка — чужие вложения (разбор 10.10.2026). Без `box_id` — ящик отправки.
    """
    if not ssylka:
        return None
    folder, uid = str(ssylka.get("folder") or INBOX), int(ssylka["uid"])
    box_id = ssylka.get("box_id")
    if not box_id or int(box_id) == box.id:
        return _Ssylka(box, folder, uid, _prochitat(k, folder, uid))
    drugoy = mail_access.proverit(db, user, int(box_id))
    with _imap(db, drugoy) as k2:
        return _Ssylka(drugoy, folder, uid, _prochitat(k2, folder, uid))


def _pismo(ssylka: _Ssylka | None) -> Razobrannoe | None:
    return ssylka.pismo if ssylka is not None else None


def _preduprezhdenie(*ssylki: _Ssylka | None) -> str | None:
    """Исходного нет — письмо уходит без цепочки и без его вложений, а ответ говорит
    об этом. Отказ здесь терял набранный текст: окно не закрывалось (разбор 10.10.2026)."""
    return ISKHODNOGO_NET if any(s is not None and s.pismo is None for s in ssylki) else None


def _est_telo(dannye: dict) -> bool:
    html = dannye.get("html") or ""
    return bool((dannye.get("text") or "").strip() or _tekst_iz_html(html) or "<img" in html.lower())


def _tsepochka(otvet: Razobrannoe | None) -> tuple[str, str]:
    if otvet is None or not otvet.message_id:
        return "", ""
    svoy = otvet.message_id if otvet.message_id.startswith("<") else f"<{otvet.message_id}>"
    vetka = [*otvet.references.split(), svoy][-mail_service.MAX_REFERENCES:]
    return svoy, " ".join(vetka)


@dataclass
class _Pismo:
    msg: EmailMessage
    message_id: str
    in_reply_to: str
    references: str
    text: str
    html: str
    vlozheniy: int


def _sobrat(
    box: MailAccount,
    dannye: dict,
    *,
    vlozheniya: list[tuple[str, str, bytes]],
    otvet: Razobrannoe | None,
    peresylka: Razobrannoe | None,
    s_bcc: bool,
) -> _Pismo:
    msg = EmailMessage()
    nazvanie = (box.title or "").strip()
    try:
        msg["From"] = Address(display_name=nazvanie if nazvanie and nazvanie != box.address else "",
                              addr_spec=box.address)
    except (ValueError, IndexError):
        msg["From"] = box.address
    for zagolovok, klyuch in (("To", "to"), ("Cc", "cc")):
        adresa = _chistye_adresa(dannye.get(klyuch))
        if adresa:
            msg[zagolovok] = ", ".join(adresa)
    if s_bcc and _chistye_adresa(dannye.get("bcc")):
        msg["Bcc"] = ", ".join(_chistye_adresa(dannye.get("bcc")))
    msg["Subject"] = _odna_stroka(dannye.get("subject") or "")
    msg["Date"] = formatdate(localtime=True)
    domen = box.address.rsplit("@", 1)[-1] or None
    message_id = make_msgid(domain=domen)
    msg["Message-ID"] = message_id
    in_reply_to, references = _tsepochka(otvet)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references

    html = dannye.get("html") or ""
    text = dannye.get("text") or ""
    if html:
        html = ochistit_html_iskhodyashchiy(html)
        text = text if text.strip() else _tekst_iz_html(html)
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text)

    vse = list(vlozheniya)
    if peresylka is not None:
        vse += [(c.name, c.content_type, c.data) for c in peresylka.chasti]
    razmer = 0
    for imya, tip, data in vse:
        razmer += len(data)
        if razmer > MAX_PISMO_BAYT:
            raise errors.ValidationError(
                f"Attachments are too big (max {MAX_PISMO_BAYT // (1024 * 1024)} MB per message)",
                code="mail_upload_too_big",
            )
        if tip == "message/rfc822":
            msg.add_attachment(email.message_from_bytes(data, policy=email.policy.default), filename=imya)
            continue
        osnovnoy, _, pod = tip.partition("/")
        if not _TIP.match(tip) or osnovnoy in ("multipart", "message"):
            osnovnoy, pod = "application", "octet-stream"
        msg.add_attachment(data, maintype=osnovnoy, subtype=pod, filename=imya)
    return _Pismo(msg, message_id, in_reply_to, references, text, html, len(vse))


def ochistit_html_iskhodyashchiy(html: str) -> str:
    """Своё письмо тоже чистим: из редактора могло прийти что угодно, а уйдёт оно клиенту."""
    return nh3.clean(
        html,
        tags=_TEGI,
        attributes=_ATRIBUTY,
        url_schemes={"http", "https", "mailto", "tel", "data"},
        link_rel="noopener noreferrer",
        filter_style_properties=_STILI,
        strip_comments=True,
    )


def _bayty(msg: EmailMessage) -> bytes:
    return msg.as_bytes(policy=email.policy.SMTP)


def _papka_ili_novaya(k, spisok: list[Papka], vid: str, imya: str) -> str:
    papka = _papka_vida(spisok, vid)
    if papka is not None:
        return papka.path
    if not any(p.path == imya for p in spisok):
        k.sozdat(imya)
    return imya


def otpravit(db: Session, user: User, account_id: int, dannye: dict) -> dict:
    """Отправить письмо из ящика: SMTP, копия в «Отправленные», запись в ленту клиента.

    Порядок тот же, что у `mail_service.send_message`: всё, что может отказать, —
    ДО SMTP. После отправки отказывать нечем: письмо уже у адресата, и ошибка
    на экране означала бы второе такое же письмо по второму нажатию.
    """
    box = mail_access.proverit(db, user, account_id, otpravka=True)
    vse_adresaty = mail_service.proverit_adresatov(
        [*(dannye.get("to") or []), *(dannye.get("cc") or []), *(dannye.get("bcc") or [])]
    )
    client_id, deal_id = mail_service.proverit_privyazki(db, dannye.get("client_id"), dannye.get("deal_id"))
    zagruzki = _zagruzki(user, dannye.get("attachments") or [])
    n = nastroyki(db, box)
    sohraneno = False
    try:
        with webmail_imap.imap(n) as k:
            spisok = k.papki()
            otvet = _iskhodnoe(db, user, box, k, dannye.get("reply_to"))
            peresylka = _iskhodnoe(db, user, box, k, dannye.get("forward_of"))
            vlozheniya_iskhodnogo = _pismo(peresylka).chasti if _pismo(peresylka) else []
            if not _est_telo(dannye) and not zagruzki and not vlozheniya_iskhodnogo:
                raise errors.ValidationError("Message body is required", code="body_required")
            pismo_ = _sobrat(box, dannye, vlozheniya=[z[:3] for z in zagruzki], otvet=_pismo(otvet),
                             peresylka=_pismo(peresylka), s_bcc=False)
            with webmail_imap.smtp(n) as s:
                otkazali = s.otpravit(box.address, vse_adresaty, _bayty(pismo_.msg))
            # Дальше — только то, без чего письмо всё равно ушло.
            sohraneno = _posle_otpravki(db, k, spisok, box, dannye, pismo_,
                                        ((otvet, "\\Answered"), (peresylka, "$Forwarded")))
    except WebmailError as exc:
        raise _otkaz(exc) from exc

    message = mail_service.zapisat_iskhodyashchee(
        db,
        user,
        box,
        message_id=pismo_.message_id,
        subject=_odna_stroka(dannye.get("subject") or ""),
        body_text=pismo_.text,
        body_html=pismo_.html,
        recipients=vse_adresaty,
        sent_at=now_utc(),
        in_reply_to=pismo_.in_reply_to,
        references=pismo_.references,
        client_id=client_id,
        deal_id=deal_id,
        has_attachments=pismo_.vlozheniy > 0,
        refused=sorted(otkazali),
    )
    for *_, put in zagruzki:
        put.unlink(missing_ok=True)
        put.with_name(f"{put.name}.json").unlink(missing_ok=True)
    return {
        "ok": True,
        "message_id": pismo_.message_id,
        "refused": otkazali,
        "saved_to_sent": sohraneno,
        "id": message.id,
        "warning": _preduprezhdenie(otvet, peresylka),
    }


def _otmetit(db: Session, k, box: MailAccount, ssylka: _Ssylka, flag: str) -> None:
    if ssylka.box.id == box.id:
        k.vybrat(ssylka.folder, tolko_chtenie=False)
        k.flagi([ssylka.uid], dobavit={flag})
        return
    with _imap(db, ssylka.box) as k2:
        k2.vybrat(ssylka.folder, tolko_chtenie=False)
        k2.flagi([ssylka.uid], dobavit={flag})


def _posle_otpravki(
    db: Session, k, spisok: list[Papka], box: MailAccount, dannye: dict, pismo_: _Pismo, otmetki
) -> bool:
    """Копия в «Отправленные», отметки исходному, снять черновик. Беда — в журнал, не на экран.

    Письмо уже ушло: отказ здесь задвоил бы его. Шаги порознь — несложившаяся отметка
    не оставляет ушедшее письмо в «Черновиках». Ответ — легла ли копия в «Отправленные».
    """
    sohraneno = True
    try:
        # Скрытая копия — только в своём экземпляре: адресатам её видеть не положено.
        bcc = _chistye_adresa(dannye.get("bcc"))
        if bcc:
            pismo_.msg["Bcc"] = ", ".join(bcc)
        otpravlennye = _papka_ili_novaya(k, spisok, "sent", "Sent")
        k.dobavit(otpravlennye, _bayty(pismo_.msg), {"\\Seen"})
    except Exception:  # noqa: BLE001
        sohraneno = False
        logger.exception("ящик %s: письмо ушло, но копия в «Отправленные» не легла", box.address)
    for ssylka, flag in otmetki:
        if ssylka is None or ssylka.pismo is None:
            continue
        try:
            _otmetit(db, k, box, ssylka, flag)
        except Exception:  # noqa: BLE001
            logger.exception("ящик %s: письмо ушло, но отметка %s исходному не легла", box.address, flag)
    if dannye.get("draft_uid"):
        try:
            chernoviki = _papka_vida(spisok, "drafts")
            if chernoviki is not None:
                k.vybrat(chernoviki.path, tolko_chtenie=False)
                k.stert([int(dannye["draft_uid"])])
        except Exception:  # noqa: BLE001
            logger.exception("ящик %s: письмо ушло, но черновик не снят", box.address)
    return sohraneno


def chernovik(db: Session, user: User, account_id: int, dannye: dict) -> dict:
    """Сохранить черновик в «Черновики»; прежний (`draft_uid`) — снять."""
    box = mail_access.proverit(db, user, account_id, otpravka=True)
    adresa = [*(dannye.get("to") or []), *(dannye.get("cc") or []), *(dannye.get("bcc") or [])]
    if any(a and a.strip() for a in adresa):
        mail_service.proverit_adresatov(adresa)
    zagruzki = _zagruzki(user, dannye.get("attachments") or [])
    with _imap(db, box) as k:
        spisok = k.papki()
        otvet = _iskhodnoe(db, user, box, k, dannye.get("reply_to"))
        peresylka = _iskhodnoe(db, user, box, k, dannye.get("forward_of"))
        pismo_ = _sobrat(box, dannye, vlozheniya=[z[:3] for z in zagruzki], otvet=_pismo(otvet),
                         peresylka=_pismo(peresylka), s_bcc=True)
        chernoviki = _papka_ili_novaya(k, spisok, "drafts", "Drafts")
        uid = k.dobavit(chernoviki, _bayty(pismo_.msg), {"\\Draft", "\\Seen"})
        if dannye.get("draft_uid"):
            k.vybrat(chernoviki, tolko_chtenie=False)
            k.stert([int(dannye["draft_uid"])])
    return {"uid": uid, "folder": chernoviki, "message_id": pismo_.message_id,
            "warning": _preduprezhdenie(otvet, peresylka)}
