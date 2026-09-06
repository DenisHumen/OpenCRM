"""Порождает данные карты для блока «Глобус» из трёх открытых наборов.

Наборы скачиваются раз и кладутся в исходники: на боевом сервере интернета для
карты не просят, а очертания материков и центры стран не меняются годами.
Разбор — `docs/bloki/25-globus.md`, раздел «Откуда данные».

Запуск (пути к скачанным файлам):

    python -m scripts.globus_dannye --mir countries-110m.json \\
        --strany countries.csv --poyasa zone.tab

Источники:
  * `countries-110m.json` — Natural Earth 1:110m через пакет `world-atlas`,
    https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json (public domain);
  * `countries.csv` — центры стран, https://github.com/google/dspl
    (`samples/google/canonical/countries.csv`, Apache-2.0);
  * `zone.tab` — таблица часовых поясов IANA, https://github.com/eggert/tz
    (public domain).

Разбор TopoJSON общий со службой докачки — `core/geo/topojson.py`.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys

from core.geo.topojson import kontury, upakovat

KOREN = pathlib.Path(__file__).resolve().parent.parent
DANNYE_PY = KOREN / "core" / "geo" / "dannye.py"
MIR_TS = KOREN / "web" / "frontend" / "crm" / "src" / "lib" / "globus" / "mir.ts"


def _iso6709(znachenie: str) -> tuple[int, int]:
    """`+5026+03031` или `+5026+0303100` → широта и долгота в 1e-7 градуса."""
    razdel = znachenie.find("+", 1)
    if razdel < 0:
        razdel = znachenie.find("-", 1)
    shirota, dolgota = znachenie[:razdel], znachenie[razdel:]

    def gradusy(chast: str, dlina_gradusov: int) -> int:
        znak = -1 if chast[0] == "-" else 1
        cifry = chast[1:]
        grad = int(cifry[:dlina_gradusov])
        minuty = int(cifry[dlina_gradusov:dlina_gradusov + 2] or 0)
        sekundy = int(cifry[dlina_gradusov + 2:dlina_gradusov + 4] or 0)
        return znak * (grad * 10_000_000 + minuty * 10_000_000 // 60 + sekundy * 10_000_000 // 3600)

    return gradusy(shirota, 2), gradusy(dolgota, 3)


def centry_stran(put: pathlib.Path) -> dict[str, tuple[int, int, str]]:
    itog: dict[str, tuple[int, int, str]] = {}
    with put.open(encoding="utf-8", newline="") as f:
        for stroka in csv.DictReader(f):
            kod = (stroka["country"] or "").strip().upper()
            if len(kod) != 2 or not stroka["latitude"]:
                continue
            itog[kod] = (
                round(float(stroka["latitude"]) * 10_000_000),
                round(float(stroka["longitude"]) * 10_000_000),
                (stroka["name"] or "").strip(),
            )
    return dict(sorted(itog.items()))


def poyasa(put: pathlib.Path) -> dict[str, tuple[str, int, int]]:
    itog: dict[str, tuple[str, int, int]] = {}
    for stroka in put.read_text(encoding="utf-8").splitlines():
        if not stroka or stroka.startswith("#"):
            continue
        chasti = stroka.split("\t")
        if len(chasti) < 3:
            continue
        strana, koordinaty, imya = chasti[0].strip(), chasti[1].strip(), chasti[2].strip()
        if len(strana) != 2 or "/" not in imya:
            continue
        shirota, dolgota = _iso6709(koordinaty)
        itog[imya] = (strana, shirota, dolgota)
    return dict(sorted(itog.items()))


SHAPKA_PY = '''"""Данные карты: центры стран и города часовых поясов. ПОРОЖДЁННЫЙ ФАЙЛ.

Правится не он, а `scripts/globus_dannye.py` — там же названы источники и их
лицензии. Разбор устройства — `docs/bloki/25-globus.md`.

Координаты целые, в 1e-7 градуса: дробь тут не нужна ни разу, а целое
сравнивается и складывается без сюрпризов округления.
"""
'''


def zapisat_python(strany: dict, chasovye: dict) -> None:
    stroki = [SHAPKA_PY, "", "#: Код ISO 3166-1 alpha-2 → широта, долгота, английское название страны.",
              "CENTRY_STRAN: dict[str, tuple[int, int, str]] = {"]
    for kod, (shirota, dolgota, imya) in strany.items():
        stroki.append(f'    "{kod}": ({shirota}, {dolgota}, {json.dumps(imya)}),')
    stroki.append("}")
    stroki.append("")
    stroki.append("#: Часовой пояс IANA → страна, широта, долгота его главного города.")
    stroki.append("POYASA: dict[str, tuple[str, int, int]] = {")
    for imya, (strana, shirota, dolgota) in chasovye.items():
        stroki.append(f'    "{imya}": ("{strana}", {shirota}, {dolgota}),')
    stroki.append("}")
    stroki.append("")
    DANNYE_PY.parent.mkdir(parents=True, exist_ok=True)
    DANNYE_PY.write_bytes("\n".join(stroki).encode("utf-8"))


SHAPKA_TS = """/**
 * Очертания материков для глобуса. ПОРОЖДЁННЫЙ ФАЙЛ.
 *
 * Правится не он, а `scripts/globus_dannye.py`; источник и лицензия названы
 * там же. Кольца лежат в сотых долях градуса приращениями — так файл втрое
 * легче, а разбор помещается в пять строк (`lib/globus/proekciya.ts`).
 */
"""


def zapisat_ts(kolca: list[list[tuple[float, float]]]) -> None:
    stroki = [SHAPKA_TS, "export const KONTURY: readonly (readonly number[])[] = ["]
    for kolco in kolca:
        stroki.append("  [" + ",".join(str(n) for n in upakovat(kolco)) + "],")
    stroki.append("];")
    stroki.append("")
    MIR_TS.parent.mkdir(parents=True, exist_ok=True)
    MIR_TS.write_bytes("\n".join(stroki).encode("utf-8"))


def main() -> int:
    razbor = argparse.ArgumentParser(description="порождение данных карты для блока «Глобус»")
    razbor.add_argument("--mir", required=True, type=pathlib.Path)
    razbor.add_argument("--strany", required=True, type=pathlib.Path)
    razbor.add_argument("--poyasa", required=True, type=pathlib.Path)
    dovody = razbor.parse_args()

    topo = json.loads(dovody.mir.read_text(encoding="utf-8"))
    kolca = kontury(topo)
    strany = centry_stran(dovody.strany)
    chasovye = poyasa(dovody.poyasa)

    zapisat_python(strany, chasovye)
    zapisat_ts(kolca)
    tochek = sum(len(k) for k in kolca)
    print(f"контуров {len(kolca)}, точек {tochek}, стран {len(strany)}, поясов {len(chasovye)}")
    print(f"{DANNYE_PY.relative_to(KOREN)} — {DANNYE_PY.stat().st_size // 1024} КБ")
    print(f"{MIR_TS.relative_to(KOREN)} — {MIR_TS.stat().st_size // 1024} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
