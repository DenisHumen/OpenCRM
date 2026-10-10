"""Агент OpenCRM внутри контейнера docker-mailserver (docs/bloki/32-pochtovyy-server.md).

Работает ВНУТРИ контейнера почтового сервера и знает только его: `setup`,
`doveadm`, файлы `/tmp/docker-mailserver`. Только stdlib и Python ≥ 3.9 — в
образе DMS свой python3, ставить туда ничего нельзя.

Два входа, одни операции:

- `python3 -c <загрузчик> <op>` через `docker exec -i` (сервер по SSH): первая
  строка stdin — base64(JSON) аргументов, остальное — текст агента
  (`mail_host.ZAGRUZCHIK`). На машине агент не лежит вовсе, поэтому его версия
  всегда равна версии CRM, а пароли не попадают в argv — его видно в `ps`.
- `python3 agent.py serve` — HTTP на 8790 внутри сети compose (сервер на этой же
  машине: у приложения нет доступа к docker и не должно быть).

Ответ — одна строка JSON: `{"ok": true, "data": {...}}` или
`{"ok": false, "code": "...", "message": "..."}`. Пароли идут в `setup` и
`doveadm` через stdin (DMS спрашивает пароль дважды) и нигде не печатаются.
"""

import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header

AGENT_VERSION = 1
CONFIG = "/tmp/docker-mailserver"
TOKEN_FILE = CONFIG + "/opencrm/agent.token"
MAIL_ROOT = "/var/mail"
PORT = 8790
MAX_BODY = 1024 * 1024
TIMEOUT = 120

ADRES = re.compile(r"^(?![-.])[a-z0-9._+-]{1,64}@(?![-.])[a-z0-9.-]{1,253}$")
DOMEN = re.compile(r"^(?![-.])[a-z0-9.-]{1,253}$")
IMYA = re.compile(r"^(?![-.])[a-z0-9._-]{1,64}$")
HOST = re.compile(r"^(?![-.])[a-z0-9.-]{1,253}$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Otkaz(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# --- обёртки ---


def _zapustit(argv, *, stdin=None, timeout=TIMEOUT, proverit=True):
    """Команда без shell. Без своего stdin — /dev/null: иначе она дочитала бы наш."""
    vvod = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            **vvod,
        )
    except FileNotFoundError:
        raise Otkaz("mail_agent_failed", "command not found: %s" % argv[0])
    except subprocess.TimeoutExpired:
        raise Otkaz("mail_agent_failed", "command timed out: %s %s" % (argv[0], argv[1] if len(argv) > 1 else ""))
    if proverit and r.returncode != 0:
        hvost = ANSI.sub("", r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise Otkaz("mail_agent_failed", "%s failed: %s" % (" ".join(argv[:3]), " | ".join(hvost)[:400]))
    return r


def _setup(*args):
    return _zapustit(["setup"] + list(args))


def _setup_s_parolem(parol, *args):
    """Пароль — через stdin, дважды (ввод и подтверждение): в argv его видно в `ps`
    и в журнале auditd (проверено на DMS 16.0.1 10.10.2026)."""
    return _zapustit(["setup"] + list(args), stdin="%s\n%s\n" % (parol, parol))


def _adres(znachenie, code="mail_local_part_bad"):
    adres = str(znachenie or "").strip().lower()
    if not ADRES.match(adres) or ".." in adres:
        raise Otkaz(code, "bad address")
    return adres


def _domen(znachenie):
    domen = str(znachenie or "").strip().lower().rstrip(".")
    if not DOMEN.match(domen) or "." not in domen or ".." in domen:
        raise Otkaz("mail_domain_bad", "bad domain")
    return domen


def _parol(znachenie):
    parol = str(znachenie or "")
    # Пробел и перевод строки ломают построчные файлы DMS, `|` — разделитель в них.
    # не-секрет: пароль сравнивается сам с собой (без пробелов по краям), а не с чужим.
    if len(parol) < 6 or len(parol) > 200 or any(c in parol for c in "\r\n|") or parol != parol.strip():
        raise Otkaz("mail_password_weak", "password is not acceptable")
    return parol


def _stroki(imya):
    try:
        with open(os.path.join(CONFIG, imya), encoding="utf-8", errors="replace") as f:
            return [s.strip() for s in f if s.strip() and not s.lstrip().startswith("#")]
    except OSError:
        return []


def _zapisat_stroki(imya, stroki):
    """Целиком и подменой: следящий за файлами DMS не должен прочесть половину."""
    put = os.path.join(CONFIG, imya)
    vremennyy = put + ".opencrm-tmp"
    with open(vremennyy, "w", encoding="utf-8") as f:
        for s in stroki:
            f.write(s + "\n")
    try:
        st = os.stat(put)
        os.chown(vremennyy, st.st_uid, st.st_gid)
        os.chmod(vremennyy, st.st_mode & 0o777)
    except OSError:
        pass
    os.replace(vremennyy, put)


def _yashchiki():
    return [s.split("|", 1)[0].strip().lower() for s in _stroki("postfix-accounts.cf") if "|" in s]


def _kvoty():
    itog = {}
    for s in _stroki("dovecot-quotas.cf"):
        adres, _, kvota = s.partition(":")
        itog[adres.strip().lower()] = kvota.strip()
    return itog


def _v_mb(kvota):
    najdeno = re.match(r"^(\d+)\s*([KMGT]?)B?$", (kvota or "").strip().upper())
    if not najdeno:
        return None
    chislo, ed = int(najdeno.group(1)), najdeno.group(2)
    return {"": chislo // (1024 * 1024), "K": chislo // 1024, "M": chislo, "G": chislo * 1024, "T": chislo * 1024 * 1024}[ed]


def _psevdonimy():
    itog = []
    for s in _stroki("postfix-virtual.cf"):
        chasti = s.split(None, 1)
        if len(chasti) != 2:
            continue
        for kuda in chasti[1].replace(" ", "").split(","):
            if kuda:
                itog.append({"source": chasti[0].lower(), "destination": kuda.lower()})
    return itog


def _mastera():
    return [s.split("|", 1)[0].strip() for s in _stroki("dovecot-masters.cf") if "|" in s]


def _tab(vyvod):
    """Таблица `doveadm -f tab`: первая строка — имена столбцов, порядок их doveadm выбирает сам."""
    stroki = [s for s in vyvod.splitlines() if s.strip()]
    if not stroki:
        return []
    shapka = [x.strip().lower() for x in stroki[0].split("\t")]
    return [dict(zip(shapka, s.split("\t"))) for s in stroki[1:]]


def _maildir(adres):
    imya, _, domen = adres.partition("@")
    return os.path.join(MAIL_ROOT, domen, imya)


def _est_pisma(put):
    """Каталог без писем бывает и у несуществующего ящика: `doveadm … -A` создаёт его
    любому адресу, пока DMS не перечитал список (пара секунд после удаления)."""
    for koren, _papki, fayly in os.walk(put):
        if fayly and os.path.basename(koren) in ("cur", "new"):
            return True
    return False


def _vladelets_pochty():
    """Чей каталог /var/mail: у DMS это docker:docker (5000), но смотрим, а не помним."""
    try:
        st = os.stat(MAIL_ROOT)
        return st.st_uid, st.st_gid
    except OSError:
        return 5000, 5000


def _dozhdatsya(uslovie, sekund=15.0):
    """DMS подхватывает правку файлов не сразу (DMS_CONFIG_POLL); ждём, а не надеемся."""
    konets = time.monotonic() + sekund
    while time.monotonic() < konets:
        if uslovie():
            return True
        time.sleep(0.5)
    return uslovie()


def _dovecot_znaet(adres):
    r = _zapustit(["doveadm", "user", adres], proverit=False, timeout=20)
    return r.returncode == 0


def _vhod_rabotaet(adres, parol):
    """Новый пароль принят Dovecot. Без этой проверки человек, сразу вбивший
    показанный пароль в телефон, 5–8 секунд получал отказ (проба 09.10.2026)."""
    _zapustit(["doveadm", "auth", "cache", "flush"], proverit=False, timeout=20)
    r = _zapustit(["doveadm", "auth", "test", "-x", "service=imap", adres], stdin=parol + "\n",
                  proverit=False, timeout=20)
    return r.returncode == 0


def _postfix_znaet(istochnik, kuda):
    """Псевдоним дошёл до таблиц Postfix: до этого письмо на него отскакивает."""
    karty = _zapustit(["postconf", "-h", "virtual_alias_maps"], proverit=False).stdout
    for karta in re.split(r"[\s,]+", karty.strip()):
        if not karta or karta.startswith("$"):
            continue
        r = _zapustit(["postmap", "-q", istochnik, karta], proverit=False, timeout=20)
        if r.returncode == 0 and kuda in r.stdout:
            return True
    return False


# --- операции ---


def op_ping(_args):
    def odna(argv):
        try:
            r = _zapustit(argv, proverit=False, timeout=20)
            return (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else ""
        except Otkaz:
            return ""

    return {
        "agent": AGENT_VERSION,
        "dms": os.environ.get("DMS_RELEASE", ""),
        "postfix": odna(["postconf", "-h", "mail_version"]),
        "dovecot": odna(["dovecot", "--version"]).split(" ")[0],
        "hostname": odna(["hostname", "-f"]),
    }


def op_accounts(_args):
    kvoty = _kvoty()
    ispolzovano = {}
    try:
        r = _zapustit(["doveadm", "-f", "tab", "quota", "get", "-A"], proverit=False, timeout=60)
        for s in _tab(r.stdout):
            if s.get("type") == "STORAGE":
                try:
                    ispolzovano[s.get("username", "").lower()] = int(s.get("value") or 0)
                except ValueError:
                    pass
    except Otkaz:
        pass
    yashchiki = _yashchiki()
    psevdonimy = _psevdonimy()
    domeny = sorted({a.split("@", 1)[1] for a in yashchiki} | {p["source"].split("@", 1)[-1] for p in psevdonimy if "@" in p["source"]})
    return {
        "accounts": [
            {"address": a, "quota_mb": _v_mb(kvoty.get(a)), "used_kb": ispolzovano.get(a)}
            for a in sorted(yashchiki)
        ],
        "aliases": psevdonimy,
        "domains": domeny,
        "masters": _mastera(),
    }


def _kvota_postavit(adres, quota_mb):
    if quota_mb in (None, "", 0):
        if adres in _kvoty():
            _setup("quota", "del", adres)
        return
    try:
        mb = int(quota_mb)
    except (TypeError, ValueError):
        raise Otkaz("mail_quota_bad", "bad quota")
    if mb < 1 or mb > 10 * 1024 * 1024:
        raise Otkaz("mail_quota_bad", "bad quota")
    _setup("quota", "set", adres, "%dM" % mb)


def op_account_add(args):
    adres = _adres(args.get("address"))
    parol = _parol(args.get("password"))
    if adres in _yashchiki():
        raise Otkaz("mail_box_exists", "mailbox already exists")
    if any(p["source"] == adres for p in _psevdonimy()):
        raise Otkaz("mail_alias_exists", "address is an alias")
    _setup_s_parolem(parol, "email", "add", adres)
    if args.get("quota_mb"):
        _kvota_postavit(adres, args.get("quota_mb"))
    # Dovecot узнаёт о ящике, когда DMS перечитает файлы (~2 с); вызывающий сразу
    # за этим входит в ящик — без ожидания получил бы AUTHORIZATIONFAILED.
    _dozhdatsya(lambda: _dovecot_znaet(adres), 15)
    return {}


def op_account_password(args):
    adres = _adres(args.get("address"))
    parol = _parol(args.get("password"))
    if adres not in _yashchiki():
        raise Otkaz("mail_box_not_on_server", "mailbox not found")
    _setup_s_parolem(parol, "email", "update", adres)
    _dozhdatsya(lambda: _vhod_rabotaet(adres, parol), 15)
    return {}


def _otlozhit_pochtu(adres):
    """Убрать письма ящика в сторону, а не стереть: удалить «с сохранением» просили буквально."""
    istochnik = _maildir(adres)
    if not os.path.isdir(istochnik):
        return ""
    kuda = os.path.join(MAIL_ROOT, ".opencrm-deleted")
    os.makedirs(kuda, exist_ok=True)
    tsel = os.path.join(kuda, "%s-%s" % (adres, datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    shutil.move(istochnik, tsel)
    return tsel


def _udalit(adres, *, steret):
    """`setup email del -y` падает, если каталога писем нет (ящик ни разу не открывали)."""
    flag = "-y" if steret and os.path.isdir(_maildir(adres)) else "-n"
    _setup("email", "del", flag, adres)


def op_account_delete(args):
    adres = _adres(args.get("address"))
    if adres not in _yashchiki():
        raise Otkaz("mail_box_not_on_server", "mailbox not found")
    sohraneno = ""
    if args.get("keep_data"):
        _zapustit(["doveadm", "kick", adres], proverit=False, timeout=20)
        sohraneno = _otlozhit_pochtu(adres)
    _udalit(adres, steret=not args.get("keep_data"))
    return {"kept": sohraneno}


def op_account_rename(args):
    staryy = _adres(args.get("old"))
    novyy = _adres(args.get("new"))
    parol = _parol(args.get("password"))
    yashchiki = _yashchiki()
    if staryy not in yashchiki:
        raise Otkaz("mail_box_not_on_server", "mailbox not found")
    if novyy in yashchiki or any(p["source"] == novyy for p in _psevdonimy()):
        raise Otkaz("mail_box_exists", "target address is taken")
    kvota = _v_mb(_kvoty().get(staryy))
    tsel = _maildir(novyy)
    if _est_pisma(tsel):
        raise Otkaz("mail_box_exists", "target maildir is not empty")
    # `email del` снимает и псевдонимы, ведущие в ящик, — их переводим на новый адрес.
    vedut = [p["source"] for p in _psevdonimy() if p["destination"] == staryy and p["source"] != novyy]
    _setup_s_parolem(parol, "email", "add", novyy)
    # Перенос каталога, а не копия через IMAP: письма, флаги и UID остаются, и
    # ящик на гигабайты переезжает мгновенно — это один и тот же раздел.
    _zapustit(["doveadm", "kick", staryy], proverit=False, timeout=20)
    istochnik = _maildir(staryy)
    try:
        if os.path.isdir(istochnik):
            if os.path.isdir(tsel):
                shutil.rmtree(tsel)
            os.makedirs(os.path.dirname(tsel), exist_ok=True)
            uid, gid = _vladelets_pochty()
            try:
                os.chown(os.path.dirname(tsel), uid, gid)
            except OSError:
                pass
            shutil.move(istochnik, tsel)
    except OSError as beda:
        _udalit(novyy, steret=False)
        raise Otkaz("mail_agent_failed", "maildir was not moved: %s" % beda)
    _udalit(staryy, steret=False)
    if kvota:
        _kvota_postavit(novyy, kvota)
    for istochnik_psevdonima in vedut:
        _setup("alias", "add", istochnik_psevdonima, novyy)
    if args.get("keep_alias"):
        _setup("alias", "add", staryy, novyy)
    _dozhdatsya(lambda: _dovecot_znaet(novyy), 10)
    _zapustit(["doveadm", "force-resync", "-u", novyy, "*"], proverit=False, timeout=60)
    return {}


def op_account_quota(args):
    adres = _adres(args.get("address"))
    if adres not in _yashchiki():
        raise Otkaz("mail_box_not_on_server", "mailbox not found")
    _kvota_postavit(adres, args.get("quota_mb"))
    return {}


def _para_psevdonima(args):
    istochnik = _adres(args.get("source"), "mail_alias_bad")
    kuda = _adres(args.get("destination"), "mail_alias_bad")
    if istochnik == kuda:
        raise Otkaz("mail_alias_bad", "alias points to itself")
    return istochnik, kuda


def op_alias_add(args):
    istochnik, kuda = _para_psevdonima(args)
    if istochnik in _yashchiki():
        raise Otkaz("mail_alias_bad", "source is a mailbox")
    if {"source": istochnik, "destination": kuda} in _psevdonimy():
        raise Otkaz("mail_alias_exists", "alias already exists")
    _setup("alias", "add", istochnik, kuda)
    _dozhdatsya(lambda: _postfix_znaet(istochnik, kuda), 15)
    return {}


def op_alias_delete(args):
    istochnik, kuda = _para_psevdonima(args)
    if {"source": istochnik, "destination": kuda} not in _psevdonimy():
        return {"missing": True}
    _setup("alias", "del", istochnik, kuda)
    return {}


def op_master_ensure(args):
    user = str(args.get("user") or "").strip()
    if not IMYA.match(user):
        raise Otkaz("mail_agent_failed", "bad master user")
    parol = _parol(args.get("password"))
    _setup_s_parolem(parol, "dovecot-master", "update" if user in _mastera() else "add", user)
    return {}


def _dkim_fayl(domen):
    return os.path.join(CONFIG, "rspamd", "dkim", "rsa-2048-mail-%s.public.dns.txt" % domen)


def _dkim_txt(tekst):
    """«v=DKIM1; k=rsa; p=…» одной строкой — как бы ни был разбит файл кавычками и скобками."""
    kuski = re.findall(r'"([^"]*)"', tekst)
    stroka = "".join(kuski) if kuski else tekst
    stroka = re.sub(r"\s+", " ", stroka.replace("(", " ").replace(")", " ")).strip()
    najdeno = re.search(r"v=DKIM1.*", stroka)
    return (najdeno.group(0) if najdeno else stroka).strip().rstrip(";").strip()


def op_dkim(args):
    domen = _domen(args.get("domain"))
    put = _dkim_fayl(domen)
    if not os.path.exists(put):
        _setup("config", "dkim", "keysize", "2048", "domain", domen)
    try:
        with open(put, encoding="utf-8") as f:
            txt = _dkim_txt(f.read())
    except OSError:
        raise Otkaz("dkim_failed", "dkim key was not created")
    return {"selector": "mail", "txt": txt}


def op_status(_args):
    yashchiki = set(_yashchiki())
    r = _zapustit(["doveadm", "-f", "tab", "mailbox", "status", "-A", "messages unseen uidnext uidvalidity", "INBOX"], timeout=90)
    itog = []
    for s in _tab(r.stdout):
        adres = (s.get("username") or "").lower()
        if adres not in yashchiki:
            continue
        try:
            itog.append({
                "address": adres,
                "messages": int(s.get("messages") or 0),
                "unseen": int(s.get("unseen") or 0),
                "uidnext": int(s.get("uidnext") or 0),
                "uidvalidity": int(s.get("uidvalidity") or 0),
            })
        except ValueError:
            continue
    return {"boxes": itog}


def _zagolovok(tekst):
    try:
        return str(make_header(decode_header(tekst or ""))).strip()
    except Exception:  # noqa: BLE001 — кривой заголовок не повод терять письмо
        return (tekst or "").strip()


def op_headers(args):
    adres = _adres(args.get("address"))
    if adres not in _yashchiki():
        raise Otkaz("mail_box_not_on_server", "mailbox not found")
    try:
        posle = max(0, int(args.get("since_uid") or 0))
        limit = min(50, max(1, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        raise Otkaz("mail_agent_failed", "bad arguments")
    r = _zapustit(
        ["doveadm", "-f", "json", "fetch", "-u", adres, "uid date.received hdr.from hdr.subject",
         "mailbox", "INBOX", "uid", "%d:*" % (posle + 1)],
        timeout=60,
    )
    try:
        zapisi = json.loads(r.stdout or "[]")
    except ValueError:
        zapisi = []
    itog = []
    for z in zapisi:
        try:
            uid = int(z.get("uid") or 0)
        except ValueError:
            continue
        # `N:*` при N больше последнего UID отдаёт последнее письмо — так велит IMAP.
        if uid <= posle:
            continue
        itog.append({
            "uid": uid,
            "from": _zagolovok(z.get("hdr.from")),
            "subject": _zagolovok(z.get("hdr.subject")),
            "date": z.get("date.received") or "",
        })
    itog.sort(key=lambda x: x["uid"])
    return {"items": itog[-limit:]}


def _bez_strok_domena(imya, domen):
    stroki = _stroki(imya)
    ostalos = [s for s in stroki if s.split(None, 1)[0].lower() != "@" + domen]
    if len(ostalos) != len(stroki) or not os.path.exists(os.path.join(CONFIG, imya)):
        _zapisat_stroki(imya, ostalos)


def op_relay_set(args):
    domen = _domen(args.get("domain"))
    host = str(args.get("host") or "").strip().lower()
    if not HOST.match(host):
        raise Otkaz("mail_agent_failed", "bad relay host")
    try:
        port = int(args.get("port") or 587)
    except (TypeError, ValueError):
        raise Otkaz("mail_agent_failed", "bad relay port")
    if not 1 <= port <= 65535:
        raise Otkaz("mail_agent_failed", "bad relay port")
    user = str(args.get("user") or "").strip()
    if any(c in user for c in " \t\r\n:|"):
        raise Otkaz("mail_agent_failed", "bad relay user")
    _bez_strok_domena("postfix-relaymap.cf", domen)
    _bez_strok_domena("postfix-sasl-password.cf", domen)
    _setup("relay", "add-domain", domen, host, str(port))
    if user:
        parol = str(args.get("password") or "")
        if not parol or any(c in parol for c in "\r\n"):
            raise Otkaz("mail_relay_password_required", "relay password is required")
        _setup_s_parolem(parol, "relay", "add-auth", domen, user)
    return {}


def op_relay_clear(args):
    domen = _domen(args.get("domain"))
    _bez_strok_domena("postfix-relaymap.cf", domen)
    _bez_strok_domena("postfix-sasl-password.cf", domen)
    return {}


def _sertifikat():
    put = os.environ.get("SSL_CERT_PATH") or ""
    if not put or not os.path.exists(put):
        return {"not_after": "", "self_signed": False, "sha256": ""}
    r = _zapustit(["openssl", "x509", "-in", put, "-noout", "-enddate", "-subject", "-issuer", "-fingerprint", "-sha256"],
                  proverit=False, timeout=20)
    polya = {}
    for s in r.stdout.splitlines():
        k, _, v = s.partition("=")
        polya[k.strip().lower()] = v.strip()
    not_after = ""
    try:
        not_after = datetime.strptime(polya.get("notafter", ""), "%b %d %H:%M:%S %Y %Z").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        not_after = polya.get("notafter", "")
    otpechatok = ""
    for k, v in polya.items():
        if "fingerprint" in k:
            otpechatok = v.replace(":", "").lower()
    return {
        "not_after": not_after,
        "self_signed": bool(polya.get("subject")) and polya.get("subject") == polya.get("issuer"),
        "sha256": otpechatok,
    }


def op_health(_args):
    sluzhby = {"postfix": False, "dovecot": False, "rspamd": False}
    r = _zapustit(["supervisorctl", "status"], proverit=False, timeout=20)
    for s in r.stdout.splitlines():
        chasti = s.split()
        if len(chasti) >= 2 and chasti[0] in sluzhby:
            sluzhby[chasti[0]] = chasti[1] == "RUNNING"
    ochered = 0
    q = _zapustit(["postqueue", "-j"], proverit=False, timeout=20)
    ochered = len([s for s in q.stdout.splitlines() if s.strip().startswith("{")])
    return {"services": sluzhby, "cert": _sertifikat(), "queue": ochered}


def op_logs(args):
    try:
        lines = min(2000, max(1, int(args.get("lines") or 200)))
    except (TypeError, ValueError):
        lines = 200
    r = _zapustit(["tail", "-n", str(lines), "/var/log/mail/mail.log"], proverit=False, timeout=20)
    return {"text": r.stdout}


OPERATSII = {
    "ping": op_ping,
    "accounts": op_accounts,
    "account_add": op_account_add,
    "account_password": op_account_password,
    "account_delete": op_account_delete,
    "account_rename": op_account_rename,
    "account_quota": op_account_quota,
    "alias_add": op_alias_add,
    "alias_delete": op_alias_delete,
    "master_ensure": op_master_ensure,
    "dkim": op_dkim,
    "status": op_status,
    "headers": op_headers,
    "relay_set": op_relay_set,
    "relay_clear": op_relay_clear,
    "health": op_health,
    "logs": op_logs,
}

#: Операции идут по одной: две правки postfix-accounts.cf разом теряют одну из них.
_ZAMOK = threading.Lock()


def vypolnit(op, args):
    fn = OPERATSII.get(op)
    if fn is None:
        return {"ok": False, "code": "mail_agent_failed", "message": "unknown operation"}
    if not isinstance(args, dict):
        return {"ok": False, "code": "mail_agent_failed", "message": "arguments must be an object"}
    with _ZAMOK:
        try:
            return {"ok": True, "data": fn(args)}
        except Otkaz as beda:
            return {"ok": False, "code": beda.code, "message": beda.message}
        except Exception as beda:  # noqa: BLE001 — ответ обязан быть JSON, а не трассой
            return {"ok": False, "code": "mail_agent_failed", "message": "%s: %s" % (type(beda).__name__, str(beda)[:300])}


# --- HTTP (сервер на этой же машине) ---


def _token():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return f.read().strip()


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Obrabotchik(BaseHTTPRequestHandler):
        server_version = "opencrm-mail-agent/%d" % AGENT_VERSION

        def log_message(self, *_a):  # журнал запросов не нужен: в нём были бы адреса
            pass

        def _otvet(self, kod, telo):
            dannye = json.dumps(telo, ensure_ascii=False).encode("utf-8")
            self.send_response(kod)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dannye)))
            self.end_headers()
            self.wfile.write(dannye)

        def do_GET(self):
            self._otvet(405, {"ok": False, "code": "mail_agent_failed", "message": "use POST"})

        def do_POST(self):
            try:
                ozhidaemyy = _token()
            except OSError:
                self._otvet(503, {"ok": False, "code": "mail_agent_unreachable", "message": "agent token is missing"})
                return
            prishel = (self.headers.get("Authorization") or "")[len("Bearer "):].strip()
            if not ozhidaemyy or not hmac.compare_digest(prishel.encode(), ozhidaemyy.encode()):
                self._otvet(401, {"ok": False, "code": "mail_agent_unreachable", "message": "unauthorized"})
                return
            if not self.path.startswith("/v1/"):
                self._otvet(404, {"ok": False, "code": "mail_agent_failed", "message": "not found"})
                return
            try:
                dlina = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                dlina = -1
            if dlina < 0 or dlina > MAX_BODY:
                self._otvet(413, {"ok": False, "code": "mail_agent_failed", "message": "body too large"})
                return
            try:
                args = json.loads(self.rfile.read(dlina).decode("utf-8") or "{}") if dlina else {}
            except ValueError:
                self._otvet(400, {"ok": False, "code": "mail_agent_failed", "message": "bad json"})
                return
            self._otvet(200, vypolnit(self.path[len("/v1/"):], args))

    ThreadingHTTPServer(("0.0.0.0", PORT), Obrabotchik).serve_forever()


def po_stdin(op, stroka):
    """Вход по SSH (`mail_host.ZAGRUZCHIK`): аргументы пришли первой строкой stdin."""
    try:
        args = json.loads(base64.b64decode((stroka or b"").strip() or b"e30=").decode("utf-8"))
    except ValueError:
        print(json.dumps({"ok": False, "code": "mail_agent_failed", "message": "bad arguments"}))
        return 2
    print(json.dumps(vypolnit(op, args), ensure_ascii=False))
    return 0


def main(argv):
    """`python3 agent.py <op> [b64json]` — для человека у консоли; CRM аргументы
    в argv не кладёт (см. `po_stdin`)."""
    if len(argv) >= 2 and argv[1] == "serve":
        serve()
        return 0
    if len(argv) < 2:
        print(json.dumps({"ok": False, "code": "mail_agent_failed", "message": "usage: agent.py <op> [b64json]"}))
        return 2
    try:
        args = json.loads(base64.b64decode(argv[2]).decode("utf-8")) if len(argv) > 2 and argv[2] else {}
    except ValueError:
        print(json.dumps({"ok": False, "code": "mail_agent_failed", "message": "bad arguments"}))
        return 2
    print(json.dumps(vypolnit(argv[1], args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
