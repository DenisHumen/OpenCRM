from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    client_service,
    deal_lines_service,
    deal_service,
    modules_service,
    order_service,
    permissions_service,
    pipeline_service,
    settings_service,
)
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm


def _note_authors(db, notes) -> dict[int, str]:
    """Имена авторов записей ленты — одним запросом на список.

    Пачкой, а не по строке: лента открывается на каждой карточке, и запрос на
    запись превратил бы её в самое дорогое место экрана.
    """
    people = users_repo.get_many(db, [n.author_id for n in notes])
    return {person.id: person.name for person in people}


router = APIRouter(prefix="/deals", tags=["deals"])

#: Поля запроса, в которых приезжают деньги. Перечислены здесь, а не выводятся
#: из `schemas.DEAL_MONEY_KEYS`: там ключи ОТВЕТА, и в них есть считаемые
#: (`remainder`, `is_paid`), которых во входящем теле не бывает. Списки разной
#: длины и по разному поводу — сведённые в один, они разъедутся молча.
DEAL_MONEY_FIELDS = ("amount", "prepaid")


def _lookup(db: Session, deals: list) -> tuple[dict, dict]:
    """Имена клиентов и ответственных — двумя запросами на всю выдачу.

    По одному запросу на карточку доска из полусотни сделок делала бы сотню
    обращений к базе на каждое открытие.
    """
    clients = {c.id: c for c in clients_repo.get_many(db, {d.client_id for d in deals})}
    managers = {u.id: u for u in users_repo.get_many(db, {d.manager_id for d in deals})}
    return clients, managers


def _card(db: Session, deal, user: User) -> dict:
    """Полный ответ по одной сделке — всегда одной формы.

    Историю кладём и в ответ на изменение тоже. Разная форма у GET и PATCH уже
    ломала карточку: фронт клал ответ PATCH в состояние, `stage_history`
    оказывалась undefined, и экран уходил в белое после смены этапа.
    """
    amounts = permissions_service.sees_amounts(db, user)
    client = clients_repo.get(db, deal.client_id)
    manager = users_repo.get_by_id(db, deal.manager_id) if deal.manager_id else None
    history = deals_repo.stage_history(db, deal.id)
    authors = {
        u.id: u.name
        for u in users_repo.get_many(db, {c.changed_by for c in history if c.changed_by})
    }
    stages = {s.key: s for s in pipeline_service.list_stages(db, include_archived=True)}

    data = schemas.deal_out(deal, client.name if client else None, manager, amounts=amounts)
    # Карточку открывают и менеджеры, а настройки читает только root — валюту
    # кладём в ответ, иначе сумма показывалась бы голым числом.
    data["currency"] = settings_service.get_all(db).get("currency", "USD")
    # Телефон клиента — чтобы позвонить прямо отсюда, не открывая карточку
    # клиента ради одного номера. В списках и на канбане он не нужен, поэтому
    # лежит здесь, а не в deal_out.
    data["client_phone"] = client.phone if client else ""
    # Адрес — по той же причине и рядом с телефоном. Карточка брала его из
    # справочника клиентов, загруженного в поле выбора первыми двумя сотнями:
    # у двести первого клиента форма отправки открывалась с пустым «кому», и
    # это было видно только в тот момент, когда письмо уже пишут.
    data["client_email"] = client.email if client else ""
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
def kanban(
    stage: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=deal_service.KOLONKA, ge=1, le=200),
    user: User = Depends(require_perm("deals", "view")),
    db: Session = Depends(get_db),
):
    """Канбан целиком или следующая страница ОДНОЙ колонки.

    `stage` вместе с `page` — дочитывание: человек развернул один этап, и
    перезапрашивать ради него ещё шесть колонок, которых он не трогал, значило
    бы платить семью запросами за одну кнопку.

    Прежде колонка отдавалась с пределом в две сотни и без счётчика: заявка
    номер двести один на доску не попадала вовсе. Заметить это было нечем —
    итог над колонкой считается отдельным запросом по ВСЕМ заявкам этапа и
    потому оставался верным.
    """
    mine_only = permissions_service.deals_scope(db, user)
    amounts = permissions_service.sees_amounts(db, user)
    columns = deal_service.board(
        db, only_manager_id=mine_only, stage_key=stage, page=page, per_page=per_page
    )
    everything = [deal for column in columns for deal in column["deals"]]
    clients, managers = _lookup(db, everything)
    # Суммы — отдельным запросом по всем сделкам этапа: колонка отдаётся с
    # пределом, и сложение загруженных карточек занижало бы итог там, где
    # сделок много. Тихо и правдоподобно, то есть хуже всего.
    totals = (
        deals_repo.amount_by_stage(db, only_manager_id=mine_only) if amounts else {}
    )
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
                # Итог над колонкой прячется вместе с суммами карточек: одно
                # без другого — это скрытая сумма, которую видно в шапке.
                "amount_total": totals.get(column["stage"].key, 0) if amounts else None,
                # Сколько в этапе всего. Без этого числа кнопка «показать ещё»
                # не знает, показывать ли ей себя, — а колонка выглядит полной
                # ровно тогда, когда она обрезана.
                "count": column["count"],
                "deals": [
                    schemas.deal_out(
                        d,
                        clients[d.client_id].name if d.client_id in clients else None,
                        managers.get(d.manager_id),
                        amounts=amounts,
                    )
                    for d in column["deals"]
                ],
            }
            for column in columns
        ]
    }


@router.get("")
def list_deals(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    stage: str | None = None,
    client_id: int | None = None,
    manager_id: int | None = None,
    include_closed: bool = True,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("deals", "view")),
    db: Session = Depends(get_db),
):
    amounts = permissions_service.sees_amounts(db, user)
    items, total = deals_repo.search(
        db,
        q=search,
        stage=stage,
        client_id=client_id,
        manager_id=manager_id,
        include_closed=include_closed,
        page=page,
        per_page=per_page,
        only_manager_id=permissions_service.deals_scope(db, user),
    )
    clients, managers = _lookup(db, items)
    return schemas.paginated(
        [
            schemas.deal_out(
                d,
                clients[d.client_id].name if d.client_id in clients else None,
                managers.get(d.manager_id),
                amounts=amounts,
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
    user: User = Depends(require_perm("deals", "create")),
    db: Session = Depends(get_db),
):
    # exclude_unset, чтобы отличить «поле не прислали» от «прислали пустым»:
    # первое означает «поставь по умолчанию», второе — «оставь пустым».
    data = payload.model_dump(exclude_unset=True)
    _refuse_money_without_the_right(db, data, user)
    deal = deal_service.create_deal(db, data, user)
    return _card(db, deal, user)


def _refuse_money_without_the_right(db: Session, data: dict, user: User) -> None:
    """Не видит сумм — не называет их. И при заведении заявки тоже.

    Проверка стояла только на правке, и запрет получался половинчатым: сумму
    нельзя было переписать, но можно было завести заявку сразу с ней. Число при
    этом уходило в выручку и в отчёты, а тот, кто его вписал, не видел его
    больше никогда — даже чтобы исправить опечатку. Право на суммы означает и
    «называть их», иначе оно закрывает экран, а не деньги.
    """
    touched = [key for key in DEAL_MONEY_FIELDS if key in data]
    if touched and not permissions_service.sees_amounts(db, user):
        raise errors.ForbiddenError(
            "Permission required: deals.view_amounts", code="permission_denied"
        )


def _visible(db: Session, deal_id: int, user: User):
    """Заявка, которую этому сотруднику вообще положено видеть.

    Одной строкой на каждом маршруте с `{deal_id}`, а не один раз в сервисе:
    `get_deal` зовут ещё бланки, склад и телефония, и подмешивать туда права
    заявок значило бы решать за них — а у них своя область и свои права.
    """
    deal = deal_service.get_deal(db, deal_id)
    return deal_service.ensure_visible(db, deal, permissions_service.deals_scope(db, user))


@router.get("/{deal_id}")
def get_deal(
    deal_id: int,
    user: User = Depends(require_perm("deals", "view")),
    db: Session = Depends(get_db),
):
    return _card(db, _visible(db, deal_id, user), user)


@router.patch("/{deal_id}")
def update_deal(
    deal_id: int,
    payload: schemas.DealPatchIn,
    user: User = Depends(require_perm("deals", "edit")),
    db: Session = Depends(get_db),
):
    _visible(db, deal_id, user)
    data = payload.model_dump(exclude_unset=True)
    # Не видит сумм — не правит их. Иначе право оказывалось бы наполовину
    # декоративным: сумму не показали, но её можно перезаписать вслепую, а
    # прежнее значение при этом теряется без следа.
    _refuse_money_without_the_right(db, data, user)
    deal = deal_service.update_deal(db, deal_id, data, user)
    return _card(db, deal, user)


@router.post("/{deal_id}/move")
def move_deal(
    deal_id: int,
    payload: schemas.DealMoveIn,
    user: User = Depends(require_perm("deals", "move_stage")),
    db: Session = Depends(get_db),
):
    """Перетаскивание карточки в канбане: смена этапа и/или позиции."""
    _visible(db, deal_id, user)
    deal = deal_service.move_stage(
        db, deal_id, payload.stage, user, payload.sort_order, payload.lost_reason
    )
    return _card(db, deal, user)


@router.delete("/{deal_id}")
def delete_deal(
    deal_id: int,
    user: User = Depends(require_perm("deals", "delete")),
    db: Session = Depends(get_db),
):
    _visible(db, deal_id, user)
    deal_service.delete_deal(db, deal_id, user)
    return {"message": "Deal deleted"}


@router.get("/{deal_id}/feed")
def deal_feed(
    deal_id: int,
    kind: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=200),
    user: User = Depends(require_perm("deals", "view")),
    db: Session = Depends(get_db),
):
    """Лента заявки: звонки, письма, встречи и заметки одним потоком.

    Отдельная точка, а не фильтр в ленте клиента: заявка — стержень системы, и
    спрашивать её события через клиента значит каждый раз помнить, чей это
    клиент. К тому же у клиента событий больше, чем у одной заявки.
    """
    deal = _visible(db, deal_id, user)
    notes, vsego = clients_repo.list_notes(
        db,
        client_id=deal.client_id,
        deal_id=deal_id,
        kind=kind,
        page=page,
        per_page=per_page,
    )
    authors = _note_authors(db, notes)
    # `total` в ответе, а не выброшенный в `_total`: прежде лента брала две
    # сотни событий и обрывалась молча. У заявки, которую ведут год, ранние
    # разговоры просто переставали существовать, и признака этому не было
    # никакого — ни счётчика, ни строки «показано столько-то».
    return schemas.paginated(
        [schemas.note_out(n, authors.get(n.author_id)) for n in notes],
        vsego,
        page,
        per_page,
    )


@router.post("/{deal_id}/feed", status_code=201)
def add_to_deal_feed(
    deal_id: int,
    payload: schemas.NoteIn,
    user: User = Depends(require_perm("deals", "edit")),
    db: Session = Depends(get_db),
):
    deal = _visible(db, deal_id, user)
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
    return schemas.note_out(note, note.author_id and _note_authors(db, [note]).get(note.author_id))


# --- строки заявки -----------------------------------------------------------
#
# Весь раздел закрыт блоком `warehouse`: без склада заявка снова описывается
# одной суммой, и «свои траты» без позиций смысла не имеют. Так же исчезает и
# раздел на экране — блок выключается целиком, а не наполовину
# (`docs/11-modules.md`).


@router.get("/{deal_id}/lines", dependencies=[Depends(require_module("warehouse"))])
def deal_lines(
    deal_id: int,
    user: User = Depends(require_perm("deals", "view")),
    db: Session = Depends(get_db),
):
    _visible(db, deal_id, user)
    amounts = permissions_service.sees_amounts(db, user)
    stroki = deal_lines_service.spisok(db, deal_id)
    return {
        "items": deal_lines_service.s_nehvatkoy(db, stroki, amounts),
        "total_minor": deal_lines_service.itog(db, deal_id) if amounts else None,
    }


@router.post(
    "/{deal_id}/lines", status_code=201, dependencies=[Depends(require_module("warehouse"))]
)
def add_deal_line(
    deal_id: int,
    payload: schemas.DealLineIn,
    user: User = Depends(require_perm("deals", "edit")),
    db: Session = Depends(get_db),
):
    _visible(db, deal_id, user)
    stroka = deal_lines_service.dobavit(db, deal_id, payload.model_dump(exclude_unset=True))
    # Нехватка отдаётся ответом на добавление: «добавили, но столько на складе
    # не лежит» — это предупреждение, и сказать его нужно сразу.
    #
    # Право на суммы спрашивается и у ПИШУЩЕЙ ручки: ответ на запись — такой же
    # обход, как чтение, и в проекте на этом уже спотыкались дважды.
    return deal_lines_service.s_nehvatkoy(
        db, [stroka], permissions_service.sees_amounts(db, user)
    )[0]


@router.patch(
    "/{deal_id}/lines/{line_id}", dependencies=[Depends(require_module("warehouse"))]
)
def edit_deal_line(
    deal_id: int,
    line_id: int,
    payload: schemas.DealLinePatchIn,
    user: User = Depends(require_perm("deals", "edit")),
    db: Session = Depends(get_db),
):
    _visible(db, deal_id, user)
    stroka = deal_lines_service.pravit(
        db, deal_id, line_id, payload.model_dump(exclude_unset=True)
    )
    return deal_lines_service.s_nehvatkoy(
        db, [stroka], permissions_service.sees_amounts(db, user)
    )[0]


@router.delete(
    "/{deal_id}/lines/{line_id}", dependencies=[Depends(require_module("warehouse"))]
)
def drop_deal_line(
    deal_id: int,
    line_id: int,
    user: User = Depends(require_perm("deals", "edit")),
    db: Session = Depends(get_db),
):
    _visible(db, deal_id, user)
    deal_lines_service.ubrat(db, deal_id, line_id)
    return {"message": "Line removed"}


@router.post(
    "/{deal_id}/order",
    status_code=201,
    dependencies=[Depends(require_module("orders"))],
)
def order_from_deal(
    deal_id: int,
    user: User = Depends(require_perm("orders", "create")),
    db: Session = Depends(get_db),
):
    """Завести заказ покупателя по заявке, перенеся в него её товары.

    Право спрашивается ЗАКАЗОВ, а не заявок: заводится заказ, и тот, кому
    заводить их не положено, не должен обходить это через карточку заявки.
    """
    deal = _visible(db, deal_id, user)
    zakaz = order_service.sozdat_iz_zayavki(db, deal, user)
    return schemas.order_out(
        zakaz,
        order_service.lines(db, zakaz.id),
        amounts=permissions_service.sees_amounts(db, user, "orders"),
    )
