"""Кэш сборки docker: замер, чистка и файл состояния для экрана «Обслуживание».

Разбор — docs/ekspluatatsiya/08-razvyortyvanie.md, «Кэш сборки». Приложение
docker не видит и видеть не должно, поэтому говорит с обновлятором файлами в
общем `data/`: просьба туда кладётся экраном, итог — отсюда. Имена продублированы
в `core/services/kesh_sborki_service.py`: этот каталог в образ приложения не едет.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

PAPKA = "obsluzhivanie"
SOSTOYANIE = "kesh-sborki.json"
ZAPROS = "kesh-sborki-zapros.json"

_EDINITSY = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def v_bayty(tekst: str) -> int | None:
    """«45.61GB», «1.911GB (28%)», «0B» — как пишет `docker system df` (десятичные единицы)."""
    najdeno = re.match(r"\s*([\d.]+)\s*([kKMGT]?B)\b", tekst or "")
    if not najdeno:
        return None
    return round(float(najdeno.group(1)) * _EDINITSY[najdeno.group(2).upper()])


def chelovecheski(bayt: int | None) -> str:
    if bayt is None:
        return "—"
    for edinitsa, delitel in (("ГБ", 1000**3), ("МБ", 1000**2), ("КБ", 1000)):
        if bayt >= delitel:
            return f"{bayt / delitel:.1f}".replace(".", ",") + f" {edinitsa}"
    return f"{bayt} Б"


def izmerit(shell, cwd: Path) -> dict | None:
    """Размер кэша сборки и сколько из него можно освободить. `None` — docker не ответил."""
    rezultat = shell.run(["docker", "system", "df", "--format", "{{json .}}"], cwd=cwd, timeout=180)
    if rezultat.code != 0:
        return None
    for stroka in rezultat.out.splitlines():
        try:
            zapis = json.loads(stroka)
        except ValueError:
            continue
        if zapis.get("Type") == "Build Cache":
            return {"razmer": v_bayty(zapis.get("Size", "")), "osvobodimo": v_bayty(zapis.get("Reclaimable", ""))}
    return None


def komandy(vsyo: bool, ostavit_gb: int) -> list[list[str]]:
    """Варианты чистки по убыванию точности: следующий — если docker не знает флага.

    Всё — `-a`: вручную просят освободить место, и следующая сборка честно
    пойдёт с нуля. После обновления кэш не стирается, а ужимается до порога:
    свежие слои остаются, и следующее обновление не качает всё заново.
    """
    if vsyo:
        return [["docker", "builder", "prune", "-a", "-f"]]
    predel = f"{ostavit_gb}GB"
    return [
        ["docker", "builder", "prune", "-f", "--max-used-space", predel],
        ["docker", "builder", "prune", "-f", "--keep-storage", predel],
        ["docker", "builder", "prune", "-f", "--filter", "until=24h"],
    ]


def ne_znaet_flaga(rezultat) -> bool:
    tekst = f"{rezultat.out}\n{rezultat.err}".lower()
    return "unknown flag" in tekst or "flag provided but not defined" in tekst


def papka(data_dir: Path) -> Path:
    return data_dir / PAPKA


def prochest(put: Path) -> dict | None:
    try:
        return json.loads(put.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def zapisat(data_dir: Path, sostoyanie: dict) -> None:
    """Целиком и подменой: экран не должен прочесть файл, записанный наполовину."""
    kuda = papka(data_dir)
    kuda.mkdir(parents=True, exist_ok=True)
    vremennyy = kuda / (SOSTOYANIE + ".tmp")
    vremennyy.write_text(json.dumps(sostoyanie, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(vremennyy, kuda / SOSTOYANIE)
