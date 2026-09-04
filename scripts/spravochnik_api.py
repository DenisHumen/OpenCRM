"""Справочник ручек для экрана документации — из `docs/04-api.md`.

Перечень ручек в приложении не пишется руками: второй список по тем же ручкам
разошёлся бы с документом на первой же новой. Поэтому экран читает то, что
породил этот скрипт из справочника, а свежесть порождённого файла стережёт
`tests/test_spravochnik_api.py`. Полноту самого справочника стережёт
`tests/test_api_dokumentirovan.py` — так на экране оказывается каждая ручка.

Запуск после правки `docs/04-api.md`:

    python scripts/spravochnik_api.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

KOREN = pathlib.Path(__file__).resolve().parent.parent
SPRAVOCHNIK = KOREN / "docs" / "04-api.md"
VYKHOD = KOREN / "web" / "frontend" / "crm" / "src" / "lib" / "spravochnik_api.ts"

STROKA = re.compile(r"^\| (GET|POST|PATCH|PUT|DELETE) \| `([^`]+)` \|(.*)\|\s*$")
PRAVO = re.compile(r"^🔑\s*`([a-z_]+\.[a-z_]+)`$")
OBLAST = re.compile(r"^`([a-z_]+\.[a-z_]+)`$")


def _dostup(yacheyka: str) -> tuple[str, str]:
    """Вид доступа для значка и его подпись без обозначений."""
    if yacheyka == "🔓":
        return "otkryto", ""
    if yacheyka == "👤":
        return "sotrudnik", ""
    if pravo := PRAVO.match(yacheyka):
        return "pravo", pravo.group(1)
    if oblast := OBLAST.match(yacheyka):
        return "klyuch", oblast.group(1)
    return "inoe", yacheyka


def razobrat(tekst: str) -> list[dict]:
    """Разделы справочника с их ручками; разделы без ручек не попадают."""
    razdely: list[dict] = []
    razdel: dict | None = None
    podrazdel = ""
    for stroka in tekst.split("\n"):
        if stroka.startswith("## "):
            razdel = {"nazvanie": stroka[3:].strip(), "ruchki": []}
            razdely.append(razdel)
            podrazdel = ""
            continue
        if stroka.startswith("### "):
            podrazdel = stroka[4:].strip()
            continue
        sovpalo = STROKA.match(stroka)
        if not sovpalo or razdel is None:
            continue
        yacheyki = [ch.strip() for ch in sovpalo.group(3).split(" | ")]
        # Таблица витрины идёт без колонки прав: там всё открыто по устройству.
        if len(yacheyki) == 1:
            vid, podpis = "otkryto", ""
        else:
            vid, podpis = _dostup(yacheyki[0])
        razdel["ruchki"].append(
            {
                "metod": sovpalo.group(1),
                "put": sovpalo.group(2),
                "vid": vid,
                "dostup": podpis,
                "opisanie": " | ".join(yacheyki[1:]) if len(yacheyki) > 1 else yacheyki[0],
                "podrazdel": podrazdel,
                # Витрина живёт вне `/api/v1`, и приставка у неё сбила бы с толку.
                "vne_api": "вне /api" in razdel["nazvanie"],
            }
        )
    return [r for r in razdely if r["ruchki"]]


def porodit(tekst: str) -> str:
    """Текст файла для экрана. Порождённый — руками не правится."""
    dannye = json.dumps(razobrat(tekst), ensure_ascii=False, indent=2)
    return (
        "// Порождено скриптом scripts/spravochnik_api.py из docs/04-api.md.\n"
        "// Руками не править: правится справочник, потом запускается скрипт.\n"
        "\n"
        "export type VidDostupa = \"otkryto\" | \"sotrudnik\" | \"pravo\" | \"klyuch\" | \"inoe\";\n"
        "\n"
        "export type Ruchka = {\n"
        "  metod: string;\n"
        "  put: string;\n"
        "  vid: VidDostupa;\n"
        "  dostup: string;\n"
        "  opisanie: string;\n"
        "  podrazdel: string;\n"
        "  vne_api: boolean;\n"
        "};\n"
        "\n"
        "export type RazdelApi = { nazvanie: string; ruchki: Ruchka[] };\n"
        "\n"
        f"export const SPRAVOCHNIK_API: RazdelApi[] = {dannye};\n"
    )


def main() -> int:
    novyy = porodit(SPRAVOCHNIK.read_text(encoding="utf-8"))
    byl = VYKHOD.read_text(encoding="utf-8") if VYKHOD.exists() else ""
    if novyy == byl:
        print("справочник свеж")
        return 0
    VYKHOD.write_text(novyy, encoding="utf-8", newline="\n")
    schyot = sum(len(r["ruchki"]) for r in razobrat(SPRAVOCHNIK.read_text(encoding="utf-8")))
    print(f"записано: {VYKHOD.relative_to(KOREN)}, ручек {schyot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
