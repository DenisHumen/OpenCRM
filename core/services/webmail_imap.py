"""Разговор веб-почты с сервером: IMAPS 993 и SMTPS 465 (docs/bloki/32-pochtovyy-server.md).

Здесь только протокол: войти, перечислить папки, отсортировать, забрать, переложить.
Что из этого показать человеку и что записать в базу, решает `webmail_service`.
Всё, что ходит в сеть, создаётся фабриками `set_imap_factory` / `set_smtp_factory`:
в наборе тестов на их место встаёт почтовый ящик в памяти, и ни один тест в сеть
не ходит.

Свой сервер открывается мастером (`ящик*мастер`): CRM не хранит паролей ящиков вовсе.
Сертификат сверяется с ИМЕНЕМ сервера, даже когда соединяемся по адресу контейнера;
самоподписанный — по отпечатку `tls_pin`, а не «не проверять».
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import imaplib
import re
import smtplib
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

#: Сеть молчит — не держим человека у экрана дольше этого.
TIMEOUT_SECONDS = 20
#: Сколько писем забираем одной командой FETCH (страница списка — до 200).
FETCH_PACHKA = 200


class WebmailError(Exception):
    """Сервер не ответил, отказал или ответил не по протоколу. `code` — для экрана."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = (message or code)[:500]
        super().__init__(self.message)


@dataclass(frozen=True)
class Nastroyki:
    """Куда и как входить. Пароль в repr не попадает: настройки бывают в журнале ошибок."""

    address: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    login: str
    password: str = field(repr=False)
    imap_ssl: bool = True
    smtp_ssl: bool = True
    #: Имя для SNI и сверки сертификата; пусто — имя хоста соединения.
    tls_name: str = ""
    #: sha256 сертификата (hex) для самоподписанного; пусто — обычная проверка цепочки.
    tls_pin: str = field(default="", repr=False)


# --- TLS ---------------------------------------------------------------------

def _kontekst(pin: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if pin:
        # Отпечаток строже цепочки: ему верим, остальное не спрашиваем.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _normalnyy_pin(pin: str) -> str:
    return (pin or "").replace(":", "").strip().lower()


def proverit_pin(ssock: ssl.SSLSocket, pin: str) -> None:
    """Сертификат сервера совпал с запомненным отпечатком — или отказ."""
    pin = _normalnyy_pin(pin)
    if not pin:
        return
    der = ssock.getpeercert(binary_form=True) or b""
    if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), pin):
        try:
            ssock.close()
        finally:
            raise ssl.SSLError("server certificate does not match the pinned fingerprint")


class _Imap4Tls(imaplib.IMAP4_SSL):
    """IMAPS, где имя для сертификата не обязано совпадать с адресом соединения."""

    def __init__(self, host: str, port: int, *, tls_name: str, tls_pin: str, timeout: float):
        # До super(): соединение открывается прямо в конструкторе.
        self._tls_name = tls_name or host
        self._tls_pin = tls_pin
        super().__init__(host, port, ssl_context=_kontekst(tls_pin), timeout=timeout)

    def _create_socket(self, timeout):
        sock = imaplib.IMAP4._create_socket(self, timeout)
        ssock = self.ssl_context.wrap_socket(sock, server_hostname=self._tls_name)
        proverit_pin(ssock, self._tls_pin)
        return ssock


class _SmtpTls(smtplib.SMTP_SSL):
    def __init__(self, host: str, port: int, *, tls_name: str, tls_pin: str, timeout: float):
        self._tls_name = tls_name or host
        self._tls_pin = tls_pin
        super().__init__(host, port, timeout=timeout, context=_kontekst(tls_pin))

    def _get_socket(self, host, port, timeout):
        sock = smtplib.SMTP._get_socket(self, host, port, timeout)
        ssock = self.context.wrap_socket(sock, server_hostname=self._tls_name)
        proverit_pin(ssock, self._tls_pin)
        return ssock


# --- имена папок: modified UTF-7 (RFC 3501 §5.1.3) ----------------------------

def mutf7_encode(text: str) -> str:
    vyhod: list[str] = []
    bufer: list[str] = []

    def slit() -> None:
        if bufer:
            syrye = "".join(bufer).encode("utf-16-be")
            vyhod.append("&" + base64.b64encode(syrye).decode().rstrip("=").replace("/", ",") + "-")
            bufer.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            slit()
            vyhod.append("&-" if ch == "&" else ch)
        else:
            bufer.append(ch)
    slit()
    return "".join(vyhod)


def mutf7_decode(text: str) -> str:
    vyhod: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "&":
            vyhod.append(text[i])
            i += 1
            continue
        konec = text.find("-", i)
        if konec == -1:
            vyhod.append(text[i:])
            break
        kusok = text[i + 1 : konec]
        if not kusok:
            vyhod.append("&")
        else:
            kusok = kusok.replace(",", "/")
            try:
                vyhod.append(base64.b64decode(kusok + "=" * (-len(kusok) % 4)).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                vyhod.append(text[i : konec + 1])
        i = konec + 1
    return "".join(vyhod)


def _v_kavychkah(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imya_papki(path: str) -> str:
    """Путь папки для команды IMAP: в UTF-7 и в кавычках."""
    return _v_kavychkah(mutf7_encode(path))


# --- разбор ответов IMAP -------------------------------------------------------

_LP, _RP, _NIL = object(), object(), object()


def _tokeny(data: list) -> Iterator:
    """Лексемы ответа imaplib: строки и литералы вперемешку, как он их отдаёт."""
    chasti: list[tuple[bool, bytes]] = []
    for item in data or []:
        if isinstance(item, tuple):
            chasti.append((False, item[0]))
            chasti.append((True, item[1]))
        elif isinstance(item, bytes):
            chasti.append((False, item))
    for literal, kusok in chasti:
        if literal:
            yield kusok
            continue
        i, n = 0, len(kusok)
        while i < n:
            c = kusok[i : i + 1]
            if c in b" \r\n\t":
                i += 1
            elif c == b"(":
                yield _LP
                i += 1
            elif c == b")":
                yield _RP
                i += 1
            elif c == b'"':
                j, buf = i + 1, bytearray()
                while j < n and kusok[j : j + 1] != b'"':
                    if kusok[j : j + 1] == b"\\" and j + 1 < n:
                        j += 1
                    buf += kusok[j : j + 1]
                    j += 1
                yield bytes(buf)
                i = j + 1
            elif c in (b"{", b"~") and re.fullmatch(rb"~?\{\d+\+?\}", kusok[i:]):
                # Литерал: его содержимое — следующая часть ответа.
                i = n
            else:
                j, glubina = i, 0
                while j < n:
                    ch = kusok[j : j + 1]
                    if ch == b"[":
                        glubina += 1
                    elif ch == b"]":
                        glubina -= 1
                    elif glubina <= 0 and ch in b" ()":
                        break
                    j += 1
                atom = kusok[i:j].decode("utf-8", "replace")
                yield _NIL if atom.upper() == "NIL" else atom
                i = j


def razobrat(data: list) -> list:
    """Ответ IMAP → значения: атом — str, строка — bytes, NIL — None, скобки — list."""
    tokeny = list(_tokeny(data))
    pos = 0

    def znachenie():
        nonlocal pos
        tok = tokeny[pos]
        pos += 1
        if tok is _LP:
            spisok = []
            while pos < len(tokeny) and tokeny[pos] is not _RP:
                spisok.append(znachenie())
            pos += 1
            return spisok
        return None if tok is _NIL else tok

    vyhod = []
    while pos < len(tokeny):
        if tokeny[pos] is _RP:
            pos += 1
            continue
        vyhod.append(znachenie())
    return vyhod


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def raskodirovat(v) -> str:
    """Заголовок с encoded-words (=?utf-8?b?...?=) → текст."""
    text = _str(v)
    if "=?" not in text:
        return text
    try:
        return str(make_header(decode_header(text)))
    except Exception:  # noqa: BLE001 — кривая кодировка темы не повод терять письмо
        return text


def data_utc(value) -> datetime | None:
    text = _str(value).strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed
    try:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, ValueError):
        return None


def _internaldate(value) -> datetime | None:
    text = _str(value).strip()
    try:
        parsed = datetime.strptime(text, "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class Papka:
    path: str
    delimiter: str
    flags: frozenset[str]
    total: int = 0
    unseen: int = 0


@dataclass
class Svodka:
    """Строка списка писем — всё, что видно без тела письма."""

    uid: int
    flags: frozenset[str] = frozenset()
    size: int = 0
    subject: str = ""
    from_: dict = field(default_factory=lambda: {"name": "", "email": ""})
    to: list[dict] = field(default_factory=list)
    cc: list[dict] = field(default_factory=list)
    date: datetime | None = None
    message_id: str = ""
    has_attachments: bool = False
    preview: str = ""


@dataclass(frozen=True)
class Poisk:
    """Что ищем в папке. `pole`: text | from | subject."""

    q: str = ""
    pole: str = "text"
    unread: bool | None = None
    flagged: bool | None = None


def _adresa(spisok) -> list[dict]:
    vyhod = []
    for a in spisok or []:
        if not isinstance(a, list) or len(a) < 4 or a[3] is None:
            continue  # начало и конец группы адресов (RFC 3501 §7.4.2)
        vyhod.append({"name": raskodirovat(a[0]), "email": f"{_str(a[2])}@{_str(a[3])}".lower()})
    return vyhod


def _est_vlozheniya(bs, pod_related: bool = False) -> bool:
    if not isinstance(bs, list) or not bs:
        return False
    if isinstance(bs[0], list):
        deti = []
        for x in bs:
            if not isinstance(x, list):
                break
            deti.append(x)
        podtip = _str(bs[len(deti)]).lower() if len(bs) > len(deti) else ""
        return any(_est_vlozheniya(d, podtip == "related") for d in deti)
    tip, podtip = _str(bs[0]).lower(), _str(bs[1]).lower()
    if (tip, podtip) == ("message", "rfc822"):
        return True
    nachalo_rasshireniy = 8 if tip == "text" else 7
    disp = bs[nachalo_rasshireniy + 1] if len(bs) > nachalo_rasshireniy + 1 else None
    vid = _str(disp[0]).lower() if isinstance(disp, list) and disp else ""
    if vid == "attachment":
        return True
    imya = False
    for params in (bs[2], disp[1] if isinstance(disp, list) and len(disp) > 1 else None):
        if isinstance(params, list):
            klyuchi = [_str(k).lower() for k in params[::2]]
            imya = imya or "name" in klyuchi or "filename" in klyuchi
    return imya and tip != "text" and not pod_related


def svodka_iz_fetch(attrs: dict) -> Svodka:
    env = attrs.get("ENVELOPE") or [None] * 10
    env = list(env) + [None] * (10 - len(env))
    otkogo = _adresa(env[2])
    preview = attrs.get("PREVIEW")
    if isinstance(preview, list):  # старое SNIPPET (FUZZY "...")
        preview = preview[-1] if preview else None
    return Svodka(
        uid=int(attrs["UID"]),
        flags=frozenset(_str(f) for f in attrs.get("FLAGS") or []),
        size=int(attrs.get("RFC822.SIZE") or 0),
        subject=raskodirovat(env[1]),
        from_=otkogo[0] if otkogo else {"name": "", "email": ""},
        to=_adresa(env[5]),
        cc=_adresa(env[6]),
        date=data_utc(env[0]) or _internaldate(attrs.get("INTERNALDATE")),
        message_id=_str(env[9]).strip(),
        has_attachments=_est_vlozheniya(attrs.get("BODYSTRUCTURE")),
        preview=" ".join(_str(preview).split())[:300],
    )


def fetch_atributy(data: list) -> list[dict]:
    """Ответ FETCH → словари атрибутов писем (без чужих FETCH без UID)."""
    vyhod = []
    for v in razobrat(data):
        if not isinstance(v, list):
            continue
        attrs = {}
        for i in range(0, len(v) - 1, 2):
            attrs[_str(v[i]).upper()] = v[i + 1]
        if "UID" in attrs:
            vyhod.append(attrs)
    return vyhod


def _diapazon(uids: list[int]) -> str:
    return ",".join(str(u) for u in uids)


# --- клиент IMAP -------------------------------------------------------------

class ImapKlient:
    """Один вход на одну операцию экрана: открыть, сделать, закрыть."""

    def __init__(self, soedinenie: imaplib.IMAP4):
        self.c = soedinenie
        typ, dat = self.c.capability()
        caps = _str(dat[0]).upper().split() if typ == "OK" and dat else []
        self.caps = set(caps) | {c.upper() for c in self.c.capabilities}
        self.vybrana: str | None = None

    def _ok(self, otvet, chto: str):
        typ, dat = otvet
        if typ != "OK":
            raise WebmailError(code="mail_imap_failed", message=f"IMAP {chto}: {_kratko(dat)}")
        return dat

    def zakryt(self) -> None:
        try:
            self.c.logout()
        except Exception:  # noqa: BLE001 — дело сделано, разрыв связи не новость
            pass

    # папки
    def papki(self) -> list[Papka]:
        if "LIST-STATUS" in self.caps:
            self.c.untagged_responses.pop("STATUS", None)
            otvet = self.c._simple_command(
                "LIST", '""', '"*"', "RETURN", "(SPECIAL-USE STATUS (MESSAGES UNSEEN))"
            )
            self._ok(otvet, "LIST")
            spisok = self.c._untagged_response(otvet[0], otvet[1], "LIST")[1]
            statusy = self.c.untagged_responses.pop("STATUS", [])
        else:
            spisok = self._ok(self.c.list('""', '"*"'), "LIST")
            statusy = None
        papki: dict[str, Papka] = {}
        znach = razobrat(spisok)
        for i in range(0, len(znach) - 2, 3):
            flagi, razdel, imya = znach[i], znach[i + 1], znach[i + 2]
            flags = frozenset(_str(f).lower() for f in flagi or [])
            path = mutf7_decode(_str(imya))
            papki[path] = Papka(path=path, delimiter=_str(razdel) or "/", flags=flags)
        if statusy is None:
            for papka in papki.values():
                if "\\noselect" in papka.flags or "\\nonexistent" in papka.flags:
                    continue
                otvet = self.c._simple_command(
                    "STATUS", imya_papki(papka.path), "(MESSAGES UNSEEN)"
                )
                if otvet[0] == "OK":
                    statusy = (statusy or []) + self.c._untagged_response(
                        otvet[0], otvet[1], "STATUS"
                    )[1]
        znach = razobrat(statusy or [])
        for i in range(0, len(znach) - 1, 2):
            path = mutf7_decode(_str(znach[i]))
            schyot = znach[i + 1] if isinstance(znach[i + 1], list) else []
            pary = {_str(schyot[j]).upper(): _str(schyot[j + 1]) for j in range(0, len(schyot) - 1, 2)}
            if path in papki:
                papki[path].total = int(pary.get("MESSAGES") or 0)
                papki[path].unseen = int(pary.get("UNSEEN") or 0)
        return list(papki.values())

    def vybrat(self, path: str, *, tolko_chtenie: bool = True) -> int:
        typ, dat = self.c.select(imya_papki(path), readonly=tolko_chtenie)
        if typ != "OK":
            raise WebmailError(code="mail_folder_bad", message=f"IMAP SELECT {path}: {_kratko(dat)}")
        self.vybrana = path
        return int(_str(dat[0]) or 0) if dat and dat[0] else 0

    def sozdat(self, path: str) -> None:
        self._papka_ok(self.c.create(imya_papki(path)), "CREATE", path)
        try:
            self.c.subscribe(imya_papki(path))
        except imaplib.IMAP4.error:
            pass

    def pereimenovat(self, path: str, novyy: str) -> None:
        self._papka_ok(self.c.rename(imya_papki(path), imya_papki(novyy)), "RENAME", path)

    def udalit_papku(self, path: str) -> None:
        self._papka_ok(self.c.delete(imya_papki(path)), "DELETE", path)
        try:
            self.c.unsubscribe(imya_papki(path))
        except imaplib.IMAP4.error:
            pass

    def _papka_ok(self, otvet, chto: str, path: str) -> None:
        typ, dat = otvet
        if typ != "OK":
            raise WebmailError(code="mail_folder_bad", message=f"IMAP {chto} {path}: {_kratko(dat)}")

    # письма
    def uidy(self, poisk: Poisk) -> list[int]:
        """UID писем выбранной папки, новые сверху."""
        kriterii: list[str] = []
        if poisk.unread is True:
            kriterii.append("UNSEEN")
        elif poisk.unread is False:
            kriterii.append("SEEN")
        if poisk.flagged is True:
            kriterii.append("FLAGGED")
        elif poisk.flagged is False:
            kriterii.append("UNFLAGGED")
        q = " ".join((poisk.q or "").split())
        if q:
            # Строку поиска — литералом: UTF-8 в кавычках IMAP не пропускает.
            kriterii.append({"from": "FROM", "subject": "SUBJECT"}.get(poisk.pole, "TEXT"))
            self.c.literal = q.encode("utf-8")
        if not kriterii:
            kriterii.append("ALL")
        if "SORT" in self.caps:
            dat = self._ok(self.c.uid("SORT", "(REVERSE ARRIVAL)", "UTF-8", *kriterii), "SORT")
            return [int(x) for x in _str(dat[0] if dat else b"").split()]
        if q:
            dat = self._ok(self.c.uid("SEARCH", "CHARSET", "UTF-8", *kriterii), "SEARCH")
        else:
            dat = self._ok(self.c.uid("SEARCH", *kriterii), "SEARCH")
        # Без SORT порядок прихода — это порядок UID: сервер выдаёт их по возрастанию.
        return sorted((int(x) for x in _str(dat[0] if dat else b"").split()), reverse=True)

    def svodki(self, uids: list[int]) -> list[Svodka]:
        if not uids:
            return []
        polya = "UID FLAGS RFC822.SIZE INTERNALDATE ENVELOPE BODYSTRUCTURE"
        if "PREVIEW" in self.caps:
            polya += " PREVIEW"
        vyhod: dict[int, Svodka] = {}
        for i in range(0, len(uids), FETCH_PACHKA):
            pachka = uids[i : i + FETCH_PACHKA]
            dat = self._ok(self.c.uid("FETCH", _diapazon(pachka), f"({polya})"), "FETCH")
            for attrs in fetch_atributy(dat):
                svodka = svodka_iz_fetch(attrs)
                vyhod[svodka.uid] = svodka
        return [vyhod[u] for u in uids if u in vyhod]

    def syroe(self, uid: int) -> tuple[bytes, frozenset[str]] | None:
        """Письмо целиком, без отметки «прочитано» (её ставит вызывающий явно)."""
        dat = self._ok(self.c.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])"), "FETCH")
        for attrs in fetch_atributy(dat):
            if int(attrs["UID"]) != uid:
                continue
            telo = attrs.get("BODY[]")
            if telo is None:
                return None
            flags = frozenset(_str(f) for f in attrs.get("FLAGS") or [])
            return (telo if isinstance(telo, bytes) else _str(telo).encode()), flags
        return None

    # архив писем в CRM (core/services/mail_archive.py)
    def sostoyanie(self) -> tuple[int, int]:
        """UIDVALIDITY и UIDNEXT выбранной папки — из ответа SELECT; 0 — сервер не назвал."""
        def chislo(kod: str) -> int:
            dat = self.c.untagged_responses.get(kod) or []
            try:
                return int(_str(dat[-1]).split()[0]) if dat else 0
            except (ValueError, IndexError):
                return 0

        return chislo("UIDVALIDITY"), chislo("UIDNEXT")

    def uidy_ot(self, ot: int) -> list[int]:
        """UID выбранной папки от `ot`, по возрастанию.

        `N:*` при пустом хвосте отдаёт последнее письмо (RFC 3501 §6.4.8): его отсеиваем.
        """
        ot = max(1, int(ot))
        dat = self._ok(self.c.uid("SEARCH", f"UID {ot}:*"), "SEARCH")
        return sorted(u for u in (int(x) for x in _str(dat[0] if dat else b"").split()) if u >= ot)

    def opisi(self, uids: list[int] | None = None) -> dict[int, tuple[frozenset[str], datetime | None, int]]:
        """Флаги, INTERNALDATE и размер писем; `None` — всей выбранной папки."""
        if uids is not None and not uids:
            return {}
        nabor = "1:*" if uids is None else _diapazon(uids)
        dat = self._ok(self.c.uid("FETCH", nabor, "(UID FLAGS INTERNALDATE RFC822.SIZE)"), "FETCH")
        vyhod = {}
        for attrs in fetch_atributy(dat):
            vyhod[int(attrs["UID"])] = (
                frozenset(_str(f) for f in attrs.get("FLAGS") or []),
                _internaldate(attrs.get("INTERNALDATE")),
                int(_str(attrs.get("RFC822.SIZE")) or 0),
            )
        return vyhod

    def flagi(self, uids: list[int], dobavit: set[str] = frozenset(), ubrat: set[str] = frozenset()) -> None:
        if not uids:
            return
        if dobavit:
            self._ok(self.c.uid("STORE", _diapazon(uids), "+FLAGS.SILENT", f"({' '.join(sorted(dobavit))})"), "STORE")
        if ubrat:
            self._ok(self.c.uid("STORE", _diapazon(uids), "-FLAGS.SILENT", f"({' '.join(sorted(ubrat))})"), "STORE")

    def peremestit(self, uids: list[int], kuda: str) -> None:
        if not uids:
            return
        if "MOVE" in self.caps:
            self._ok(self.c.uid("MOVE", _diapazon(uids), imya_papki(kuda)), "MOVE")
            return
        self._ok(self.c.uid("COPY", _diapazon(uids), imya_papki(kuda)), "COPY")
        self.stert(uids)

    def stert(self, uids: list[int]) -> None:
        """Насовсем: \\Deleted и вычеркнуть ровно эти письма."""
        if not uids:
            return
        self.flagi(uids, dobavit={"\\Deleted"})
        if "UIDPLUS" in self.caps:
            self._ok(self.c.uid("EXPUNGE", _diapazon(uids)), "EXPUNGE")
        else:
            self._ok(self.c.expunge(), "EXPUNGE")

    def dobavit(self, path: str, pismo: bytes, flags: set[str]) -> int | None:
        """APPEND; UID нового письма, если сервер его назвал (UIDPLUS)."""
        typ, dat = self.c.append(
            imya_papki(path), f"({' '.join(sorted(flags))})", imaplib.Time2Internaldate(time.time()), pismo
        )
        if typ != "OK":
            raise WebmailError(code="mail_imap_failed", message=f"IMAP APPEND {path}: {_kratko(dat)}")
        najdeno = re.search(rb"\[APPENDUID \d+ (\d+)\]", b" ".join(x for x in dat if isinstance(x, bytes)))
        return int(najdeno.group(1)) if najdeno else None


def _kratko(otvet) -> str:
    if isinstance(otvet, (list, tuple)):
        otvet = b" ".join(x for x in otvet if isinstance(x, bytes))
    return _str(otvet)[:200]


# --- клиент SMTP -------------------------------------------------------------

class SmtpKlient:
    def __init__(self, server: smtplib.SMTP):
        self.s = server

    def otpravit(self, ot: str, komu: list[str], pismo: bytes) -> dict[str, str]:
        """Отправить; вернуть отказавших (часть адресатов). Отказ всем — исключение."""
        try:
            otkazali = self.s.sendmail(ot, komu, pismo)
        except smtplib.SMTPRecipientsRefused as exc:
            raise WebmailError(code="mail_smtp_failed", message=f"SMTP: all recipients refused: {_otkazy(exc.recipients)}") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise WebmailError(code="mail_smtp_failed", message=f"SMTP: {exc}") from exc
        return _otkazy_slovar(otkazali)

    def zakryt(self) -> None:
        try:
            self.s.quit()
        except Exception:  # noqa: BLE001 — письмо ушло, разрыв связи не новость
            pass


def _otkazy_slovar(otkazali: dict) -> dict[str, str]:
    vyhod = {}
    for adres, otvet in (otkazali or {}).items():
        if isinstance(otvet, tuple) and len(otvet) == 2:
            vyhod[_str(adres)] = f"{otvet[0]} {_str(otvet[1])}"[:300]
        else:
            vyhod[_str(adres)] = _str(otvet)[:300]
    return vyhod


def _otkazy(otkazali: dict) -> str:
    return "; ".join(f"{a}: {t}" for a, t in _otkazy_slovar(otkazali).items())[:400]


# --- фабрики (шов для тестов) ------------------------------------------------

def _imap_po_umolchaniyu(n: Nastroyki) -> ImapKlient:
    try:
        if n.imap_ssl:
            c: imaplib.IMAP4 = _Imap4Tls(
                n.imap_host, n.imap_port, tls_name=n.tls_name, tls_pin=n.tls_pin, timeout=TIMEOUT_SECONDS
            )
        else:
            # Без TLS не ходим вовсе: STARTTLS обязателен, как в `mail_transport`.
            c = imaplib.IMAP4(n.imap_host, n.imap_port, timeout=TIMEOUT_SECONDS)
            c.starttls(ssl_context=ssl.create_default_context())
        c.login(n.login, n.password)
    except (OSError, imaplib.IMAP4.error, socket.timeout) as exc:
        raise WebmailError(code="mail_imap_failed", message=f"IMAP: {exc}") from exc
    try:
        return ImapKlient(c)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise WebmailError(code="mail_imap_failed", message=f"IMAP: {exc}") from exc


def _smtp_po_umolchaniyu(n: Nastroyki) -> SmtpKlient:
    try:
        if n.smtp_ssl:
            s: smtplib.SMTP = _SmtpTls(
                n.smtp_host, n.smtp_port, tls_name=n.tls_name, tls_pin=n.tls_pin, timeout=TIMEOUT_SECONDS
            )
        else:
            s = smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=TIMEOUT_SECONDS)
            s.starttls(context=ssl.create_default_context())
        s.login(n.login, n.password)
    except (OSError, smtplib.SMTPException) as exc:
        raise WebmailError(code="mail_smtp_failed", message=f"SMTP: {exc}") from exc
    return SmtpKlient(s)


_imap_factory: Callable[[Nastroyki], ImapKlient] = _imap_po_umolchaniyu
_smtp_factory: Callable[[Nastroyki], SmtpKlient] = _smtp_po_umolchaniyu


def set_imap_factory(factory: Callable[[Nastroyki], ImapKlient]) -> None:
    global _imap_factory
    _imap_factory = factory


def reset_imap_factory() -> None:
    global _imap_factory
    _imap_factory = _imap_po_umolchaniyu


def set_smtp_factory(factory: Callable[[Nastroyki], SmtpKlient]) -> None:
    global _smtp_factory
    _smtp_factory = factory


def reset_smtp_factory() -> None:
    global _smtp_factory
    _smtp_factory = _smtp_po_umolchaniyu


@contextmanager
def imap(n: Nastroyki) -> Iterator[ImapKlient]:
    klient = _imap_factory(n)
    try:
        yield klient
    except (OSError, imaplib.IMAP4.error) as exc:
        raise WebmailError(code="mail_imap_failed", message=f"IMAP: {exc}") from exc
    finally:
        klient.zakryt()


@contextmanager
def smtp(n: Nastroyki) -> Iterator[SmtpKlient]:
    klient = _smtp_factory(n)
    try:
        yield klient
    finally:
        klient.zakryt()
