"""Место клиента на карте: точка, город или страна.

Правило одно: **хранится источник, а не вывод**. Центр страны и город часового
пояса — величины производные, и в базе им не место (`CLAUDE.md` §3). В базе
только точка, поставленная рукой (`clients.lat_e7`); всё остальное считается
здесь. Разбор — `docs/bloki/26-adresa.md`.

Служба осталась от снятого блока «Глобус» и держит ровно то, что пережило его:
подсказки в поле адреса и миниатюру карты в карточке клиента. Планеты, гостей и
связей здесь больше нет — довод, почему блок сняли, в
`docs/osnovy/09-sostoyanie-i-resheniya.md`.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.geo.dannye import CENTRY_STRAN, POYASA
from database.models import Client

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
