"""Чтение векторной плитки (Mapbox Vector Tile) — ровно столько, сколько нужно.

Своё, а не пакетом: готовые читалки тянут за собой `protobuf` со сборкой под
каждую платформу, а нам от формата нужны три поля из четырёх и одна команда из
трёх. Сам формат простой и не меняется с 2016 года — 2.1 это последняя версия.

Что берём: имя слоя, вид фигуры, геометрию и один-два признака строкой.
Чего не берём: числовые признаки, идентификаторы, вложенные значения — их в
`shortbread` у улиц и домов нет, а разбирать ради полноты то, что никто не
прочтёт, значит писать код, который никто не проверит.

Разбор — `docs/bloki/25-globus.md` §11.2.
"""
from __future__ import annotations

#: Виды фигур из спецификации. Точки нам не нужны, но пропускать их надо со
#: знанием дела, а не по счётчику байт.
TOCHKA = 1
LINIYA = 2
MNOGOUGOLNIK = 3


class Figura:
    """Одна фигура плитки: вид, признаки и кольца в координатах плитки."""

    __slots__ = ("vid", "metki", "kolca")

    def __init__(self, vid: int, metki: dict[str, str], kolca: list[list[tuple[int, int]]]):
        self.vid = vid
        self.metki = metki
        self.kolca = kolca


def _varint(dannye: bytes, i: int) -> tuple[int, int]:
    itog = 0
    sdvig = 0
    while True:
        bayt = dannye[i]
        i += 1
        itog |= (bayt & 0x7F) << sdvig
        if not bayt & 0x80:
            return itog, i
        sdvig += 7


def _polya(dannye: bytes, ot: int = 0, do: int | None = None):
    """Перебор полей сообщения: номер, тип, значение или срез."""
    do = len(dannye) if do is None else do
    i = ot
    while i < do:
        klyuch, i = _varint(dannye, i)
        nomer, tip = klyuch >> 3, klyuch & 7
        if tip == 0:
            znachenie, i = _varint(dannye, i)
            yield nomer, tip, znachenie
        elif tip == 2:
            dlina, i = _varint(dannye, i)
            yield nomer, tip, (i, i + dlina)
            i += dlina
        elif tip == 5:
            yield nomer, tip, (i, i + 4)
            i += 4
        elif tip == 1:
            yield nomer, tip, (i, i + 8)
            i += 8
        else:
            # Групп (3 и 4) в этом формате нет; встретили — плитка не наша.
            raise ValueError(f"неизвестный тип поля {tip}")


def _stroka(dannye: bytes, ot: int, do: int) -> str:
    return dannye[ot:do].decode("utf-8", "replace")


def _znachenie(dannye: bytes, ot: int, do: int) -> str | None:
    """Значение признака. Берём только строку: числа у нас не читает никто."""
    for nomer, _tip, znach in _polya(dannye, ot, do):
        if nomer == 1 and isinstance(znach, tuple):
            return _stroka(dannye, *znach)
    return None


def _geometriya(chisla: list[int]) -> list[list[tuple[int, int]]]:
    """Команды в кольца. `MoveTo` начинает новое, `ClosePath` его замыкает."""
    kolca: list[list[tuple[int, int]]] = []
    tekushchee: list[tuple[int, int]] = []
    x = y = 0
    i = 0
    while i < len(chisla):
        komanda = chisla[i]
        i += 1
        vid, skolko = komanda & 7, komanda >> 3
        if vid == 7:  # ClosePath — параметров нет
            if tekushchee:
                tekushchee.append(tekushchee[0])
            continue
        for _ in range(skolko):
            if i + 1 >= len(chisla) + 1 and i + 2 > len(chisla):
                return kolca  # обрезанная плитка: отдаём, что успели прочесть
            # Зигзаг: знак в младшем бите, иначе отрицательные съели бы varint.
            dx = (chisla[i] >> 1) ^ -(chisla[i] & 1)
            dy = (chisla[i + 1] >> 1) ^ -(chisla[i + 1] & 1)
            i += 2
            x += dx
            y += dy
            if vid == 1:  # MoveTo — новая линия
                if len(tekushchee) > 1:
                    kolca.append(tekushchee)
                tekushchee = [(x, y)]
            else:
                tekushchee.append((x, y))
    if len(tekushchee) > 1:
        kolca.append(tekushchee)
    return kolca


def sloi(syroe: bytes, nuzhny: set[str], priznaki: set[str]) -> dict[str, list[Figura]]:
    """Плитка в фигуры по слоям. Чужие слои не разбираются вовсе.

    `nuzhny` — имена слоёв, `priznaki` — какие признаки оставить. Всё
    остальное пропускается, не доходя до разбора: в плитке `shortbread` два
    десятка слоёв, а рисуем мы два.
    """
    itog: dict[str, list[Figura]] = {}
    for nomer, _tip, znach in _polya(syroe):
        if nomer != 3 or not isinstance(znach, tuple):
            continue
        imya, figury, ekstent = _sloy(syroe, *znach, nuzhny, priznaki)
        if imya is None:
            continue
        itog[imya] = figury
        itog.setdefault("_ekstent", ekstent)  # type: ignore[arg-type]
    return itog


def _sloy(syroe: bytes, ot: int, do: int, nuzhny: set[str], priznaki: set[str]):
    imya: str | None = None
    ekstent = 4096
    klyuchi: list[str] = []
    znacheniya: list[str | None] = []
    figury_syrye: list[tuple[int, int]] = []

    for nomer, tip, znach in _polya(syroe, ot, do):
        if nomer == 1 and isinstance(znach, tuple):
            imya = _stroka(syroe, *znach)
            if imya not in nuzhny:
                return None, [], ekstent
        elif nomer == 2 and isinstance(znach, tuple):
            figury_syrye.append(znach)
        elif nomer == 3 and isinstance(znach, tuple):
            klyuchi.append(_stroka(syroe, *znach))
        elif nomer == 4 and isinstance(znach, tuple):
            znacheniya.append(_znachenie(syroe, *znach))
        elif nomer == 5 and tip == 0:
            ekstent = int(znach)  # type: ignore[arg-type]

    if imya is None:
        return None, [], ekstent

    figury = [_figura(syroe, a, b, klyuchi, znacheniya, priznaki) for a, b in figury_syrye]
    return imya, [f for f in figury if f is not None], ekstent


def _figura(syroe: bytes, ot: int, do: int, klyuchi, znacheniya, priznaki) -> Figura | None:
    vid = 0
    pary: list[int] = []
    chisla: list[int] = []
    for nomer, _tip, znach in _polya(syroe, ot, do):
        if nomer == 2 and isinstance(znach, tuple):
            pary = _pachka(syroe, *znach)
        elif nomer == 3:
            vid = int(znach)  # type: ignore[arg-type]
        elif nomer == 4 and isinstance(znach, tuple):
            chisla = _pachka(syroe, *znach)
    if vid not in (LINIYA, MNOGOUGOLNIK) or not chisla:
        return None

    metki: dict[str, str] = {}
    for i in range(0, len(pary) - 1, 2):
        k, v = pary[i], pary[i + 1]
        if k < len(klyuchi) and klyuchi[k] in priznaki and v < len(znacheniya):
            znachenie = znacheniya[v]
            if znachenie is not None:
                metki[klyuchi[k]] = znachenie
    kolca = _geometriya(chisla)
    return Figura(vid, metki, kolca) if kolca else None


def _pachka(dannye: bytes, ot: int, do: int) -> list[int]:
    itog: list[int] = []
    i = ot
    while i < do:
        znachenie, i = _varint(dannye, i)
        itog.append(znachenie)
    return itog
