"""Блок «Глобус»: где точка каждого клиента, гостя и базы фирмы.

Правило одно: **хранится источник, а не вывод**. Центр страны и город
часового пояса — величины производные, и в базе им не место (`CLAUDE.md` §3).
В базе либо точка, поставленная рукой (`clients.lat_e7`), либо сырой признак
(`share_views.tz`); всё остальное считается здесь. Разбор —
`docs/bloki/25-globus.md`.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.geo.dannye import CENTRY_STRAN, POYASA
from core.services import modules_service, permissions_service, settings_service
from core.strany import KODY_STRAN
from core.utils import now_utc
from database.models import Client, User
from database.repositories import globus as globus_repo

#: Сколько точек уходит на планету. Больше — не карта, а туман: точки сливаются
#: раньше, чем кончается польза, а ответ растёт линейно.
PREDEL_TOCHEK = 500
#: Столько же гостей: лента справа показывает последние, а не всю историю.
PREDEL_GOSTEY = 200

#: Точность места, от лучшей к худшей.
TOCHNOST_TOCHKA = "tochka"
TOCHNOST_GOROD = "gorod"
TOCHNOST_STRANA = "strana"

#: Разброс точек внутри страны, в 1e-7 градуса (±2°). Порядок размера страны и
#: меньше расстояния между столицами: тридцать клиентов не лягут одной точкой,
#: но и не уедут к соседям.
RAZBROS = 20_000_000

#: Города пишут по-разному, а пояс назван одним написанием. Здесь только те
#: расхождения, которые встречаются в адресах чаще прочих; всё остальное честно
#: показывается по стране.
ALIASY_GORODOV = {
    "kiev": "kyiv", "kyyiv": "kyiv", "kijow": "kyiv",
    "moskva": "moscow", "moskau": "moscow",
    "warszawa": "warsaw", "varshava": "warsaw",
    "praha": "prague", "praga": "prague",
    "wien": "vienna", "vena": "vienna",
    "roma": "rome", "rim": "rome",
    "lisboa": "lisbon", "lissabon": "lisbon",
    "athina": "athens", "afiny": "athens",
    "bucuresti": "bucharest", "buharest": "bucharest",
    "kobenhavn": "copenhagen", "kopengagen": "copenhagen",
    "beograd": "belgrade", "belgrad": "belgrade",
    "chisinau": "chisinau", "kishinev": "chisinau",
    "tbilisi": "tbilisi", "tiflis": "tbilisi",
    "nyu-york": "new york", "nyuyork": "new york",
    "london": "london", "londyn": "london",
    "berlin": "berlin", "myunhen": "berlin",
}

#: Кириллица в латиницу — чтобы «Киев» и «Kyiv» встретились в одном ключе.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "ё": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}


def _klyuch_goroda(nazvanie: str) -> str:
    """Название города в сравнимый вид: латиница, без знаков и регистра."""
    bukvy = []
    for znak in (nazvanie or "").strip().lower():
        if znak in TRANSLIT:
            bukvy.append(TRANSLIT[znak])
        elif znak.isalnum():
            bukvy.append(znak)
        elif znak in " -_'":
            bukvy.append(" ")
    klyuch = " ".join("".join(bukvy).split())
    return ALIASY_GORODOV.get(klyuch, klyuch)


def _goroda_poyasov() -> dict[tuple[str, str], tuple[int, int]]:
    """(страна, ключ города) → координаты. Строится раз: словарь на 419 строк."""
    itog: dict[tuple[str, str], tuple[int, int]] = {}
    for poyas, (strana, shirota, dolgota) in POYASA.items():
        gorod = _klyuch_goroda(poyas.split("/")[-1].replace("_", " "))
        itog.setdefault((strana, gorod), (shirota, dolgota))
    return itog


GORODA = _goroda_poyasov()


def _razbros(semya: int) -> tuple[int, int]:
    """Смещение точки внутри страны. Считается от номера записи, поэтому не
    прыгает между запросами: прыгающая точка читается как переезд клиента."""
    smes = (semya * 2_654_435_761) % (2**32)
    dx = smes % (2 * RAZBROS + 1) - RAZBROS
    dy = (smes // 65_536) % (2 * RAZBROS + 1) - RAZBROS
    return dy, dx


def mesto_strany(kod: str, semya: int = 0) -> tuple[int, int] | None:
    """Центр страны с разбросом. `None` — страны нет в справочнике."""
    centr = CENTRY_STRAN.get((kod or "").upper())
    if centr is None:
        return None
    shirota, dolgota, _ = centr
    if not semya:
        return shirota, dolgota
    dy, dx = _razbros(semya)
    return max(-850_000_000, min(850_000_000, shirota + dy)), _svernut(dolgota + dx)


def _svernut(dolgota: int) -> int:
    """Долгота за 180° — это тот же меридиан с другой стороны."""
    while dolgota > 1_800_000_000:
        dolgota -= 3_600_000_000
    while dolgota < -1_800_000_000:
        dolgota += 3_600_000_000
    return dolgota


def mesto_poyasa(poyas: str) -> tuple[str, int, int] | None:
    """Часовой пояс → страна и город. `None` — пояс незнаком."""
    zapis = POYASA.get((poyas or "").strip())
    if zapis is None:
        return None
    return zapis


def mesto_klienta(client: Client) -> tuple[int, int, str] | None:
    """Широта, долгота и точность. `None` — места нет вовсе."""
    if client.lat_e7 is not None and client.lon_e7 is not None:
        return client.lat_e7, client.lon_e7, TOCHNOST_TOCHKA
    strana = (client.country or "").upper()
    if strana and client.city:
        gorod = GORODA.get((strana, _klyuch_goroda(client.city)))
        if gorod is not None:
            return gorod[0], gorod[1], TOCHNOST_GOROD
    if strana:
        centr = mesto_strany(strana, client.id)
        if centr is not None:
            return centr[0], centr[1], TOCHNOST_STRANA
    return None


def baza(db: Session) -> dict | None:
    """Точка фирмы: откуда тянутся линии поставок.

    Порядок: поставленная руками точка → страна из кода для местных номеров.
    Ни того, ни другого — линий поставок нет, и экран говорит об этом словами.
    """
    nastroyki = settings_service.get_all(db)
    svoya = (nastroyki.get("globe_base") or "").strip()
    if svoya:
        chasti = svoya.split(",")
        if len(chasti) == 2:
            try:
                return {
                    "lat": int(chasti[0]) / 1e7,
                    "lon": int(chasti[1]) / 1e7,
                    "tochnost": TOCHNOST_TOCHKA,
                    "imya": nastroyki.get("brand_name", ""),
                }
            except ValueError:
                pass
    strana = KODY_STRAN.get((nastroyki.get("default_country_code") or "").strip())
    if strana:
        centr = mesto_strany(strana)
        if centr is not None:
            return {
                "lat": centr[0] / 1e7,
                "lon": centr[1] / 1e7,
                "tochnost": TOCHNOST_STRANA,
                "imya": nastroyki.get("brand_name", ""),
                "strana": strana,
            }
    return None


def postavit_tochku(db: Session, client: Client, lat: float | None, lon: float | None) -> Client:
    """Поставить или снять точку клиента руками.

    Хранится целым в 1e-7 градуса: это сантиметры, вчетверо точнее любого
    адреса, и целое не теряет знаков при сравнении.
    """
    if lat is None or lon is None:
        client.lat_e7 = None
        client.lon_e7 = None
        db.flush()
        return client
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise errors.ValidationError("Coordinates are out of range", code="bad_coordinates")
    client.lat_e7 = round(lat * 1e7)
    client.lon_e7 = round(lon * 1e7)
    db.flush()
    return client


def _sostoyanie(zayavok: int, zakazov: int, prosrocheno: int) -> str:
    if prosrocheno:
        return "prosrochka"
    if zayavok or zakazov:
        return "rabota"
    return "pokoy"


def sloi(db: Session) -> list[str]:
    """Какие слои имеют смысл при нынешних блоках. Выключенный блок не
    оставляет ни точек, ни строки в панели — правило блоков целиком."""
    itog = ["clients"]
    if modules_service.is_enabled(db, "deals"):
        itog.append("deals")
    if modules_service.is_enabled(db, "orders"):
        itog.extend(("orders", "overdue"))
    if modules_service.is_enabled(db, "boards") and globus_repo.dosok_s_geo(db) > 0:
        itog.append("visitors")
    itog.extend(("links", "grid", "labels", "night"))
    return itog


def kartina(db: Session, user: User) -> dict:
    """Вся планета одним ответом: точки, связи, счётчики, разрез по странам.

    Одним, а не тремя: экран рисует картину целиком, и три запроса означали бы
    три разных мгновения на одной планете.
    """
    dostupnye = sloi(db)
    seychas = now_utc()
    klienty = globus_repo.klienty_s_mestom(db, PREDEL_TOCHEK)
    ids = [k.id for k in klienty]

    zayavki = globus_repo.otkrytye_zayavki(db, ids) if "deals" in dostupnye else {}
    zakazy = globus_repo.otkrytye_zakazy(db, ids, seychas) if "orders" in dostupnye else {}
    menedzhery = globus_repo.imena_menedzherov(db, sorted({k.manager_id for k in klienty if k.manager_id}))
    summy_vidny = permissions_service.has(db, user, "deals", "view_amounts")

    tochki: list[dict] = []
    po_stranam: dict[str, int] = {}
    bez_tochki = 0
    for klient in klienty:
        mesto = mesto_klienta(klient)
        if mesto is None:
            bez_tochki += 1
            continue
        shirota, dolgota, tochnost = mesto
        zayavok, summa = zayavki.get(klient.id, (0, 0))
        zakazov, prosrocheno = zakazy.get(klient.id, (0, 0))
        strana = (klient.country or "").upper()
        if strana:
            po_stranam[strana] = po_stranam.get(strana, 0) + 1
        tochki.append(
            {
                "vid": "client",
                "id": klient.id,
                "imya": klient.name,
                "podpis": ", ".join(x for x in (klient.city, strana) if x),
                "lat": shirota / 1e7,
                "lon": dolgota / 1e7,
                "tochnost": tochnost,
                "strana": strana,
                "company": klient.company,
                "phone": klient.phone,
                "email": klient.email,
                "manager": menedzhery.get(klient.manager_id or 0, ""),
                "deals_open": zayavok,
                "orders_open": zakazov,
                "overdue": prosrocheno,
                "amount": summa if summy_vidny else None,
                "state": _sostoyanie(zayavok, zakazov, prosrocheno),
            }
        )

    svyazi: list[dict] = []
    tochka_bazy = baza(db)
    if tochka_bazy is not None:
        for tochka in tochki:
            ves = tochka["deals_open"] + tochka["orders_open"]
            if ves:
                svyazi.append(
                    {"ot": "base", "k": f"client:{tochka['id']}", "vid": "postavka", "ves": ves}
                )

    gosti = _gosti(db, dostupnye, tochki, svyazi)

    return {
        "base": tochka_bazy,
        "points": tochki + gosti,
        "links": svyazi,
        "layers": dostupnye,
        "countries": [
            {"code": kod, "name": CENTRY_STRAN.get(kod, (0, 0, kod))[2], "clients": skolko}
            for kod, skolko in sorted(po_stranam.items(), key=lambda p: (-p[1], p[0]))
        ],
        "totals": {
            "clients": len(tochki),
            "no_place": globus_repo.bez_mesta(db) + bez_tochki,
            "countries": len(po_stranam),
            "visitors": len(gosti),
            "links": len(svyazi),
        },
        "at": seychas.isoformat(),
    }


def _gosti(db: Session, dostupnye: list[str], tochki: list[dict], svyazi: list[dict]) -> list[dict]:
    """Гости витрин: одна точка на просмотр, линия — к клиенту доски."""
    if "visitors" not in dostupnye:
        return []
    est_klient = {t["id"] for t in tochki}
    itog: list[dict] = []
    for prosmotr, doska_id, nazvanie, klient_id in globus_repo.gosti(db, PREDEL_GOSTEY):
        mesto = mesto_poyasa(prosmotr.tz)
        if mesto is None:
            continue
        strana, shirota, dolgota = mesto
        itog.append(
            {
                "vid": "visitor",
                "id": prosmotr.id,
                "imya": prosmotr.tz.split("/")[-1].replace("_", " "),
                "podpis": strana,
                "lat": shirota / 1e7,
                "lon": dolgota / 1e7,
                "tochnost": TOCHNOST_GOROD,
                "strana": strana,
                "board": nazvanie,
                "board_id": doska_id,
                "at": prosmotr.viewed_at.isoformat() if prosmotr.viewed_at else None,
                "state": "gost",
            }
        )
        if klient_id in est_klient:
            svyazi.append(
                {"ot": f"visitor:{prosmotr.id}", "k": f"client:{klient_id}", "vid": "prosmotr", "ves": 1}
            )
    return itog


def zapisat_poyas(db: Session, share_link_id: int, ip_hash: str, poyas: str) -> bool:
    """Дописать пояс последнему просмотру этого гостя.

    Дописать, а не завести новую строку: иначе один заход считался бы дважды и
    счётчик просмотров врал бы ровно на тех досках, где гео включено.
    """
    zapis = globus_repo.poslednij_prosmotr(db, share_link_id, ip_hash)
    if zapis is None:
        return False
    zapis.tz = (poyas or "")[:64]
    db.flush()
    return True


def znakomyy_poyas(poyas: str) -> bool:
    return (poyas or "").strip() in POYASA
