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
) -> tuple[list[Deal], int]:
    stmt = select(Deal).where(Deal.deleted_at.is_(None))
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


def by_stage(db: Session, stage: str, limit: int = 200) -> list[Deal]:
    """Колонка канбана. Порядок — заданный руками, при равенстве свежие выше."""
    return list(
        db.scalars(
            select(Deal)
            .where(Deal.deleted_at.is_(None), Deal.stage == stage)
            .order_by(Deal.sort_order.asc(), Deal.id.desc())
            .limit(limit)
        )
    )


def amount_by_stage(db: Session) -> dict[str, int]:
    """Сумма сделок в каждом этапе — запросом, а не сложением карточек.

    `by_stage` отдаёт колонку с пределом, и сумма по загруженным карточкам
    занижала бы итог ровно там, где сделок много, — то есть там, где на него и
    смотрят. Ошибка при этом тихая: число есть, оно правдоподобное, и заметить
    его можно только сверив вручную.
    """
    rows = db.execute(
        select(Deal.stage, func.coalesce(func.sum(Deal.amount), 0))
        .where(Deal.deleted_at.is_(None))
        .group_by(Deal.stage)
    ).all()
    return {stage: int(total or 0) for stage, total in rows}


def money_summary(db: Session, since) -> dict[str, int]:
    """Деньги для сводки: сколько в работе и сколько выиграно с даты.

    Считаем по ВИДУ этапа, а не по названию: у каждого бизнеса воронка своя, и
    «Выдано», «Оплачено», «Договор подписан» — это всё один и тот же `won`.
    """
    def total(kind: str, closed_since=None) -> int:
        query = (
            select(func.coalesce(func.sum(Deal.amount), 0))
            .join(PipelineStage, PipelineStage.key == Deal.stage)
            .where(Deal.deleted_at.is_(None), PipelineStage.kind == kind)
        )
        if closed_since is not None:
            query = query.where(Deal.closed_at >= closed_since)
        return int(db.scalar(query) or 0)

    return {
        "in_work": total(KIND_OPEN),
        "won_since": total(KIND_WON, since),
    }


def stage_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Deal.stage, func.count())
        .where(Deal.deleted_at.is_(None))
        .group_by(Deal.stage)
    ).all()
    return {stage: count for stage, count in rows}


def next_sort_order(db: Session, stage: str) -> int:
    current = db.scalar(
        select(func.max(Deal.sort_order)).where(
            Deal.stage == stage, Deal.deleted_at.is_(None)
        )
    )
    return (current or 0) + 10


def for_client(db: Session, client_id: int) -> list[Deal]:
    return list(
        db.scalars(
            select(Deal)
            .where(Deal.client_id == client_id, Deal.deleted_at.is_(None))
            .order_by(Deal.created_at.desc())
        )
    )


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
