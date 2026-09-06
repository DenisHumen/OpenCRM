"""Порождённые данные карты: центры стран, пояса, очертания суши.

Файлы порождает `scripts/globus_dannye.py` из трёх открытых наборов, и на
боевом сервере их никто не пересчитывает. Значит проверять надо не «совпадает
ли с источником» (источника рядом нет), а внутреннюю правду: коды по две
буквы, координаты в пределах шара, кольца не пустые. Разбор —
`docs/bloki/25-globus.md` §11.
"""
import pathlib
import re

from core.geo.dannye import CENTRY_STRAN, POYASA
from core.geo.topojson import kontury, upakovat

KOREN = pathlib.Path(__file__).resolve().parent.parent
MIR_TS = KOREN / "web" / "frontend" / "crm" / "src" / "lib" / "globus" / "mir.ts"
KOLCO = re.compile(r"^  \[([-\d,]+)\],$", re.M)


def _kolca() -> list[list[int]]:
    tekst = MIR_TS.read_text(encoding="utf-8")
    return [[int(x) for x in stroka.split(",")] for stroka in KOLCO.findall(tekst)]


def test_centry_stran_pravdopodobny():
    assert len(CENTRY_STRAN) > 200, f"стран {len(CENTRY_STRAN)} — набор не тот"
    for kod, (shirota, dolgota, imya) in CENTRY_STRAN.items():
        assert len(kod) == 2 and kod.isalpha() and kod.isupper(), f"код страны {kod!r}"
        assert -900_000_000 <= shirota <= 900_000_000, f"{kod}: широта {shirota}"
        assert -1_800_000_000 <= dolgota <= 1_800_000_000, f"{kod}: долгота {dolgota}"
        assert imya, f"{kod}: страна без названия"


def test_poyasa_pravdopodobny():
    assert len(POYASA) > 400, f"поясов {len(POYASA)} — набор не тот"
    for imya, (strana, shirota, dolgota) in POYASA.items():
        assert "/" in imya, f"пояс {imya!r} без области"
        assert len(strana) == 2 and strana.isupper(), f"{imya}: страна {strana!r}"
        assert -900_000_000 <= shirota <= 900_000_000, f"{imya}: широта {shirota}"
        assert -1_800_000_000 <= dolgota <= 1_800_000_000, f"{imya}: долгота {dolgota}"


def test_izvestnye_mesta_na_svoikh_mestakh():
    """Пара опорных точек: набор мог приехать со сдвинутыми столбцами, и это
    заметно только на знакомом городе."""
    shirota, dolgota, imya = CENTRY_STRAN["UA"]
    assert imya == "Ukraine" and 44 < shirota / 1e7 < 53 and 22 < dolgota / 1e7 < 41
    strana, shirota, dolgota = POYASA["Europe/Kyiv"]
    assert strana == "UA" and 50.0 < shirota / 1e7 < 50.9 and 30.0 < dolgota / 1e7 < 31.0
    strana, shirota, dolgota = POYASA["America/New_York"]
    assert strana == "US" and dolgota < 0, "западное полушарие ушло в плюс"


def test_ochertaniya_razvorachivayutsya_v_shar():
    """Кольца лежат приращениями: разворачиваем так же, как экран, и смотрим,
    что получившиеся точки — на шаре, а не за его пределами."""
    kolca = _kolca()
    assert len(kolca) > 200, f"колец {len(kolca)} — файл не тот"
    vsego = 0
    for nomer, kolco in enumerate(kolca):
        assert len(kolco) >= 8 and len(kolco) % 2 == 0, f"кольцо {nomer}: точек {len(kolco) // 2}"
        lon = lat = 0
        for i in range(0, len(kolco), 2):
            lon += kolco[i]
            lat += kolco[i + 1]
            assert -18_001 <= lon <= 18_001, f"кольцо {nomer}: долгота {lon / 100}"
            assert -9_001 <= lat <= 9_001, f"кольцо {nomer}: широта {lat / 100}"
            vsego += 1
    assert vsego > 5_000, f"точек {vsego} — суша слишком грубая"


def test_upakovka_obratima():
    """Приращения — не хитрость, а сжатие: разворот обязан вернуть исходное с
    точностью до сотой доли градуса."""
    ishodnoe = [(30.52, 50.45), (30.7, 50.6), (-179.9, -17.5), (179.9, 17.5)]
    upakovano = upakovat(ishodnoe)
    lon = lat = 0
    razvernuto = []
    for i in range(0, len(upakovano), 2):
        lon += upakovano[i]
        lat += upakovano[i + 1]
        razvernuto.append((lon / 100, lat / 100))
    assert razvernuto == [(round(a, 2), round(b, 2)) for a, b in ishodnoe]


def test_razbor_topojson_ponimaet_oba_sloya():
    """Разбор один на два набора: вшитый зовётся `countries`, докачиваемый
    может прийти слоем `land` — на этом уже спотыкались чужие разборы."""
    topo = {
        "type": "Topology",
        "transform": {"scale": [0.01, 0.01], "translate": [0, 0]},
        "arcs": [[[0, 0], [500, 0], [0, 500], [-500, 0], [0, -500]]],
        "objects": {"land": {"type": "GeometryCollection", "geometries": [{"type": "Polygon", "arcs": [[0]]}]}},
    }
    kolca = kontury(topo, shag=0.1, melkiy=0.5)
    assert len(kolca) == 1 and len(kolca[0]) >= 4
