"""Кэш сборки docker на экране «Обслуживание»: что знает служба обновления и просьба почистить.

Разбор — docs/ekspluatatsiya/08-razvyortyvanie.md, «Кэш сборки». Приложение docker
не видит: чистит служба обновления на хосте, а мы только кладём просьбу в общий
`data/` и читаем её итог. Имена файлов — те же, что в `deploy/kesh.py`
(сверяет `tests/test_kesh_sborki.py`): тот каталог в образ приложения не едет.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import BASE_DIR
from core.services import audit_service
from database.models import User
from database.models.audit import SOURCE_MANUAL

PAPKA = "obsluzhivanie"
SOSTOYANIE = "kesh-sborki.json"
ZAPROS = "kesh-sborki-zapros.json"
#: Служба забирает просьбу за 15 секунд; лежит дольше — службы, видимо, нет.
ZAPROS_ZABYT_SEKUND = 600


def _papka() -> Path:
    koren = os.environ.get("OPENCRM_DATA_DIR")
    return (Path(koren) if koren else BASE_DIR / "data") / PAPKA


def _prochest(put: Path) -> dict | None:
    try:
        return json.loads(put.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _teper() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sostoyanie() -> dict:
    """Последний замер и чистка — со слов службы; `sluzhba=False` — она ничего не писала."""
    dannye = _prochest(_papka() / SOSTOYANIE)
    zapros = _prochest(_papka() / ZAPROS)
    zabyt = False
    if zapros and zapros.get("at"):
        try:
            kogda = datetime.strptime(zapros["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            zabyt = (datetime.now(timezone.utc) - kogda).total_seconds() > ZAPROS_ZABYT_SEKUND
        except ValueError:
            zabyt = True
    return {
        "sluzhba": dannye is not None,
        "izmereno": (dannye or {}).get("izmereno"),
        "razmer": (dannye or {}).get("razmer"),
        "osvobodimo": (dannye or {}).get("osvobodimo"),
        "svobodno_mb": (dannye or {}).get("svobodno_mb"),
        "ostavlyat_gb": (dannye or {}).get("ostavlyat_gb"),
        "poslednyaya": (dannye or {}).get("poslednyaya"),
        "zapros": zapros,
        "zapros_zabyt": zabyt,
    }


def zaprosit(db: Session, actor: User) -> dict:
    """Попросить службу обновления почистить кэш целиком. Вторая просьба до первой — та же."""
    papka = _papka()
    put = papka / ZAPROS
    if not put.exists():
        papka.mkdir(parents=True, exist_ok=True)
        vremennyy = papka / (ZAPROS + ".tmp")
        vremennyy.write_text(
            json.dumps({"at": _teper(), "kto": actor.name, "user_id": actor.id}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(vremennyy, put)
        audit_service.record(
            db,
            actor=actor,
            source=SOURCE_MANUAL,
            action=audit_service.ACTION_BUILD_CACHE_REQUESTED,
            entity_type=audit_service.ENTITY_MODULE,
            entity_label="build-cache",
        )
    return sostoyanie()
