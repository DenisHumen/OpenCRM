"""Планировщик звонков напоминаний: фоновый поток процесса (docs/bloki/29 §6).

Поток, а не таймер systemd: звонок должен прийти в свою минуту, а заход в
контейнер раз в минуту стоит секунду процессора и не даёт точности лучше минуты.
Процессов несколько — звонят все, а двойного звонка нет: его не пустит ключ
(напоминание, человек, минута) в `task_signals`.
"""

from __future__ import annotations

import threading

from core.services import task_service
from database.session import SessionLocal

#: Шаг, секунд. Окно звонка — пятнадцать минут назад, так что пропуск шага не теряет звонков.
SHAG = 20
#: Раз в сколько шагов убирать старые звонки: примерно час.
UBORKA_KAZHDYE = 180

_stop = threading.Event()
_potok: threading.Thread | None = None


def shag() -> int:
    """Один шаг в своей сессии. Отказ шага не роняет поток — следующий повторит окно."""
    with SessionLocal() as db:
        zvonkov = task_service.tick(db)
        db.commit()
        return zvonkov


def _krug() -> None:
    nomer = 0
    while not _stop.wait(SHAG):
        nomer += 1
        try:
            shag()
            if nomer % UBORKA_KAZHDYE == 0:
                with SessionLocal() as db:
                    task_service.zvonki_ubrat_starye(db)
                    db.commit()
        except Exception as exc:  # noqa: BLE001 — поток обязан пережить сбой базы
            print(f"[opencrm] звонки напоминаний: шаг не удался — {exc!r}")


def zapustit() -> None:
    global _potok
    if _potok is not None and _potok.is_alive():
        return
    _stop.clear()
    _potok = threading.Thread(target=_krug, daemon=True, name="task-signals")
    _potok.start()


def ostanovit() -> None:
    _stop.set()
