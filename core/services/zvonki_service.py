"""Планировщик звонков напоминаний: фоновый поток процесса (docs/bloki/29 §6).

Поток, а не таймер systemd: звонок должен прийти в свою минуту, а заход в
контейнер раз в минуту стоит секунду процессора и не даёт точности лучше минуты.
Процессов несколько — звонят все, а двойного звонка нет: его не пустит ключ
(напоминание, человек, минута) в `task_signals`.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from core import redis_client
from core.services import api_stats_service, board_service, notification_service, push_service, task_service
from core.utils import now_utc
from database.session import SessionLocal

#: Шаг, секунд. Окно звонка — пятнадцать минут назад, так что пропуск шага не теряет звонков.
SHAG = 20
#: Раз в сколько шагов доводить брошенные работы досок: примерно час.
UBORKA_KAZHDYE = 180

#: Уборка старого — раз в час на все процессы, а спрашивают о ней раз в пять минут.
#: Час, считанный от старта процесса, не наступал: прод обновляется чаще, и старые
#: звонки не убирались никогда (разбор 29.09.2026). Час держит ключ в Redis.
KLYUCH_UBORKI = f"{redis_client.PREFIX}zvonki:uborka"
UBORKA_SEKUND = 3600
PROVERKA_UBORKI = 15
#: Одна пачка — одна короткая транзакция; впервые включённая уборка на годовой
#: таблице иначе удаляла бы всё одним `DELETE`, держа замки строк до конца.
PACHKA_UBORKI = 5000
PREDEL_UBORKI_SEKUND = 60

#: Отметка последнего удачного шага (общая на процессы): окно звонка начинается от
#: неё. Жёсткие «15 минут назад» теряли всё, что должно было прозвенеть за простой
#: дольше окна: обновление с миграцией, откат, перезагрузка (разбор 29.09.2026).
KLYUCH_OTMETKI = f"{redis_client.PREFIX}zvonki:posledniy_shag"

_stop = threading.Event()
_potok: threading.Thread | None = None


def _nachalo_okna(teper: datetime) -> datetime:
    """От последнего удачного шага, но не раньше 12 часов и не позже 15 минут назад."""
    obychnoe = teper - task_service.OKNO_ZVONKA
    try:
        klient = redis_client.get_client()
        otmetka = klient.get(KLYUCH_OTMETKI) if klient is not None else None
        posledniy = datetime.fromisoformat(otmetka.decode() if isinstance(otmetka, bytes) else otmetka) if otmetka else None
    except Exception:  # noqa: BLE001 — нет отметки: окно обычное
        posledniy = None
    if posledniy is None:
        return obychnoe
    return max(teper - task_service.NASTOYCHIVO_OKNO, min(obychnoe, posledniy))


def _otmetit(teper: datetime) -> None:
    try:
        klient = redis_client.get_client()
        if klient is not None:
            klient.set(KLYUCH_OTMETKI, teper.isoformat(), ex=7 * 24 * 3600)
    except Exception:  # noqa: BLE001 — отметка удобство: без неё окно обычное
        pass


def shag() -> int:
    """Один шаг в своей сессии. Отказ шага не роняет поток — следующий повторит окно."""
    teper = now_utc()
    with SessionLocal() as db:
        zvonkov = task_service.tick(db, teper, _nachalo_okna(teper))
        ochered = db.info.pop(push_service.OCHERED, [])
        db.commit()
        _otmetit(teper)
        # В сеть — после фиксации: чужая служба не должна держать замки нашей базы.
        if ochered:
            push_service.razoslat(db, ochered)
            db.commit()
        return zvonkov


def _pora_ubirat(nomer: int) -> bool:
    """Один процесс в час; без Redis — каждый свой час от старта."""
    try:
        klient = redis_client.get_client()
        if klient is not None:
            return bool(klient.set(KLYUCH_UBORKI, "1", nx=True, ex=UBORKA_SEKUND))
    except Exception:  # noqa: BLE001 — Redis недоступен: считаем час сами
        pass
    return nomer % UBORKA_KAZHDYE == 0


def ubrat_staroe() -> dict[str, int]:
    """Старые уведомления, часы обращений по ключам и звонки — пачками, в пределе минуты."""
    konets = time.monotonic() + PREDEL_UBORKI_SEKUND
    itog: dict[str, int] = {}
    for imya, ubrat in (
        ("notifications", notification_service.ubrat_starye),
        ("api_key_hits", api_stats_service.ubrat_starye),
        ("task_signals", task_service.zvonki_ubrat_starye),
    ):
        vsego = 0
        while time.monotonic() < konets:
            with SessionLocal() as db:
                ushlo = ubrat(db, PACHKA_UBORKI)
                db.commit()
            vsego += ushlo
            if ushlo < PACHKA_UBORKI:
                break
        itog[imya] = vsego
    if any(itog.values()):
        print(f"[opencrm] уборка старого: {itog}")
    return itog


def _shag_kruga(nomer: int) -> None:
    shag()
    if nomer % PROVERKA_UBORKI == 0 and _pora_ubirat(nomer):
        ubrat_staroe()
    # Первый заход — через минуту после старта: брошенное выкладкой видно сразу.
    if nomer % UBORKA_KAZHDYE == 3:
        dovesti_v_storone()


def _krug() -> None:
    nomer = 0
    while not _stop.wait(SHAG):
        nomer += 1
        try:
            _shag_kruga(nomer)
        except Exception as exc:  # noqa: BLE001 — поток обязан пережить сбой базы
            print(f"[opencrm] звонки напоминаний: шаг не удался — {exc!r}")


_dovodka: threading.Thread | None = None


def dovesti_v_storone() -> None:
    """Доводка брошенных работ досок — своим потоком: три видео по пять минут в
    потоке звонков съели бы их пятнадцатиминутное окно (разбор 28.09.2026)."""
    global _dovodka
    if _dovodka is not None and _dovodka.is_alive():
        return

    def rabota() -> None:
        try:
            with SessionLocal() as db:
                board_service.dovesti_zastryavshie(db)
        except Exception as exc:  # noqa: BLE001 — поток доводки не роняет процесс
            print(f"[opencrm] доводка работ досок не удалась — {exc!r}")

    _dovodka = threading.Thread(target=rabota, daemon=True, name="board-dovodka")
    _dovodka.start()


def zapustit() -> None:
    global _potok
    if _potok is not None and _potok.is_alive():
        return
    _stop.clear()
    _potok = threading.Thread(target=_krug, daemon=True, name="task-signals")
    _potok.start()


def ostanovit() -> None:
    _stop.set()
