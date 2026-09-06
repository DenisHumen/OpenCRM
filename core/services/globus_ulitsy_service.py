"""Улицы и дома под сильным приближением: докачка плитками, когда сеть есть.

Владелец 06.09.2026: «в примере что я предоставил там обрисовываются прям
дороги при сильном приближении. Я так же хочу что бы обрисовывались дороги и
макеты зданий».

Правило блока прежнее (`globus_karta_service`): без интернета планета работает
как работала, а подробности — необязательное улучшение. Улицы тем более: их
просят, только когда человек подъехал вплотную к городу, и до этого момента ни
одного запроса наружу не уходит.

Разбор — `docs/bloki/25-globus.md` §11.2.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config.settings import get_settings

#: Откуда берём. Overpass отдаёт сырой OSM выборкой по прямоугольнику: свой
#: тайловый сервер поднимать не под что, а растровые плитки — картинки, тогда
#: как владелец просил ОБРИСОВКУ, то есть линии.
ISTOCHNIK = "https://overpass-api.de/api/interpreter"

#: Уровень плитки. 16-й — примерно 600 метров по экватору: столько же, сколько
#: видно на экране под тем приближением, с которого улицы вообще включаются.
#: Крупнее — ответ в мегабайты и минуту ожидания, мельче — вчетверо больше
#: запросов на тот же вид.
PLITKA_Z = 16

#: Точность — стотысячная градуса, около метра. Дом занимает десятки метров,
#: полметра его очертанию не добавят, а вес удваивают.
TOCHNOST = 100_000

#: Сколько ждём ответ. Overpass на людной плитке думает секунды, а не мгновения.
SROK_SEKUND = 45
#: Пауза между запросами наружу. Overpass — общий бесплатный сервер, и
#: очередь из десятка плиток подряд с него прилетает отказом, а не данными.
PAUZA_SEKUND = 2.0
#: Отдых после отказа: сеть пропала или нас притормозили — в обоих случаях
#: долбить дальше бессмысленно.
OTDYH_SEKUND = 60.0
#: Больше этого ответ не бывает: защита от подменённого адреса и от диска.
POTOLOK_BAYT = 12 * 1024 * 1024
#: Сколько плиток ждут очереди. Экран просит только видимые; больше десятка
#: разом — значит человек возит планету, и старые запросы уже не нужны.
POTOLOK_OCHEREDI = 16
#: Сколько плиток храним. 800 штук — примерно 30 МБ и полтора года разъездов
#: по одному городу; дальше самые старые уходят.
POTOLOK_PLITOK = 800

#: Вид дороги числом: 3 — магистраль, 2 — главная, 1 — улица, 0 — проезд.
#: Числом, а не словом: экран по нему выбирает толщину линии, и разбирать в
#: цикле отрисовки строки было бы дороже самой отрисовки.
VIDY_DOROG = {
    "motorway": 3, "motorway_link": 3, "trunk": 3, "trunk_link": 3,
    "primary": 3, "primary_link": 3,
    "secondary": 2, "secondary_link": 2, "tertiary": 2, "tertiary_link": 2,
    "residential": 1, "unclassified": 1, "living_street": 1, "pedestrian": 1,
}
#: Чего не рисуем вовсе: недостроенное и задуманное дорогой не является.
MIMO_DOROG = {"proposed", "construction", "raceway", "bus_guideway"}

_ZAMOK = threading.Lock()
#: Очередь плиток и общий отдых. В памяти, а не в базе: переживать перезапуск
#: очереди незачем — после него экран попросит те же плитки заново.
_OCHERED: list[tuple[int, int, int]] = []
_IDET: set[tuple[int, int, int]] = set()
_OSHIBKA = {"tekst": "", "do": 0.0}
_RABOTNIK: threading.Thread | None = None


# --- плитки -------------------------------------------------------------------


def _katalog():
    return get_settings().storage_dir / "globus" / "ulitsy"


def _fayl(z: int, x: int, y: int):
    return _katalog() / f"{z}_{x}_{y}.json"


def granicy(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Прямоугольник плитки: юг, запад, север, восток — в градусах."""
    n = 2**z
    zapad = x / n * 360.0 - 180.0
    vostok = (x + 1) / n * 360.0 - 180.0
    yug = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    sever = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return yug, zapad, sever, vostok


def nomer(lon: float, lat: float, z: int = PLITKA_Z) -> tuple[int, int, int]:
    """Плитка, в которую попала точка."""
    n = 2**z
    lat = max(-85.05, min(85.05, lat))
    x = int((lon + 180.0) / 360.0 * n) % n
    doba = math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
    y = int((1 - doba / math.pi) / 2 * n)
    return z, x, max(0, min(n - 1, y))


def _upakovat(tochki: list[dict]) -> list[int]:
    """Точки в разности целых: у соседних узлов улицы совпадают четыре знака."""
    itog: list[int] = []
    plon = plat = 0
    for tochka in tochki:
        lon = round(tochka["lon"] * TOCHNOST)
        lat = round(tochka["lat"] * TOCHNOST)
        itog.append(lon - plon)
        itog.append(lat - plat)
        plon, plat = lon, lat
    return itog


def _razobrat(otvet: dict) -> dict:
    dorogi: list[list[int]] = []
    doma: list[list[int]] = []
    for element in otvet.get("elements", []):
        tochki = element.get("geometry")
        if not tochki or len(tochki) < 2:
            continue
        metki = element.get("tags") or {}
        if metki.get("building"):
            doma.append(_upakovat(tochki))
            continue
        vid = metki.get("highway")
        if not vid or vid in MIMO_DOROG:
            continue
        dorogi.append([VIDY_DOROG.get(vid, 0)] + _upakovat(tochki))
    return {"dorogi": dorogi, "doma": doma}


def _prochitat(z: int, x: int, y: int) -> dict | None:
    put = _fayl(z, x, y)
    if not put.exists():
        return None
    try:
        return json.loads(put.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Оборванный файл — то же, что его отсутствие: снесём и перекачаем.
        put.unlink(missing_ok=True)
        return None


def _zapisat(z: int, x: int, y: int, dannye: dict) -> None:
    katalog = _katalog()
    katalog.mkdir(parents=True, exist_ok=True)
    vremenno = _fayl(z, x, y).with_suffix(".tmp")
    vremenno.write_text(json.dumps(dannye, separators=(",", ":")), encoding="utf-8")
    vremenno.replace(_fayl(z, x, y))
    _podmesti()


def _podmesti() -> None:
    """Держим потолок плиток: лишние уходят самые давние."""
    fayly = sorted(_katalog().glob("*.json"), key=lambda p: p.stat().st_mtime)
    for put in fayly[: max(0, len(fayly) - POTOLOK_PLITOK)]:
        put.unlink(missing_ok=True)


# --- очередь ------------------------------------------------------------------


def plitka(z: int, x: int, y: int, hotim: bool) -> dict:
    """Что показать по одной плитке — и заодно повод встать в очередь.

    Как и у подробных очертаний, проверка сети привязана к вопросу, а не к
    таймеру: пока никто не подъехал к городу, наружу не уходит ничего.
    """
    if z != PLITKA_Z or not (0 <= x < 2**z) or not (0 <= y < 2**z):
        return {"z": z, "x": x, "y": y, "gotovo": False, "idet": False, "oshibka": "tile_out_of_range"}
    lezhit = _prochitat(z, x, y)
    if lezhit is not None:
        return {"z": z, "x": x, "y": y, "gotovo": True, "idet": False, "oshibka": "", **lezhit}

    klyuch = (z, x, y)
    with _ZAMOK:
        idet = klyuch in _IDET or klyuch in _OCHERED
        oshibka = _OSHIBKA["tekst"] if time.time() < _OSHIBKA["do"] else ""
        if hotim and not idet and not oshibka and len(_OCHERED) < POTOLOK_OCHEREDI:
            _OCHERED.append(klyuch)
            idet = True
            _pustit()
    return {"z": z, "x": x, "y": y, "gotovo": False, "idet": idet, "oshibka": oshibka}


def _pustit() -> None:
    """Пускает работника. Зовётся под замком."""
    global _RABOTNIK
    if _RABOTNIK is not None and _RABOTNIK.is_alive():
        return
    _RABOTNIK = threading.Thread(target=_rabotat, daemon=True, name="globus-ulitsy")
    _RABOTNIK.start()


def _rabotat() -> None:
    while True:
        with _ZAMOK:
            if not _OCHERED:
                return
            klyuch = _OCHERED.pop(0)
            _IDET.add(klyuch)
        try:
            _zapisat(*klyuch, _vzyat(*klyuch))
            with _ZAMOK:
                _OSHIBKA.update({"tekst": "", "do": 0.0})
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as beda:
            # Нет сети или нас притормозили — не беда, а состояние: очередь
            # сбрасывается, экран рисует то, что уже лежит, и просит снова
            # после отдыха.
            with _ZAMOK:
                _OSHIBKA.update({"tekst": str(beda)[:200], "do": time.time() + OTDYH_SEKUND})
                _OCHERED.clear()
        finally:
            with _ZAMOK:
                _IDET.discard(klyuch)
        time.sleep(PAUZA_SEKUND)


def _vzyat(z: int, x: int, y: int) -> dict:
    yug, zapad, sever, vostok = granicy(z, x, y)
    ramka = f"{yug:.6f},{zapad:.6f},{sever:.6f},{vostok:.6f}"
    vopros = (
        "[out:json][timeout:25];"
        f'(way["highway"]({ramka});way["building"]({ramka}););'
        "out geom;"
    )
    zapros = urllib.request.Request(
        ISTOCHNIK,
        data=urllib.parse.urlencode({"data": vopros}).encode("utf-8"),
        # Overpass просит называться: безымянных он режет первыми, когда
        # сервер загружен, и «нет сети» оказалось бы враньём.
        headers={"User-Agent": "OpenCRM globe (https://github.com/DenisHumen/OpenCRM)"},
    )
    with urllib.request.urlopen(zapros, timeout=SROK_SEKUND) as otvet:
        syroe = otvet.read(POTOLOK_BAYT + 1)
    if len(syroe) > POTOLOK_BAYT:
        raise ValueError("ответ больше потолка")
    return _razobrat(json.loads(syroe.decode("utf-8")))


def zabyt() -> None:
    """Снести скачанные улицы: планета вернётся к одним очертаниям."""
    with _ZAMOK:
        _OCHERED.clear()
    katalog = _katalog()
    if katalog.exists():
        for put in katalog.glob("*.json"):
            put.unlink(missing_ok=True)


def zapas() -> dict:
    """Сколько плиток лежит и сколько это весит — для экрана настроек."""
    katalog = _katalog()
    fayly = list(katalog.glob("*.json")) if katalog.exists() else []
    with _ZAMOK:
        v_ocheredi = len(_OCHERED) + len(_IDET)
        oshibka = _OSHIBKA["tekst"] if time.time() < _OSHIBKA["do"] else ""
    return {
        "tiles": len(fayly),
        "bytes": sum(put.stat().st_size for put in fayly),
        "queued": v_ocheredi,
        "error": oshibka,
        "zoom": PLITKA_Z,
        "source": ISTOCHNIK,
    }
