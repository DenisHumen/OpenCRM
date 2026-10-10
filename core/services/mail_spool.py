"""Почтовый сервер на этой машине: просьба обновлятору и её журнал (docs/bloki/32-pochtovyy-server.md).

У приложения нет доступа к docker и не должно быть: ставит сервер обновлятор на
хосте (`deploy/pochta.py`), а говорим мы с ним файлами в общем `data/` — как с
кэшем сборки. Имена — те же, что в `deploy/pochta.py` (сверяет
`tests/test_pochta_server.py`): тот каталог в образ приложения не едет.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from core import exceptions as errors

PAPKA = "obsluzhivanie"
ZAPROS = "pochta-zapros.json"
ZHURNAL = "pochta-zhurnal-{job}.log"
ITOG = "pochta-itog-{job}.json"
OBNOVLYATOR = "pochta-obnovlyator.json"
OPERATSII = ("check", "install", "remove", "adopt")
#: Обновлятор отмечается каждые 15 секунд; молчит две минуты — его нет или он занят.
ZHIV_SEKUND = 120
#: Кусок журнала за один опрос экрана — экран просит раз в полторы секунды.
KUSOK_ZHURNALA = 256 * 1024


def papka() -> Path:
    koren = os.environ.get("OPENCRM_DATA_DIR")
    return (Path(koren) if koren else BASE_DIR / "data") / PAPKA


def _teper() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prochest(put: Path) -> dict | None:
    try:
        return json.loads(put.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _vremya(tekst) -> datetime | None:
    try:
        return datetime.strptime(str(tekst), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def obnovlyator() -> dict:
    """Жив ли обновлятор: его отметка свежее `ZHIV_SEKUND`."""
    dannye = _prochest(papka() / OBNOVLYATOR) or {}
    kogda = _vremya(dannye.get("at"))
    zhiv = kogda is not None and (datetime.now(timezone.utc) - kogda).total_seconds() < ZHIV_SEKUND
    return {"zhiv": zhiv, "at": dannye.get("at")}


def zapros() -> dict | None:
    return _prochest(papka() / ZAPROS)


def zaprosit(job_id: int, op: str, env: dict[str, str], kto: str) -> None:
    """Положить просьбу: атомарно и 0600 — в ней пароли. Чужая ещё лежит — занято."""
    if op not in OPERATSII:
        raise ValueError(op)
    kuda = papka()
    kuda.mkdir(parents=True, exist_ok=True)
    put = kuda / ZAPROS
    lezhit = _prochest(put)
    if lezhit is not None and lezhit.get("job_id") != job_id:
        raise errors.ConflictError("Another mail server job is waiting for the updater", code="mail_server_busy")
    vremennyy = kuda / (ZAPROS + ".tmp")
    fd = os.open(vremennyy, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"job_id": job_id, "op": op, "env": env, "at": _teper(), "kto": kto}, f, ensure_ascii=False)
    os.chmod(vremennyy, 0o600)
    os.replace(vremennyy, put)


def otozvat(job_id: int) -> bool:
    """Убрать свою просьбу, пока обновлятор её не взял. True — успели."""
    put = papka() / ZAPROS
    lezhit = _prochest(put)
    if lezhit is not None and lezhit.get("job_id") == job_id:
        put.unlink(missing_ok=True)
        return True
    return False


def vzyata(job_id: int) -> bool:
    """Обновлятор забрал просьбу: её больше нет, а журнал или итог появились."""
    lezhit = _prochest(papka() / ZAPROS)
    if lezhit is not None and lezhit.get("job_id") == job_id:
        return False
    return (papka() / ZHURNAL.format(job=job_id)).exists() or (papka() / ITOG.format(job=job_id)).exists()


def zhurnal(job_id: int, s_bayta: int = 0) -> tuple[bytes, int]:
    """Кусок журнала с байта `s_bayta` и его полный размер."""
    put = papka() / ZHURNAL.format(job=job_id)
    try:
        razmer = put.stat().st_size
        with open(put, "rb") as f:
            f.seek(max(0, min(s_bayta, razmer)))
            return f.read(KUSOK_ZHURNALA), razmer
    except OSError:
        return b"", 0


def itog(job_id: int) -> dict | None:
    return _prochest(papka() / ITOG.format(job=job_id))


def ubrat(job_id: int) -> None:
    """Журнал и итог переписаны в базу — файлы больше не нужны."""
    for imya in (ZHURNAL.format(job=job_id), ITOG.format(job=job_id)):
        (papka() / imya).unlink(missing_ok=True)
