"""Справочник ручек API сайта для экрана документации — из `docs/osnovy/04-api.md`.

На экран идёт ТОЛЬКО то, что зовёт чужая программа по ключу: раздел «API сайта
магазина» справочника, ручки под ключом и снимок товара. Остальные ручки зовёт
сам экран CRM, и внешнему они не нужны — владелец снял их 05.09.2026, когда
на экране оказался весь внутренний API.

Перечень не пишется руками: второй список по тем же ручкам разошёлся бы с
документом на первой же новой. Свежесть порождённого файла стережёт
`tests/test_spravochnik_api.py`, полноту справочника — `test_api_dokumentirovan`.

Запуск после правки `docs/osnovy/04-api.md`:

    python scripts/spravochnik_api.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

KOREN = pathlib.Path(__file__).resolve().parent.parent
SPRAVOCHNIK = KOREN / "docs" / "osnovy" / "04-api.md"
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
                # Витрина и снимки товара живут вне `/api/v1`, и приставка у них
                # сбила бы с толку.
                "vne_api": "вне /api" in razdel["nazvanie"] or sovpalo.group(2).startswith("/media/"),
            }
        )
    return [r for r in razdely if r["ruchki"]]


RAZDEL_SAYTA = "API сайта магазина"


def dlya_sayta(razdely: list[dict]) -> list[dict]:
    """Ручки под ключом из раздела API сайта, разложенные по его подразделам.

    Настройки ключей, `/live` и соседние ручки CRM из того же раздела остаются
    за бортом: их зовёт экран сотрудника, а не сайт.
    """
    razdel = next((r for r in razdely if r["nazvanie"].startswith(RAZDEL_SAYTA)), None)
    if razdel is None:
        raise RuntimeError(f"в справочнике нет раздела «{RAZDEL_SAYTA}»")
    gruppy: list[dict] = []
    for ruchka in razdel["ruchki"]:
        if ruchka["vid"] not in ("klyuch", "inoe"):
            continue
        imya = ruchka["podrazdel"] or razdel["nazvanie"]
        gruppa = next((g for g in gruppy if g["nazvanie"] == imya), None)
        if gruppa is None:
            gruppa = {"nazvanie": imya, "ruchki": []}
            gruppy.append(gruppa)
        gruppa["ruchki"].append({**ruchka, "podrazdel": ""})
    return gruppy


def porodit(tekst: str) -> str:
    """Текст файла для экрана. Порождённый — руками не правится."""
    dannye = json.dumps(dlya_sayta(razobrat(tekst)), ensure_ascii=False, indent=2)
    return (
        "// Порождено скриптом scripts/spravochnik_api.py из раздела «API сайта магазина» docs/osnovy/04-api.md.\n"
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
    schyot = sum(len(r["ruchki"]) for r in dlya_sayta(razobrat(SPRAVOCHNIK.read_text(encoding="utf-8"))))
    print(f"записано: {VYKHOD.relative_to(KOREN)}, ручек {schyot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
