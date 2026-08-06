"""Какие блоки системы включены у этого бизнеса.

Реестр блоков — в core/modules.py, здесь только состояние и правила переключения.

Состояние спрашивают на каждом запросе к защищённому блоку, поэтому держим
короткий кэш — иначе каждый чих ходил бы в базу за одной строкой. Две секунды,
как и у режима обслуживания: столько же составляет расхождение между процессами
uvicorn, если их несколько, и столько же ждёт root между «выключил» и «пропало
из меню».
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import modules
from core.services import audit_service
from core.utils import now_utc
from database.models import ModuleState, User
from database.repositories import modules as modules_repo

CACHE_SECONDS = 2.0

_cache: dict[str, bool] | None = None
_cached_at: float = 0.0


def _now() -> float:
    return now_utc().timestamp()


def invalidate() -> None:
    """Сбросить кэш — зовётся сразу после переключения, чтобы не ждать секунды."""
    global _cache, _cached_at
    _cache = None
    _cached_at = 0.0


def state(db: Session) -> dict[str, bool]:
    """Ключ блока → включён ли он. Ответ всегда содержит все блоки реестра."""
    global _cache, _cached_at
    if _cache is not None and _now() - _cached_at < CACHE_SECONDS:
        # Копия, а не сам кэш: возвращённый словарь правят вызывающие (дашборд
        # так и делает), и правка уходила бы в состояние блоков всего процесса.
        return dict(_cache)

    result = _read_state(db)
    _cache = result
    _cached_at = _now()
    return dict(result)


def _read_state(db: Session) -> dict[str, bool]:
    """Состояние блоков по базе, мимо кэша. Реестр главнее строки в базе."""
    stored = modules_repo.enabled_map(db)
    result: dict[str, bool] = {}
    for module in modules.MODULES:
        if module.core:
            # Несущий блок включён всегда, что бы ни лежало в базе: на нём
            # держатся остальные, и случайная строка не должна его гасить.
            result[module.key] = True
        elif not module.ready:
            # Блок ещё не написан. Даже если его когда-то включили, включённым
            # он быть не может — иначе меню обещало бы несуществующий раздел.
            result[module.key] = False
        else:
            result[module.key] = stored.get(module.key, module.default)
    return result


def is_enabled(db: Session, key: str) -> bool:
    return state(db).get(key, False)


def set_enabled(db: Session, key: str, enabled: bool, user: User) -> dict[str, bool]:
    """Включить или выключить блок.

    Отказ здесь — не придирка. Выключить то, на чём держится другой включённый
    блок, значит получить раздел, который открывается и падает; включить блок
    без его основания — раздел, которому не на что опереться. В обоих случаях
    честнее отказать с указанием причины, чем молча увести за собой соседей.
    """
    module = modules.get(key)
    if module is None:
        raise errors.ValidationError(f"Unknown module: {key}", code="unknown_module")
    if module.core:
        raise errors.ValidationError(
            f"Module '{key}' is part of the core and cannot be switched off",
            code="module_is_core",
        )
    if not module.ready:
        raise errors.ValidationError(
            f"Module '{key}' is not built yet", code="module_not_ready"
        )

    current = state(db)
    if enabled:
        missing = [dep for dep in module.requires if not current.get(dep)]
        if missing:
            raise errors.ValidationError(
                f"Module '{key}' needs these switched on first: {', '.join(missing)}",
                code="module_requires",
            )
    else:
        blocking = [dep for dep in modules.dependents_of(key) if current.get(dep)]
        if blocking:
            raise errors.ValidationError(
                f"Module '{key}' is still needed by: {', '.join(blocking)}",
                code="module_required_by",
            )

    # Состояние до переключения. `state` всегда содержит все блоки реестра,
    # поэтому ключ на месте, даже если строки в базе ещё нет.
    was = current[key]
    row = modules_repo.get(db, key)
    if row is None:
        db.add(ModuleState(key=key, enabled=enabled, updated_by=user.id))
    else:
        row.enabled = enabled
        row.updated_by = user.id
        row.updated_at = now_utc()
    db.flush()
    # Выключенный блок исчезает из меню, из API и из отчётов целиком. Вопрос
    # «куда делся раздел» задают на следующий день после того, как его выключили,
    # и `module_states.updated_by` отвечает только про последнее переключение —
    # предыдущие он затирает собой.
    audit_service.record(
        db,
        action=audit_service.ACTION_MODULE_SWITCHED,
        actor=user,
        source=audit_service.SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_MODULE,
        # У блока нет числового идентификатора — ключ и есть его имя.
        entity_label=key,
        before="on" if was else "off",
        after="on" if enabled else "off",
    )
    invalidate()
    # Кэш НЕ набиваем здесь. `state(db)` внутри незакоммиченной транзакции
    # положил бы в глобальный кэш то, что видит только эта сессия: откатись она
    # (упавший commit, «database is locked», ошибка дальше по запросу) — и весь
    # процесс до двух секунд ведёт себя так, будто блок выключен, хотя в базе
    # ничего не менялось. Меню, гварды маршрутов и гейт подписчиков читают
    # именно этот кэш.
    #
    # Ответ собираем из реестра и текущей сессии, ничего не запоминая: он верен
    # для этого запроса, а следующий прочитает базу заново — уже после коммита.
    return _read_state(db)


def details(db: Session) -> list[dict]:
    """Полная картина для экрана настроек: состояние плюс причины, почему нельзя."""
    current = state(db)
    stamps = {row.key: row for row in modules_repo.all_rows(db)}
    result = []
    for module in modules.MODULES:
        row = stamps.get(module.key)
        result.append(
            {
                "key": module.key,
                "enabled": current[module.key],
                "core": module.core,
                "ready": module.ready,
                "requires": list(module.requires),
                # Кто держит этот блок включённым: показываем в подсказке, чтобы
                # отказ переключить не выглядел необъяснимым.
                "required_by": [
                    dep for dep in modules.dependents_of(module.key) if current.get(dep)
                ],
                "updated_at": row.updated_at.isoformat() if row else None,
                "updated_by": row.updated_by if row else None,
            }
        )
    return result
