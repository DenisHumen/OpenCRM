"""Очертания суши из TopoJSON: разбор, прореживание, упаковка.

Живёт в `core/`, а не в скрипте, потому что читателей двое: скрипт, который
вшивает грубые очертания в исходники, и служба, которая докачивает подробные
(`core/services/globus_karta_service.py`). Две копии разошлись бы на первой же
правке, а расхождение здесь — это две разные планеты в одном интерфейсе.
"""
from __future__ import annotations

#: Прореживание по умолчанию: точка ближе к предыдущей — лишняя. На глобусе
#: радиусом 350px 0.25° меньше пикселя, а вес файла втрое ниже.
SHAG = 0.25
#: Кольца мельче этого размера (в градусах) выбрасываем: на глобусе они в точку.
MELKIY_OSTROV = 0.8


def _tochki_dugi(topo: dict, nomer: int) -> list[tuple[float, float]]:
    """Дуга TopoJSON: первая точка абсолютная, дальше приращения."""
    obratno = nomer < 0
    duga = topo["arcs"][~nomer if obratno else nomer]
    scale = topo["transform"]["scale"]
    translate = topo["transform"]["translate"]
    x = y = 0
    tochki = []
    for dx, dy in duga:
        x += dx
        y += dy
        tochki.append((x * scale[0] + translate[0], y * scale[1] + translate[1]))
    return tochki[::-1] if obratno else tochki


def _kolco(topo: dict, nomera: list[int]) -> list[tuple[float, float]]:
    tochki: list[tuple[float, float]] = []
    for nomer in nomera:
        kusok = _tochki_dugi(topo, nomer)
        tochki.extend(kusok[1:] if tochki else kusok)
    return tochki


def _prorezhivanie(tochki: list[tuple[float, float]], shag: float) -> list[tuple[float, float]]:
    itog = [tochki[0]]
    for lon, lat in tochki[1:]:
        prosh_lon, prosh_lat = itog[-1]
        if abs(lon - prosh_lon) >= shag or abs(lat - prosh_lat) >= shag:
            itog.append((lon, lat))
    return itog


def _krupnoe(tochki: list[tuple[float, float]], melkiy: float) -> bool:
    lony = [t[0] for t in tochki]
    laty = [t[1] for t in tochki]
    return max(lony) - min(lony) >= melkiy or max(laty) - min(laty) >= melkiy


def kontury(topo: dict, shag: float = SHAG, melkiy: float = MELKIY_OSTROV) -> list[list[tuple[float, float]]]:
    """Все кольца суши, прорежённые и без мелких островов."""
    if "transform" not in topo or "arcs" not in topo:
        raise ValueError("не TopoJSON: нет transform или arcs")
    obekty = topo.get("objects") or {}
    sloy = obekty.get("countries") or obekty.get("land")
    if sloy is None:
        raise ValueError("в наборе нет слоя countries или land")
    geometrii = sloy.get("geometries") or [sloy]
    itog = []
    for geometriya in geometrii:
        vid = geometriya.get("type")
        if vid == "Polygon":
            spisok = geometriya["arcs"]
        elif vid == "MultiPolygon":
            spisok = [kolco for kusok in geometriya["arcs"] for kolco in kusok]
        else:
            continue
        for nomera in spisok:
            tochki = _prorezhivanie(_kolco(topo, nomera), shag)
            if len(tochki) >= 4 and _krupnoe(tochki, melkiy):
                itog.append(tochki)
    return itog


def upakovat(kolco: list[tuple[float, float]]) -> list[int]:
    """Кольцо в сотых долях градуса: первая точка целиком, дальше приращения.

    Приращения короче абсолютных чисел вчетверо, а разбор на экране — цикл в
    пять строк (`lib/globus/proekciya.ts::razvernut`).
    """
    itog: list[int] = []
    prosh_lon = prosh_lat = 0
    for lon, lat in kolco:
        cel_lon = round(lon * 100)
        cel_lat = round(lat * 100)
        itog.extend((cel_lon - prosh_lon, cel_lat - prosh_lat))
        prosh_lon, prosh_lat = cel_lon, cel_lat
    return itog
