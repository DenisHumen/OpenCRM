from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Client, Deal, DealStageChange, PipelineStage
from database.models.pipeline import CLOSED_KINDS, KIND_OPEN, KIND_WON


def get(db: Session, deal_id: int, include_deleted: bool = False) -> Deal | None:
    deal = db.get(Deal, deal_id)
    if deal is None:
        return None
    if deal.deleted_at is not None and not include_deleted:
        return None
    return deal


def search(
    db: Session,
    q: str | None = None,
    stage: str | None = None,
    client_id: int | None = None,
    manager_id: int | None = None,
    include_closed: bool = True,
    page: int = 1,
    per_page: int = 50,
    only_manager_id: int | None = None,
) -> tuple[list[Deal], int]:
    """`only_manager_id` — ограничение доступа, а не фильтр из интерфейса.

    Отдельно от `manager_id` намеренно: тот приходит из запроса и его можно
    снять, этот приходит из прав и снять его нельзя. Слитые в один параметр они
    означали бы, что достаточно прислать чужой `manager_id`, чтобы обойти
    ограничение, — а выглядело бы это как работающая проверка.
    """
    stmt = select(Deal).where(Deal.deleted_at.is_(None))
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    if q:
        like = f"%{q.strip()}%"
        # Ищем и по названию клиента: в жизни спрашивают «что там по Ромашке»,
        # а не «как называлась та сделка».
        stmt = stmt.join(Client, Client.id == Deal.client_id).where(
            or_(Deal.title.ilike(like), Deal.description.ilike(like), Client.name.ilike(like))
        )
    if stage:
        stmt = stmt.where(Deal.stage == stage)
    if client_id:
        stmt = stmt.where(Deal.client_id == client_id)
    if manager_id:
        stmt = stmt.where(Deal.manager_id == manager_id)
    if not include_closed:
        # «Закрытые» определяются типом этапа, а не списком имён: названия у
        # каждого бизнеса свои, тип — общий.
        closed = select(PipelineStage.key).where(PipelineStage.kind.in_(CLOSED_KINDS))
        stmt = stmt.where(Deal.stage.notin_(closed))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(Deal.updated_at.desc()).offset((page - 1) * per_page).limit(per_page)
    return list(db.scalars(stmt)), total


def by_stage(
    db: Session, stage: str, limit: int = 200, only_manager_id: int | None = None
) -> list[Deal]:
    """Колонка канбана. Порядок — заданный руками, при равенстве свежие выше."""
    stmt = select(Deal).where(Deal.deleted_at.is_(None), Deal.stage == stage)
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    return list(
        db.scalars(stmt.order_by(Deal.sort_order.asc(), Deal.id.desc()).limit(limit))
    )


def amount_by_stage(db: Session, only_manager_id: int | None = None) -> dict[str, int]:
    """Сумма сделок в каждом этапе — запросом, а не сложением карточек.

    `by_stage` отдаёт колонку с пределом, и сумма по загруженным карточкам
    занижала бы итог ровно там, где сделок много, — то есть там, где на него и
    смотрят. Ошибка при этом тихая: число есть, оно правдоподобное, и заметить
    его можно только сверив вручную.

    Итог считается по тем же заявкам, которые человек видит: иначе сумма над
    колонкой из трёх карточек оказалась бы взята из тридцати, и разошлась бы с
    тем, что под ней, — а разошедшийся итог читается как ошибка в расчётах.
    """
    stmt = select(Deal.stage, func.coalesce(func.sum(Deal.amount), 0)).where(
        Deal.deleted_at.is_(None)
    )
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    rows = db.execute(stmt.group_by(Deal.stage)).all()
    return {stage: int(total or 0) for stage, total in rows}


def money_summary(db: Session, since, only_manager_id: int | None = None) -> dict[str, int | None]:
    """Деньги для сводки: сколько в работе и сколько выиграно с даты.

    Считаем по ВИДУ этапа, а не по названию: у каждого бизнеса воронка своя, и
    «Выдано», «Оплачено», «Договор подписан» — это всё один и тот же `won`.

    `only_manager_id` — то же ограничение доступа, что у списка и канбана
    (`deals.view_others`). Без него сводка отвечала бы на вопрос, которого
    сотруднику не задавали: список показывает три его заявки, а плитка сверху —
    выручку всей фирмы. Спрятать чужие карточки и оставить их сумму — это не
    половина запрета, а его отсутствие: узнать оборот фирмы и было целью.
    """
    def total(kind: str, closed_since=None) -> int:
        query = (
            select(func.coalesce(func.sum(Deal.amount), 0))
            .join(PipelineStage, PipelineStage.key == Deal.stage)
            .where(Deal.deleted_at.is_(None), PipelineStage.kind == kind)
        )
        if only_manager_id is not None:
            query = query.where(Deal.manager_id == only_manager_id)
        if closed_since is not None:
            query = query.where(Deal.closed_at >= closed_since)
        return int(db.scalar(query) or 0)

    # Знаменатель среднего чека — сделки с НАЗВАННОЙ суммой. `count(amount)`
    # не считает NULL, и это здесь главное: «сумму ещё не назвали» — не то же
    # самое, что «работа бесплатная». Возьми в знаменатель все выигранные — и
    # каждая сделка без цены будет тихо занижать средний чек.
    priced = (
        select(func.count(Deal.amount))
        .join(PipelineStage, PipelineStage.key == Deal.stage)
        .where(
            Deal.deleted_at.is_(None),
            PipelineStage.kind == KIND_WON,
            Deal.closed_at >= since,
        )
    )
    if only_manager_id is not None:
        priced = priced.where(Deal.manager_id == only_manager_id)
    priced_won = int(db.scalar(priced) or 0)
    won_since = total(KIND_WON, since)

    # Сколько сделок выиграно за период — ВСЕХ, а не только с названной ценой.
    # Плитка сводки подписана «выиграно за месяц», и показывать там число
    # оценённых значило отвечать не на тот вопрос: менеджер выиграл сделку и не
    # заполнил сумму — на сводке месяц пустой, а в отчёте за тот же месяц
    # «выиграно 1». Два разных ответа на один вопрос.
    counted = (
        select(func.count())
        .select_from(Deal)
        .join(PipelineStage, PipelineStage.key == Deal.stage)
        .where(
            Deal.deleted_at.is_(None),
            PipelineStage.kind == KIND_WON,
            Deal.closed_at >= since,
        )
    )
    if only_manager_id is not None:
        counted = counted.where(Deal.manager_id == only_manager_id)
    won_count = int(db.scalar(counted) or 0)

    return {
        "in_work": total(KIND_OPEN),
        # «Цену не назвали» и «работа бесплатная» — разные вещи, и на сводке
        # тоже: без единой оценённой сделки здесь прочерк, а не ноль. Иначе
        # плитка пишет «0 ₽» там, где верный ответ — «пока не о чем говорить»,
        # и расходится с отчётом за тот же месяц.
        "won_since": won_since if priced_won else None,
        "won_count": won_count,
        "won_count_priced": priced_won,
        # Без единой сделки с ценой среднего чека нет — и это НЕ ноль. Ноль
        # прочитают как «работаем даром», а верный ответ — «пока не о чем
        # говорить».
        "avg_check": round(won_since / priced_won) if priced_won else None,
    }


def stage_counts(db: Session, only_manager_id: int | None = None) -> dict[str, int]:
    """Сколько заявок в каждом этапе. Сужается тем же правом, что и суммы:
    воронка из чужих карточек — это тоже сведения о чужой работе."""
    stmt = select(Deal.stage, func.count()).where(Deal.deleted_at.is_(None))
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    rows = db.execute(stmt.group_by(Deal.stage)).all()
    return {stage: count for stage, count in rows}


def next_sort_order(db: Session, stage: str) -> int:
    current = db.scalar(
        select(func.max(Deal.sort_order)).where(
            Deal.stage == stage, Deal.deleted_at.is_(None)
        )
    )
    return (current or 0) + 10


def for_client(
    db: Session, client_id: int, only_manager_id: int | None = None
) -> list[Deal]:
    stmt = select(Deal).where(Deal.client_id == client_id, Deal.deleted_at.is_(None))
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    return list(db.scalars(stmt.order_by(Deal.created_at.desc())))


# --- журнал этапов ---


def add_stage_change(
    db: Session, deal_id: int, from_stage: str, to_stage: str, user_id: int | None
) -> DealStageChange:
    row = DealStageChange(
        deal_id=deal_id, from_stage=from_stage, to_stage=to_stage, changed_by=user_id
    )
    db.add(row)
    db.flush()
    return row


def stage_history(db: Session, deal_id: int) -> list[DealStageChange]:
    return list(
        db.scalars(
            select(DealStageChange)
            .where(DealStageChange.deal_id == deal_id)
            .order_by(DealStageChange.changed_at.asc(), DealStageChange.id.asc())
        )
    )
