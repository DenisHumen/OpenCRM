"""Ручной режим обслуживания: root закрывает сайт, себе доступ оставляет.

Не путать с заглушкой nginx (`docker/nginx/maintenance/maintenance.html`): та
показывается, когда приложения нет вовсе — во время обновления или падения.
Эта включается руками, когда приложение работает: root правит данные, переносит
базу, разбирается с проблемой — и не хочет, чтобы в это время кто-то ещё писал
в CRM или открывал витрины.

Состояние читается на каждом запросе, поэтому держим короткий кэш: без него
каждый запрос за картинкой шёл бы в базу за одной строкой. Две секунды — предел
задержки между «root выключил» и «посетитель увидел сайт»; столько же составляет
расхождение между процессами uvicorn, если их несколько.
"""

from sqlalchemy.orm import Session

from core.services import settings_service
from core.utils import now_utc

CACHE_SECONDS = 2.0

_cache: dict | None = None
_cached_at: float = 0.0


def _now() -> float:
    return now_utc().timestamp()


def invalidate() -> None:
    """Сбросить кэш — зовётся сразу после переключения, чтобы не ждать секунды."""
    global _cache, _cached_at
    _cache = None
    _cached_at = 0.0


def state(db: Session) -> dict:
    global _cache, _cached_at
    if _cache is not None and _now() - _cached_at < CACHE_SECONDS:
        return _cache
    values = settings_service.get_all(db)
    _cache = {
        "enabled": values.get("maintenance_mode") == "1",
        "note": values.get("maintenance_note", ""),
        "since": values.get("maintenance_since", ""),
        "by": values.get("maintenance_by", ""),
    }
    _cached_at = _now()
    return _cache


def set_mode(db: Session, enabled: bool, note: str, who: str) -> dict:
    """Включить или выключить режим. Отметку о том, кто и когда, ставим здесь.

    Выключение чистит и пояснение: оставшийся от прошлого раза текст «вернёмся к
    14:00» на следующем обслуживании ввёл бы в заблуждение сильнее, чем его
    отсутствие.
    """
    values = {
        "maintenance_mode": "1" if enabled else "0",
        "maintenance_note": (note or "").strip()[:200] if enabled else "",
        "maintenance_since": now_utc().isoformat() if enabled else "",
        "maintenance_by": who if enabled else "",
    }
    settings_service.update(db, values)
    invalidate()
    return state(db)
