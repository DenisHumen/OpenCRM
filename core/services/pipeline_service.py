"""Воронка: этапы и готовые наборы под отрасль.

Пресеты, а не пустой редактор. Малый бизнес не станет настраивать систему
вместо того, чтобы работать: пустая воронка на первом экране — это причина
закрыть вкладку. Поэтому при установке кладём универсальный набор, а сменить
его на отраслевой можно одним нажатием.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import exceptions as errors
from database.models import PipelineStage, User
from database.repositories import deals as deals_repo
from database.repositories import pipeline as pipeline_repo
from database.models.pipeline import (
    CLOSED_KINDS,
    KIND_LOST,
    KIND_OPEN,
    KIND_WON,
    STAGE_KINDS,
)

MAX_NAME = 60
MAX_STAGES = 12

# Наборы этапов под отрасль. Ключи внутри набора уникальны и стабильны —
# по ним ссылаются сделки; названия человек правит как хочет.
#
# У каждого набора ровно один `won` и один `lost` в конце: без них не считаются
# ни конверсия, ни потери, а «закрыть сделку» становится некуда.
PRESETS: dict[str, dict] = {
    "universal": {
        "name": "Универсальный",
        "hint": "Подойдёт почти любому делу — начните с него и переименуйте под себя",
        "stages": [
            ("new", "Новая заявка", KIND_OPEN),
            ("in_progress", "В работе", KIND_OPEN),
            ("ready", "Готово", KIND_OPEN),
            ("done", "Завершена", KIND_WON),
            ("cancelled", "Отказ", KIND_LOST),
        ],
    },
    "services": {
        "name": "Услуги и ремонт",
        "hint": "Ремонт техники, мастерские, выездные работы",
        "stages": [
            ("request", "Заявка", KIND_OPEN),
            ("diagnostics", "Диагностика", KIND_OPEN),
            ("approval", "Согласование цены", KIND_OPEN),
            ("repair", "В работе", KIND_OPEN),
            ("ready", "Готово к выдаче", KIND_OPEN),
            ("issued", "Выдано", KIND_WON),
            ("refused", "Отказ", KIND_LOST),
        ],
    },
    "beauty": {
        "name": "Бьюти и услуги по записи",
        "hint": "Салоны, студии, мастера — работа по записи на время",
        "stages": [
            ("request", "Обращение", KIND_OPEN),
            ("booked", "Записан", KIND_OPEN),
            ("confirmed", "Подтвердил", KIND_OPEN),
            ("came", "Пришёл", KIND_OPEN),
            ("served", "Услуга оказана", KIND_WON),
            ("no_show", "Не пришёл", KIND_LOST),
        ],
    },
    "shop": {
        "name": "Магазин",
        "hint": "Розница и интернет-магазин: от заказа до получения",
        "stages": [
            ("new", "Новый заказ", KIND_OPEN),
            ("paid", "Оплачен", KIND_OPEN),
            ("packed", "Собран", KIND_OPEN),
            ("shipped", "Отправлен", KIND_OPEN),
            ("received", "Получен", KIND_WON),
            ("cancelled", "Отменён", KIND_LOST),
        ],
    },
    "agency": {
        "name": "Студия и агентство",
        "hint": "Проектная работа со сметой и сдачей",
        "stages": [
            ("lead", "Заявка", KIND_OPEN),
            ("quote", "Смета", KIND_OPEN),
            ("in_progress", "В работе", KIND_OPEN),
            ("review", "Сдача", KIND_OPEN),
            ("won", "Закрыта", KIND_WON),
            ("lost", "Отказ", KIND_LOST),
        ],
    },
}

DEFAULT_PRESET = "universal"


def list_stages(db: Session, include_archived: bool = False) -> list[PipelineStage]:
    return pipeline_repo.list_stages(db, include_archived=include_archived)


def get_stage(db: Session, key: str) -> PipelineStage:
    stage = pipeline_repo.get_by_key(db, key)
    if stage is None:
        raise errors.ValidationError(f"Unknown stage: {key}", code="unknown_stage")
    return stage


def is_closed(db: Session, key: str) -> bool:
    return get_stage(db, key).kind in CLOSED_KINDS


def first_open_key(db: Session) -> str:
    """Куда попадает новая сделка, если этап не указан."""
    for stage in list_stages(db):
        if stage.kind == KIND_OPEN:
            return stage.key
    stages = list_stages(db)
    if not stages:
        raise errors.ValidationError("Pipeline is empty", code="pipeline_empty")
    return stages[0].key


def seed_defaults(db: Session) -> None:
    """Кладёт набор по умолчанию, если воронка пуста. Зовётся при старте.

    Терпит соседа: при `uvicorn --workers 2` оба процесса поднимаются разом и
    оба видят пустую воронку. Итог нужен один и тот же, и если его достиг
    другой — падать в момент старта незачем.
    """
    if pipeline_repo.any_exists(db):
        return
    try:
        apply_preset(db, DEFAULT_PRESET)
    except (errors.ConflictError, IntegrityError):
        db.rollback()


def apply_preset(
    db: Session, preset: str, actor: User | None = None
) -> list[PipelineStage]:
    """Заменить воронку готовым набором.

    `actor` необязателен только ради посева при первом запуске: там заявок нет
    и переселять некого. Из запроса он приходит всегда — иначе журнал этапов
    получит переходы без исполнителя.

    Старые этапы не удаляем, а прячем: на них ссылаются закрытые сделки и
    журнал перемещений. Этап с тем же ключом переиспользуем — тогда смена
    пресета не осиротит карточки, которые в нём стоят.
    """
    if preset not in PRESETS:
        raise errors.ValidationError(f"Unknown preset: {preset}", code="unknown_preset")

    existing = {s.key: s for s in list_stages(db, include_archived=True)}
    wanted = PRESETS[preset]["stages"]
    keys = {key for key, _, _ in wanted}

    for order, (key, name, kind) in enumerate(wanted, start=1):
        stage = existing.get(key)
        if stage is None:
            db.add(PipelineStage(key=key, name=name, kind=kind, sort_order=order * 10))
        else:
            stage.name = name
            stage.kind = kind
            stage.sort_order = order * 10
            stage.is_archived = False

    # Сначала прячем лишние этапы и только потом ищем, куда переселять сделки.
    # В обратном порядке целью оказывался этап, который сам сейчас спрячут, и
    # переезд получался сам в себя — карточки исчезали с доски.
    dropped = [key for key in existing if key not in keys]
    for key in dropped:
        existing[key].is_archived = True
    db.flush()

    if dropped:
        # Без переезда сделки остались бы ссылаться на спрятанный этап:
        # формально целы, а на доске их нет и найти нельзя.
        target = first_open_key(db)
        for deal in deals_repo.in_stages(db, dropped):
            was = deal.stage
            deal.stage = target
            # Переезд идёт в журнал этапов наравне с обычной сменой.
            #
            # Правило проекта — «журнал заполняется всегда, иначе отчёт „сколько
            # заявка простояла в этапе“ дырявый ровно там, где этап поменяли
            # другим путём» (см. `deal_service.move_stage`). Смена пресета и была
            # таким путём: десятки заявок меняли этап молча, и в истории каждой
            # оставался разрыв — последний записанный переход вёл в этап,
            # которого у заявки уже нет.
            #
            # Событие при этом НЕ поднимаем, и это осознанно: в ленте каждой
            # заявки появилась бы строка «этап сменился», которой никто не
            # делал. Перестройка воронки — не работа по заявке, а настройка
            # системы; её место в журнале действий, а не в переписке с клиентом.
            deals_repo.add_stage_change(db, deal.id, was, target, actor.id if actor else None)
        db.flush()

    return list_stages(db)


def create_stage(db: Session, name: str, kind: str, after: str | None = None) -> PipelineStage:
    name = (name or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    if kind not in STAGE_KINDS:
        raise errors.ValidationError(f"Unknown kind: {kind}", code="unknown_kind")
    if len(list_stages(db)) >= MAX_STAGES:
        # Не придирка: доску с двумя десятками колонок невозможно просматривать,
        # а воронка из них ничего не показывает.
        raise errors.ValidationError(
            f"Too many stages (max {MAX_STAGES})", code="too_many_stages"
        )

    key = _free_key(db, name)
    order = 10
    if after:
        order = get_stage(db, after).sort_order + 5
    else:
        stages = list_stages(db)
        order = (stages[-1].sort_order + 10) if stages else 10

    stage = PipelineStage(key=key, name=name[:MAX_NAME], kind=kind, sort_order=order)
    db.add(stage)
    db.flush()
    return stage


def update_stage(db: Session, key: str, data: dict) -> PipelineStage:
    stage = get_stage(db, key)
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name:
            raise errors.ValidationError("Name is required", code="name_required")
        stage.name = name[:MAX_NAME]
    if "color" in data and data["color"] is not None:
        stage.color = data["color"].strip()[:7]
    if "sort_order" in data and data["sort_order"] is not None:
        stage.sort_order = int(data["sort_order"])
    if "kind" in data and data["kind"]:
        if data["kind"] not in STAGE_KINDS:
            raise errors.ValidationError(f"Unknown kind: {data['kind']}", code="unknown_kind")
        _check_last_of_kind(db, stage, data["kind"])
        stage.kind = data["kind"]
    db.flush()
    return stage


def archive_stage(db: Session, key: str) -> None:
    """Убрать этап с доски. Сделки в нём переезжают на первый открытый."""
    stage = get_stage(db, key)
    _check_last_of_kind(db, stage, None)

    target = None
    for candidate in list_stages(db):
        if candidate.key != key and candidate.kind == KIND_OPEN:
            target = candidate.key
            break
    if target is None:
        raise errors.ValidationError(
            "Cannot archive the last open stage", code="last_open_stage"
        )

    for deal in deals_repo.in_stages(db, [key]):
        deal.stage = target
    stage.is_archived = True
    db.flush()


def _check_last_of_kind(db: Session, stage: PipelineStage, new_kind: str | None) -> None:
    """Нельзя остаться без «выиграна» или «провалена».

    Без них некуда закрыть сделку, а конверсия и потери перестают считаться —
    воронка превращается в список без итога.
    """
    if stage.kind not in CLOSED_KINDS or new_kind == stage.kind:
        return
    same = [s for s in list_stages(db) if s.kind == stage.kind and s.key != stage.key]
    if not same:
        raise errors.ValidationError(
            f"The pipeline needs at least one '{stage.kind}' stage",
            code="last_stage_of_kind",
        )


def _free_key(db: Session, name: str) -> str:
    """Ключ из названия: латиница, цифры и подчёркивания.

    Кириллица в ключ не идёт — он попадает в адреса и в выгрузки; если из
    названия ничего не осталось, берём порядковый.
    """
    base = "".join(ch if ch.isalnum() and ch.isascii() else "_" for ch in name.lower())
    base = "_".join(part for part in base.split("_") if part)[:24] or "stage"
    taken = {s.key for s in list_stages(db, include_archived=True)}
    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}_{suffix}"
        if candidate not in taken:
            return candidate
    raise errors.ValidationError("Cannot allocate a stage key", code="stage_key_taken")
