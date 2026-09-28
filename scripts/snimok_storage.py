"""Снимок storage для ночной копии: каталог, где неизменное — жёсткая ссылка на прошлый снимок.

Архив tar весил весь storage каждую ночь, и 7+4 копии съедали диск (28.09.2026:
7,9 ГБ за ночь при 22 ГБ свободных). Разбор — docs/ekspluatatsiya/08-razvyortyvanie.md,
«Копия файлов — снимком».

Запуск: python -m scripts.snimok_storage <storage> <каталог-снимка>
        python -m scripts.snimok_storage --vosstanovit <каталог-снимка> <storage>
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

#: Пишется последней: снимок без неё — оборванный, и проверка его не примет.
METKA = ".opencrm-snimok"


def gotov(snimok: Path) -> bool:
    return (snimok / METKA).is_file()


def proshlyy_snimok(cel: Path) -> Path | None:
    """Самый свежий ГОТОВЫЙ снимок рядом с целью (и в weekly), кроме неё самой."""
    kandidaty = []
    for katalog in (cel.parent, cel.parent.parent / "weekly"):
        if katalog.is_dir():
            kandidaty += [
                p for p in katalog.glob("storage-*")
                if p.is_dir() and p.name != cel.name and not p.name.endswith(".part") and gotov(p)
            ]
    return max(kandidaty, key=lambda p: p.name, default=None)


def _svyazat(st: os.stat_result, byl: Path, kuda: Path) -> bool:
    """Жёсткая ссылка на файл прошлого снимка, если это тот же файл. Нет — копируем."""
    try:
        prezhniy = byl.lstat()
        if not (
            stat.S_ISREG(prezhniy.st_mode)
            and prezhniy.st_size == st.st_size
            and prezhniy.st_mtime_ns == st.st_mtime_ns
        ):
            return False
        os.link(byl, kuda)
        return True
    except OSError:
        # Прошлого нет или ссылка невозможна (другой диск) — значит копия.
        return False


def sobrat(istochnik: Path, cel: Path, proshlyy: Path | None = None) -> dict:
    """Собрать снимок `istochnik` в `cel`. Черновик `.part` становится целью только готовым."""
    chernovik = cel.with_name(cel.name + ".part")
    shutil.rmtree(chernovik, ignore_errors=True)
    chernovik.mkdir(parents=True)
    itog = {"files": 0, "bytes": 0, "linked": 0, "copied": 0, "base": proshlyy.name if proshlyy else None}

    for koren, _papki, fayly in os.walk(istochnik):
        otn = Path(koren).relative_to(istochnik)
        if otn != Path("."):
            # Права вложенных каталогов — как у оригинала: восстановленный storage
            # отдаёт nginx. Корень снимка остаётся 700 — посторонним внутрь хода нет.
            (chernovik / otn).mkdir()
            os.chmod(chernovik / otn, stat.S_IMODE(Path(koren).stat().st_mode))
        for imya in fayly:
            otkuda = Path(koren) / imya
            st = otkuda.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            kuda = chernovik / otn / imya
            if proshlyy is not None and _svyazat(st, proshlyy / otn / imya, kuda):
                itog["linked"] += 1
            else:
                shutil.copy2(otkuda, kuda)
                itog["copied"] += 1
            itog["files"] += 1
            itog["bytes"] += st.st_size

    (chernovik / METKA).write_text(json.dumps(itog), encoding="utf-8")
    shutil.rmtree(cel, ignore_errors=True)
    chernovik.rename(cel)
    return itog


def vosstanovit(snimok: Path, storage: Path) -> int:
    """Разложить снимок поверх storage. Права самого storage не трогаем — только содержимое."""
    if not gotov(snimok):
        raise SystemExit(f"снимок {snimok} не дособран — метки {METKA} нет")
    skolko = 0
    storage.mkdir(parents=True, exist_ok=True)
    for koren, _papki, fayly in os.walk(snimok):
        otn = Path(koren).relative_to(snimok)
        if otn != Path("."):
            (storage / otn).mkdir(exist_ok=True)
            os.chmod(storage / otn, stat.S_IMODE(Path(koren).stat().st_mode))
        for imya in fayly:
            if otn == Path(".") and imya == METKA:
                continue
            shutil.copy2(Path(koren) / imya, storage / otn / imya)
            skolko += 1
    return skolko


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--vosstanovit":
        print(f"snimok: разложено {vosstanovit(Path(argv[1]), Path(argv[2]))} файлов")
        return 0
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    istochnik, cel = Path(argv[0]), Path(argv[1])
    if not istochnik.is_dir():
        print(f"snimok: нет каталога {istochnik}", file=sys.stderr)
        return 1
    itog = sobrat(istochnik, cel, proshlyy_snimok(cel))
    print(
        f"snimok: {itog['files']} файлов, скопировано {itog['copied']}, "
        f"ссылками {itog['linked']} (основа — {itog['base'] or 'нет'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
