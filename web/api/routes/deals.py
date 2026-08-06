from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.services import (
    client_service,
    deal_service,
    modules_service,
    pipeline_service,
    settings_service,
)
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/deals", tags=["deals"])


def _lookup(db: Session, deals: list) -> tuple[dict, dict]:
    """Имена клиентов и ответственных — двумя запросами на всю выдачу.

    По одному запросу на карточку доска из полусотни сделок делала бы сотню
    обращений к базе на каждое открытие.
    """
    clients = {c.id: c for c in clients_repo.get_many(db, {d.client_id for d in deals})}
    managers = {u.id: u for u in users_repo.get_many(db, {d.manager_id for d in deals})}
    return clients, managers


def _card(db: Session, deal) -> dict:
    """Полный ответ по одной сделке — всегда одной формы.

    Историю кладём и в ответ на изменение тоже. Разная форма у GET и PATCH уже
    ломала карточку: фронт клал ответ PATCH в состояние, `stage_history`
    оказывалась undefined, и экран уходил в белое после смены этапа.
    """
    client = clients_repo.get(db, deal.client_id)
    manager = users_repo.get_by_id(db, deal.manager_id) if deal.manager_id else None
    history = deals_repo.stage_history(db, deal.id)
    authors = {
        u.id: u.name
        for u in users_repo.get_many(db, {c.changed_by for c in history if c.changed_by})
    }
    stages = {s.key: s for s in pipeline_service.list_stages(db, include_archived=True)}

    data = schemas.deal_out(deal, client.name if client else None, manager)
    # Карточку открывают и менеджеры, а настройки читает только root — валюту
    # кладём в ответ, иначе сумма показывалась бы голым числом.
    data["currency"] = settings_service.get_all(db).get("currency", "USD")
    # Телефон клиента — чтобы позвонить прямо отсюда, не открывая карточку
    # клиента ради одного номера. В списках и на канбане он не нужен, поэтому
    # лежит здесь, а не в deal_out.
    data["client_phone"] = client.phone if client else ""
    # Доски этой заявки. Модуль досок может быть выключен — тогда и списка нет:
    # спрашивать за него незачем, а показывать пустой раздел тем более.
    if modules_service.is_enabled(db, "boards"):
        data["boards"] = [
            {"id": b.id, "title": b.title, "is_published": b.is_published}
            for b in boards_repo.for_deal(db, deal.id)
        ]
    else:
        data["boards"] = []
    # Названия этапов в истории берём из воронки: голые ключи вроде
    # `in_progress` человеку ничего не говорят, а у каждого бизнеса они свои.
    data["stage_history"] = [
        {
            **schemas.stage_change_out(c, authors.get(c.changed_by)),
            "from_name": stages[c.from_stage].name if c.from_stage in stages else "",
            "to_name": stages[c.to_stage].name if c.to_stage in stages else c.to_stage,
        }
        for c in history
    ]
    return data


@router.get("/board")
def kanban(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    columns = deal_service.board(db)
    everything = [deal for column in columns for deal in column["deals"]]
    clients, managers = _lookup(db, everything)
    # Суммы — отдельным запросом по всем сделкам этапа: колонка отдаётся с
    # пределом, и сложение загруженных карточек занижало бы итог там, где
    # сделок много. Тихо и правдоподобно, то есть хуже всего.
    totals = deals_repo.amount_by_stage(db)
    # Валюта одна на систему, но лежит в настройках, а их читает только root.
    # Отдаём её вместе с доской: иначе менеджер видел бы суммы без обозначения,
    # а заводить ради одной строки отдельный запрос — хуже.
    currency = settings_service.get_all(db).get("currency", "USD")
    return {
        "currency": currency,
        "columns": [
            {
                # Отдаём этап целиком, а не один ключ: фронт рисует названия
                # воронки этого бизнеса и не может знать их заранее.
                **schemas.stage_out(column["stage"]),
                "amount_total": totals.get(column["stage"].key, 0),
                "deals": [
                    schemas.deal_out(
                        d,
                        clients[d.client_id].name if d.client_id in clients else None,
                        managers.get(d.manager_id),
                    )
                    for d in column["deals"]
                ],
            }
            for column in columns
        ]
    }


@router.get("")
def list_deals(
    search: str | None = None,
    stage: str | None = None,
    client_id: int | None = None,
    manager_id: int | None = None,
    include_closed: bool = True,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    items, total = deals_repo.search(
        db,
        q=search,
        stage=stage,
        client_id=client_id,
        manager_id=manager_id,
        include_closed=include_closed,
        page=page,
        per_page=per_page,
    )
    clients, managers = _lookup(db, items)
    return schemas.paginated(
        [
            schemas.deal_out(
                d,
                clients[d.client_id].name if d.client_id in clients else None,
                managers.get(d.manager_id),
            )
            for d in items
        ],
        total,
        page,
        per_page,
    )


@router.post("", status_code=201)
def create_deal(
    payload: schemas.DealIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    # exclude_unset, чтобы отличить «поле не прислали» от «прислали пустым»:
    # первое означает «поставь по умолчанию», второе — «оставь пустым».
    deal = deal_service.create_deal(db, payload.model_dump(exclude_unset=True), user)
    return _card(db, deal)


@router.get("/{deal_id}")
def get_deal(
    deal_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    return _card(db, deal_service.get_deal(db, deal_id))


@router.patch("/{deal_id}")
def update_deal(
    deal_id: int,
    payload: schemas.DealPatchIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    deal = deal_service.update_deal(
        db, deal_id, payload.model_dump(exclude_unset=True), user
    )
    return _card(db, deal)


@router.post("/{deal_id}/move")
def move_deal(
    deal_id: int,
    payload: schemas.DealMoveIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Перетаскивание карточки в канбане: смена этапа и/или позиции."""
    deal = deal_service.move_stage(
        db, deal_id, payload.stage, user, payload.sort_order, payload.lost_reason
    )
    return _card(db, deal)


@router.delete("/{deal_id}")
def delete_deal(
    deal_id: int,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    deal_service.delete_deal(db, deal_id, user)
    return {"message": "Deal deleted"}


@router.get("/{deal_id}/feed")
def deal_feed(
    deal_id: int,
    kind: str | None = None,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Лента заявки: звонки, письма, встречи и заметки одним потоком.

    Отдельная точка, а не фильтр в ленте клиента: заявка — стержень системы, и
    спрашивать её события через клиента значит каждый раз помнить, чей это
    клиент. К тому же у клиента событий больше, чем у одной заявки.
    """
    deal = deal_service.get_deal(db, deal_id)
    notes, _total = clients_repo.list_notes(
        db, client_id=deal.client_id, deal_id=deal_id, kind=kind, per_page=200
    )
    return {"items": [schemas.note_out(n) for n in notes]}


@router.post("/{deal_id}/feed", status_code=201)
def add_to_deal_feed(
    deal_id: int,
    payload: schemas.NoteIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    deal = deal_service.get_deal(db, deal_id)
    note = client_service.add_note(
        db,
        deal.client_id,
        user,
        payload.kind,
        payload.body,
        payload.happened_at,
        direction=payload.direction,
        deal_id=deal_id,
    )
    return schemas.note_out(note)
