"""Записи DNS почтового домена: что завести и что уже заведено (docs/bloki/32-pochtovyy-server.md).

Без A, MX, SPF и DKIM Gmail письма с нового сервера в лучшем случае кладёт в спам,
а PTR задаёт только хостинг — поэтому каждая запись приходит с ожидаемым
значением, найденным и подсказкой, что делать. Спрашиваем публичные резолверы
(или `OPENCRM_MAIL_DNS`), а не системный: внутри docker он отвечает про свою сеть.

Сеть в тестах запрещена — шов `set_dns_resolver()`.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

REZOLVERY = ("1.1.1.1", "8.8.8.8")
TAYMAUT = 3.0
SELECTOR = "mail"

OK, MISSING, WRONG, WARN = "ok", "missing", "wrong", "warn"

#: Подсказки к записям: одна строка — что сделать, на двух языках.
PODSKAZKI = {
    "A": {
        "ru": "Заведите A-запись: имя сервера должно указывать на его публичный IP.",
        "en": "Add an A record: the server name must point to its public IP.",
    },
    "MX": {
        "ru": "Заведите MX-запись домена с приоритетом 10 на имя сервера.",
        "en": "Add an MX record for the domain with priority 10 pointing to the server name.",
    },
    "SPF": {
        "ru": "Заведите TXT-запись SPF; если SPF уже есть — добавьте в неё «mx», вторая SPF-запись ломает обе.",
        "en": "Add an SPF TXT record; if one exists, add “mx” to it — two SPF records break both.",
    },
    "DKIM": {
        "ru": "Заведите TXT-запись DKIM целиком, одной строкой, как показано.",
        "en": "Add the DKIM TXT record exactly as shown, as one string.",
    },
    "DMARC": {
        "ru": "Заведите TXT-запись DMARC: без неё Gmail и Яндекс доверяют письмам меньше.",
        "en": "Add a DMARC TXT record: without it Gmail and others trust your mail less.",
    },
    "PTR": {
        "ru": "Обратную запись (PTR) задаёт хостинг, и только после того, как A-запись имени сервера уже работает. "
              "Linode: Linodes → ваш сервер → Network → у IPv4 «⋯» → Edit RDNS → впишите имя сервера. "
              "У других — такая же настройка «Reverse DNS» в панели или просьба в поддержку.",
        "en": "Reverse DNS (PTR) is set by your hosting, and only once the A record of the server name works. "
              "Linode: Linodes → your server → Network → “⋯” at the IPv4 → Edit RDNS → enter the server name. "
              "Elsewhere — the same “Reverse DNS” setting in the panel or a support request.",
    },
}


def _po_umolchaniyu(imya: str, tip: str) -> list[str]:
    import dns.exception
    import dns.resolver

    r = dns.resolver.Resolver(configure=False)
    svoi = [a.strip() for a in os.environ.get("OPENCRM_MAIL_DNS", "").split(",") if a.strip()]
    r.nameservers = svoi or list(REZOLVERY)
    r.timeout = TAYMAUT
    r.lifetime = TAYMAUT
    try:
        if tip == "PTR":
            otvet = r.resolve_address(imya)
        else:
            otvet = r.resolve(imya, tip)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.DNSException:
        return []
    itog = []
    for zapis in otvet:
        if tip == "MX":
            itog.append(f"{zapis.preference} {str(zapis.exchange).rstrip('.').lower()}")
        elif tip == "TXT":
            itog.append(b"".join(zapis.strings).decode("utf-8", "replace"))
        else:
            itog.append(str(zapis).rstrip(".").lower())
    return itog


_resolver: Callable[[str, str], list[str]] = _po_umolchaniyu


def set_dns_resolver(fn: Callable[[str, str], list[str]]) -> None:
    """Шов для тестов: `fn(имя, тип) -> [значения]`, тип — A, MX, TXT, PTR."""
    global _resolver
    _resolver = fn


def reset_dns_resolver() -> None:
    global _resolver
    _resolver = _po_umolchaniyu


def _sprosit(imya: str, tip: str) -> list[str]:
    try:
        return list(_resolver(imya, tip))
    except Exception:  # noqa: BLE001 — молчащий DNS — это «не нашлось», а не падение экрана
        return []


def publichnyy_ip(timeout: float = 4.0) -> str:
    """Свой внешний адрес со слов ipify/ifconfig.me; пусто — не узнали."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            tekst = httpx.get(url, timeout=timeout).text.strip()
            ipaddress.ip_address(tekst)
            return tekst
        except (httpx.HTTPError, ValueError):
            continue
    return ""


def _bez_probelov(tekst: str) -> str:
    return re.sub(r"\s+", "", tekst or "").rstrip(";")


def _zapis(tip, imya, ozhidaem, naydeno, status, obyazatelna) -> dict:
    return {
        "type": tip,
        "name": imya,
        "expected": ozhidaem,
        "found": naydeno,
        "status": status,
        "required": obyazatelna,
        "hint": None if status == OK else PODSKAZKI[tip],
    }


def spf_ozhidaem() -> str:
    return "v=spf1 mx ~all"


def dmarc_ozhidaem() -> str:
    return "v=DMARC1; p=quarantine; adkim=r; aspf=r"


def proverit(hostname: str, domain: str, public_ip: str, dkim_txt: str, *, selector: str = SELECTOR) -> list[dict]:
    """Все записи домена разом, параллельно: экран ждёт не дольше одного таймаута."""
    hostname = (hostname or "").strip().lower().rstrip(".")
    domain = (domain or "").strip().lower().rstrip(".")
    voprosy = {
        "A": (hostname, "A"),
        "MX": (domain, "MX"),
        "SPF": (domain, "TXT"),
        "DKIM": (f"{selector}._domainkey.{domain}", "TXT"),
        "DMARC": (f"_dmarc.{domain}", "TXT"),
    }
    if public_ip:
        voprosy["PTR"] = (public_ip, "PTR")
    with ThreadPoolExecutor(max_workers=len(voprosy)) as pul:
        budushchie = {k: pul.submit(_sprosit, *v) for k, v in voprosy.items()}
        otvety = {k: f.result() for k, f in budushchie.items()}

    zapisi = []
    a = otvety["A"]
    if not a:
        status = MISSING
    elif public_ip and public_ip not in a:
        status = WRONG
    else:
        status = OK
    zapisi.append(_zapis("A", hostname, public_ip or "<server IP>", a, status, True))

    mx = otvety["MX"]
    hosty = [m.split(" ", 1)[-1] for m in mx]
    status = OK if hostname in hosty else (WRONG if mx else MISSING)
    zapisi.append(_zapis("MX", domain, f"10 {hostname}", mx, status, True))

    spf = [t for t in otvety["SPF"] if t.lower().startswith("v=spf1")]
    if not spf:
        status = MISSING
    elif len(spf) > 1:
        status = WRONG
    else:
        chasti = spf[0].lower().split()
        nash = (
            "mx" in chasti
            or f"a:{hostname}" in chasti
            or bool(public_ip and f"ip4:{public_ip}" in chasti)
            or bool(public_ip and f"ip6:{public_ip}" in chasti)
        )
        status = OK if nash else WRONG
    zapisi.append(_zapis("SPF", domain, spf_ozhidaem(), spf, status, True))

    dkim = otvety["DKIM"]
    if not dkim_txt:
        status = WARN if not dkim else OK
    elif not dkim:
        status = MISSING
    else:
        p_nado = re.search(r"p=([^;]+)", _bez_probelov(dkim_txt))
        status = WRONG
        for t in dkim:
            p_est = re.search(r"p=([^;]+)", _bez_probelov(t))
            if p_nado and p_est and p_est.group(1) == p_nado.group(1):
                status = OK
    zapisi.append(_zapis("DKIM", f"{selector}._domainkey.{domain}", dkim_txt or "", dkim, status, True))

    dmarc = [t for t in otvety["DMARC"] if t.lower().startswith("v=dmarc1")]
    zapisi.append(_zapis("DMARC", f"_dmarc.{domain}", dmarc_ozhidaem(), dmarc, OK if dmarc else WARN, False))

    if public_ip:
        ptr = otvety.get("PTR", [])
        zapisi.append(_zapis("PTR", public_ip, hostname, ptr, OK if hostname in ptr else WARN, False))
    return zapisi


def vsyo_v_poryadke(zapisi: list[dict]) -> bool:
    return all(z["status"] == OK for z in zapisi if z["required"])
