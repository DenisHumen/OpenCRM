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


def affected_by(db: Session, key: str, enabled: bool) -> list[str]:
    """Кого ещё придётся переключить вместе с этим блоком. Без самого блока.

    Отвечает на вопрос интерфейса «что случится, если я нажму» — до нажатия.
    Молча уводить за собой соседние разделы нельзя: человек выключает склад,
    а из меню пропадают ещё и наклейки с их настройками, и связать одно с
    другим ему нечем.
    """
    current = state(db)
    if enabled:
        return [dep for dep in modules.requirements_tree(key) if not current.get(dep)]
    return [dep for dep in modules.dependents_tree(key) if current.get(dep)]


def set_enabled(db: Session, key: str, enabled: bool, user: User) -> dict[str, bool]:
    """Включить или выключить блок — вместе со всем, что от него зависит.

    **Зависимости не отказ, а последовательность.** Выключаешь склад — гаснут и
    наклейки, которые без него не имеют смысла; включаешь наклейки — поднимается
    склад, без которого им не на что опереться. Прежде система в обоих случаях
    отказывала и называла причину, и это было честно, но перекладывало на
    человека работу, которую может сделать сама: он читал «блок ещё нужен вот
    этим», шёл выключать их по одному и возвращался. При цепочке в три звена
    порядок приходилось угадывать.

    Уводя соседей, система обязана **сказать, кого увела** — это делает ответ
    (`state` целиком) и запись в журнале на каждый переключённый блок. А
    предупредить заранее — дело интерфейса, для того и `affected_by`.

    Порядок важен: гасим снизу вверх (сначала тех, кто стоит выше), включаем
    сверху вниз (сначала основание). Иначе на середине цепочки окажется блок,
    чьё основание уже погасили.
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

    # Ненаписанный блок нельзя поднять и заодно: он бы включился «за компанию»
    # и обещал раздел, которого нет. Такую цепочку честнее не начинать вовсе.
    for dependency in affected_by(db, key, enabled):
        neighbour = modules.get(dependency)
        if enabled and neighbour is not None and not neighbour.ready:
            raise errors.ValidationError(
                f"Module '{key}' needs '{dependency}', which is not built yet",
                code="module_requires_unbuilt",
            )

    current = state(db)
    # Сам блок — последним при выключении (сначала гаснут стоящие на нём) и
    # последним при включении (сначала поднимается основание).
    for target in [*affected_by(db, key, enabled), key]:
        _write_state(db, target, enabled, user, was=current.get(target, False))
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
                # Кто стоит на этом блоке — все, не только включённые: экран
                # рисует связь целиком, а не только её работающую половину.
                "required_by": list(modules.dependents_of(module.key)),
                # Что произойдёт при нажатии — посчитано здесь, а не угадано
                # экраном. Угадывать пришлось бы обходом дерева на фронтенде,
                # то есть тем же правилом во втором экземпляре; такие два
                # экземпляра расходятся, и расходятся молча.
                "off_takes": [
                    dep for dep in modules.dependents_tree(module.key) if current.get(dep)
                ],
                "on_needs": [
                    dep
                    for dep in modules.requirements_tree(module.key)
                    if not current.get(dep)
                ],
                "updated_at": row.updated_at.isoformat() if row else None,
                "updated_by": row.updated_by if row else None,
            }
        )
    return result


def _write_state(db: Session, key: str, enabled: bool, user: User, *, was: bool) -> None:
    """Записать состояние одного блока и след в журнале.

    Запись делается даже когда значение не меняется — при каскаде так проще
    рассуждать, а лишней она не будет: журнал пишется только для тех, кого
    `affected_by` уже отобрал как «придётся переключить», то есть значение у
    них другое по построению.
    """
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
    # предыдущие он затирает собой. Тем более это верно для каскада: блок мог
    # погаснуть не сам по себе, а вслед за соседом, и в журнале это видно по
    # двум записям одной секундой.
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


def apply_preset(db: Session, key: str, user: User) -> dict:
    """Включить набор блоков под тип дела. Возвращает, что получилось.

    **Только включает, ничего не выключает.** Забрать раздел с данными одним
    нажатием — самый быстрый способ потерять доверие: человек выбрал «Розница»,
    а из меню пропали доски, на которых лежит полгода работы. Поэтому блоки вне
    набора остаются как есть, а не гасятся «для чистоты».

    Идёт **тем же путём, что и переключатель руками** — через `set_enabled`, со
    всеми зависимостями и записями в журнал. Своего пути записи у набора быть не
    должно: он однажды включил бы заказы без бланков, и разбираться пришлось бы
    в двух местах сразу.

    Тип дела при этом **никуда не запоминается**. Набор — точка отсчёта, а не
    режим работы: выбор это нажатие пяти переключателей за один раз, и только.
    Заведи мы «режим розницы», и через полгода в коде будет тридцать развилок
    `if тип == ...` (разбор — в шапке `core/modules.py`).
    """
    chosen = modules.preset(key)
    if chosen is None:
        raise errors.ValidationError(f"Unknown preset: {key}", code="unknown_preset")

    before = state(db)
    turned_on: list[str] = []
    for module_key in chosen.modules:
        if before.get(module_key):
            continue
        # `set_enabled` сам поднимет то, на чём блок стоит, и в верном порядке.
        set_enabled(db, module_key, True, user)
        turned_on.append(module_key)

    after = _read_state(db)
    return {
        "preset": chosen.key,
        # Что именно включилось — вместе с поднятым по зависимостям: человек
        # выбрал «Розница», а появились ещё и заявки, на которых стоит склад.
        # Не сказать об этом значит оставить в меню разделы, о которых не
        # договаривались.
        "enabled": sorted(k for k, on in after.items() if on and not before.get(k)),
        "asked": list(turned_on),
        "pipeline": chosen.pipeline,
        "deal_term": chosen.deal_term,
        "items": [{"key": k, "enabled": v} for k, v in after.items()],
    }
