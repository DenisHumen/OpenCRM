"""Гейт обновления обязан видеть те же файлы, что и мы снаружи.

Автообновление проверяет новый код единственным способом — собирает образ
`--target tests` и гоняет в нём весь набор (`deploy/updater.py:_checks`). Значит
всё, чего в этом образе нет, проверенным на воротах деплоя НЕ БЫВАЕТ.

Беда в том, что отсутствие файла здесь не краснеет, а МОЛЧИТ. Целые файлы
проверок помечены «пропустить, если файла нет рядом» — приём законный (набор
гоняют и вне репозитория), но в образе он превращает пропажу файла в тишину.
Так и вышло с `opencrm.sh`: этап `tests` копировал `deploy/`, `tests/`,
`docker/` — и не копировал его. Снаружи 2119 пройдено / 2 пропущено, в образе
гейта 2059 пройдено / 62 пропущено, из разницы 45 — сторожа установщика. Пять
суток это было незаметно, потому что оба прогона зелёные.

Отсюда две проверки, и обе механические — список файлов никто не ведёт руками:

1. **Каждый сторожевой файл на месте.** Внутри образа гейта это превращает
   молчаливый пропуск в красный тест: пропущенных в образе ровно столько же,
   сколько снаружи, а не на шестьдесят больше.
2. **Каждый сторожевой файл копируется этапом `tests`** — или назван в
   `.dockerignore`, то есть исключён осознанно (так исключён `.github/`:
   Dockerfile из него ничего не берёт, а вместе с ним в контекст уехала бы вся
   история веток).

Список сторожевых файлов собирается ЧТЕНИЕМ САМИХ ПРОВЕРОК, а не пишется здесь
списком: список рядом с правилом устаревает первым, и следующий файл выпал бы
ровно так же.
"""

import re
from pathlib import Path

KOREN = Path(__file__).resolve().parent.parent
DOCKERFILE = KOREN / "docker" / "Dockerfile"
DOCKERIGNORE = KOREN / ".dockerignore"

#: Присваивание вида `SCRIPT = ROOT / "opencrm.sh"` или
#: `SCRIPT = Path(__file__).resolve().parent.parent / "opencrm.sh"`.
NAZNACHENIE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+)$", re.MULTILINE)

#: Обращение вида `SCRIPT.exists()`.
PROVERKA_NALICHIYA = re.compile(r"\b([A-Z][A-Z0-9_]*)\.exists\(\)")

#: Сколько символов перед `X.exists()` смотрим в поисках слова, означающего
#: пропуск. Строка сторожа переносится по-разному, а причина пропуска обычно
#: длинная — трёхсот символов хватает и на `pytestmark = pytest.mark.skipif(...)`,
#: и на `if not X.exists(): pytest.skip(...)`.
OKNO_PROPUSKA = 300


def _puti_konstant(istochnik: str) -> dict[str, str]:
    """Имя константы → путь относительно корня репозитория.

    Берём только те, что отсчитываются от корня: либо через `Path(__file__)`,
    либо через уже вычисленный `ROOT`/`KOREN`. Сам `ROOT` строковых кусков не
    содержит и сюда не попадает.
    """
    naydeno: dict[str, str] = {}
    for imya, vyrazhenie in NAZNACHENIE.findall(istochnik):
        ot_kornya = "Path(__file__)" in vyrazhenie or re.match(r"^(ROOT|KOREN)\b", vyrazhenie)
        if not ot_kornya:
            continue
        chasti = re.findall(r'"([^"]+)"', vyrazhenie)
        if chasti:
            naydeno[imya] = "/".join(chasti)
    return naydeno


def _storozhevye_puti() -> dict[str, str]:
    """Путь → файл проверок, который без него молча пропускается целиком."""
    naydeno: dict[str, str] = {}
    for fayl in sorted(KOREN.glob("tests/test_*.py")):
        istochnik = fayl.read_text(encoding="utf-8")
        konstanty = _puti_konstant(istochnik)
        for sovpadenie in PROVERKA_NALICHIYA.finditer(istochnik):
            imya = sovpadenie.group(1)
            if imya not in konstanty:
                continue
            # Смотрим по обе стороны: сторож пишется и как
            # `skipif(not X.exists(), ...)`, и как `if not X.exists(): skip(...)`.
            nachalo = max(0, sovpadenie.start() - OKNO_PROPUSKA)
            ryadom = istochnik[nachalo : sovpadenie.end() + OKNO_PROPUSKA]
            if "skip" not in ryadom:
                continue
            naydeno.setdefault(konstanty[imya], fayl.name)
    return naydeno


def _iskluchennye() -> set[str]:
    """Верхние составляющие путей, осознанно не попадающих в контекст сборки."""
    stroki = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return {
        s.strip().lstrip("!").rstrip("/").split("/")[0]
        for s in stroki
        if s.strip() and not s.strip().startswith("#")
    }


def _istochniki_copy() -> list[str]:
    """Что копируют ВСЕ этапы Dockerfile: этап `tests` наследует образ `app`."""
    istochniki: list[str] = []
    for stroka in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stroka = stroka.strip()
        if not stroka.upper().startswith("COPY "):
            continue
        chasti = [c for c in stroka.split()[1:] if not c.startswith("--")]
        # Последний аргумент — назначение внутри образа, он нас не касается.
        istochniki += chasti[:-1]
    return istochniki


def _kopiruetsya(put: str, istochniki: list[str]) -> bool:
    for istochnik in istochniki:
        if istochnik == put:
            return True
        if istochnik.endswith("/") and put.startswith(istochnik):
            return True
    return False


def test_storozhevye_fayly_voobshche_nashlis():
    """Сама эта проверка не должна выродиться в пустой цикл.

    Разбор идёт регулярными выражениями по исходникам, и перепишись сторожа
    иначе — список стал бы пустым, а обе проверки ниже — зелёными и слепыми.
    """
    puti = _storozhevye_puti()
    assert "opencrm.sh" in puti, (
        "разбор сторожей ничего не нашёл про opencrm.sh — проверки ниже стали "
        f"пустыми; найдено: {sorted(puti)}"
    )


def test_kazhdyy_storozhevoy_fayl_na_meste():
    """Пропущенных в образе гейта — столько же, сколько снаружи, а не больше.

    Красная строка здесь и означает «в этом образе часть набора молчит».
    Проверка про ФАЙЛЫ РЕПОЗИТОРИЯ: пропуски по отсутствию `sh`, `git` или
    из-за Windows — это про машину, они законны и различаются честно.
    """
    iskluchennye = _iskluchennye()
    propali = [
        f"{put} (без него молчит {fayl})"
        for put, fayl in sorted(_storozhevye_puti().items())
        if put.split("/")[0] not in iskluchennye and not (KOREN / put).exists()
    ]
    assert not propali, (
        "файлов нет рядом, и целые файлы проверок из-за этого пропускаются молча: "
        + "; ".join(propali)
    )


def test_etap_testov_kopiruet_vse_storozhevye_fayly():
    """Иначе гейт обновления собирает образ, в котором часть набора выключена.

    Ровно это и произошло с `opencrm.sh`: в `.dockerignore` он не назван, то
    есть в контекст сборки едет, — но ни одна строка `COPY` его не брала.
    """
    iskluchennye = _iskluchennye()
    istochniki = _istochniki_copy()
    ne_edut = [
        f"{put} (без него молчит {fayl})"
        for put, fayl in sorted(_storozhevye_puti().items())
        if put.split("/")[0] not in iskluchennye and not _kopiruetsya(put, istochniki)
    ]
    assert not ne_edut, (
        "этап tests в docker/Dockerfile не копирует файлы, от наличия которых "
        "зависят целые файлы проверок: " + "; ".join(ne_edut)
    )
