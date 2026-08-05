"""Сделки: работа для клиента от заявки до закрытия."""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import pipeline_service
from core.utils import now_utc
from database.models import Deal, User
from database.models.pipeline import CLOSED_KINDS, KIND_LOST
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo

MAX_TITLE = 200
MAX_LOST_REASON = 200


def get_deal(db: Session, deal_id: int, include_deleted: bool = False) -> Deal:
    deal = deals_repo.get(db, deal_id, include_deleted=include_deleted)
    if deal is None:
        raise errors.NotFoundError("Deal not found", code="deal_not_found")
    return deal


def create_deal(db: Session, data: dict, author: User) -> Deal:
    title = (data.get("title") or "").strip()
    if not title:
        raise errors.ValidationError("Title is required", code="title_required")

    client_id = data.get("client_id")
    if not client_id:
        raise errors.ValidationError("Client is required", code="client_required")
    # Сделка без клиента бессмысленна: некому выставлять счёт и не с кем
    # переписываться. Проверяем существование, а не только наличие числа.
    if clients_repo.get(db, int(client_id)) is None:
        raise errors.NotFoundError("Client not found", code="client_not_found")

    stage = data.get("stage") or pipeline_service.first_open_key(db)
    pipeline_service.get_stage(db, stage)   # бросит unknown_stage, если этапа нет

    # «Поле не прислали» и «прислали пустым» — разные вещи. Не указали
    # ответственного — ставим автора, чтобы сделка не осталась ничьей случайно.
    # Указали пусто явно — значит так и хотели: общая очередь, разберут потом.
    manager_id = data["manager_id"] if "manager_id" in data else author.id

    deal = Deal(
        title=title[:MAX_TITLE],
        client_id=int(client_id),
        manager_id=manager_id,
        stage=stage,
        sort_order=deals_repo.next_sort_order(db, stage),
        description=(data.get("description") or "").strip(),
        due_at=data.get("due_at"),
        closed_at=now_utc() if pipeline_service.is_closed(db, stage) else None,
    )
    db.add(deal)
    db.flush()
    # Первая запись журнала — с пустым «откуда»: до создания этапа не было.
    deals_repo.add_stage_change(db, deal.id, "", stage, author.id)
    return deal


def update_deal(db: Session, deal_id: int, data: dict, author: User) -> Deal:
    deal = get_deal(db, deal_id)

    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise errors.ValidationError("Title is required", code="title_required")
        deal.title = title[:MAX_TITLE]
    if "description" in data and data["description"] is not None:
        deal.description = data["description"].strip()
    if "due_at" in data:
        deal.due_at = data["due_at"]
    if "manager_id" in data:
        deal.manager_id = data["manager_id"]
    if "client_id" in data and data["client_id"]:
        if clients_repo.get(db, int(data["client_id"])) is None:
            raise errors.NotFoundError("Client not found", code="client_not_found")
        deal.client_id = int(data["client_id"])
    if "lost_reason" in data and data["lost_reason"] is not None:
        deal.lost_reason = data["lost_reason"].strip()[:MAX_LOST_REASON]

    # Этап меняем через общий путь, чтобы журнал заполнялся и здесь тоже.
    if "stage" in data and data["stage"] and data["stage"] != deal.stage:
        move_stage(db, deal_id, data["stage"], author, lost_reason=data.get("lost_reason"))
        return get_deal(db, deal_id)

    db.flush()
    return deal


def move_stage(
    db: Session,
    deal_id: int,
    stage: str,
    author: User,
    sort_order: int | None = None,
    lost_reason: str | None = None,
) -> Deal:
    """Передвинуть сделку по воронке.

    Единственная точка смены этапа: журнал должен заполняться всегда, иначе
    отчёт «сколько сделка стоит в этапе» окажется дырявым ровно там, где кто-то
    поменял этап другим путём.
    """
    deal = get_deal(db, deal_id)
    target = pipeline_service.get_stage(db, stage)

    if stage == deal.stage:
        # Перетащили внутри той же колонки — это не смена этапа, в журнал не пишем.
        if sort_order is not None:
            deal.sort_order = sort_order
            db.flush()
        return deal

    previous = deal.stage
    deal.stage = stage
    deal.sort_order = (
        sort_order if sort_order is not None else deals_repo.next_sort_order(db, stage)
    )
    if target.kind in CLOSED_KINDS:
        deal.closed_at = now_utc()
        if target.kind == KIND_LOST and lost_reason is not None:
            deal.lost_reason = lost_reason.strip()[:MAX_LOST_REASON]
    else:
        # Вернули из закрытых в работу — дата закрытия и причина больше не верны.
        deal.closed_at = None
        deal.lost_reason = ""

    db.flush()
    deals_repo.add_stage_change(db, deal.id, previous, stage, author.id)
    return deal


def delete_deal(db: Session, deal_id: int) -> None:
    deal = get_deal(db, deal_id)
    deal.deleted_at = now_utc()
    db.flush()


def board(db: Session) -> list[dict]:
    """Канбан: колонки по этапам со сделками внутри.

    Состав и порядок колонок берём из воронки, а не из констант: у ремонта
    техники, салона и магазина этапы разные, и доска обязана показывать их.
    """
    return [
        {"stage": stage, "deals": deals_repo.by_stage(db, stage.key)}
        for stage in pipeline_service.list_stages(db)
    ]
