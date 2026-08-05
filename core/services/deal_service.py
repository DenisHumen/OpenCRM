"""Сделки: работа для клиента от заявки до закрытия."""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import company_service, pipeline_service
from core.utils import now_utc
from database.models import Deal, User
from database.models.pipeline import CLOSED_KINDS, KIND_LOST
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo

MAX_TITLE = 200
MAX_LOST_REASON = 200

# Потолок суммы в минимальных единицах: 10 миллиардов в валюте. Не про
# ограничение бизнеса, а про опечатку: лишний ноль в поле превращает отчёт за
# месяц в бессмыслицу, и заметить это тем труднее, чем позже смотришь.
MAX_MONEY = 10**12


def parse_money(value, field: str) -> int | None:
    """Сумма из запроса в минимальные единицы.

    None пропускаем как есть: «сумму ещё не назвали» — законное состояние, и
    подменять его нулём нельзя. Ноль означает другое — работа бесплатная.
    """
    if value is None or value == "":
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise errors.ValidationError(
            f"{field} must be a whole number of minor units", code="bad_money"
        ) from None
    if amount < 0:
        raise errors.ValidationError(f"{field} cannot be negative", code="negative_money")
    if amount > MAX_MONEY:
        raise errors.ValidationError(f"{field} is too large", code="money_too_large")
    return amount


def money_of(deal: Deal) -> dict:
    """Деньги сделки для ответа API.

    Остаток считается здесь и нигде не хранится: третье поле рядом с суммой и
    предоплатой начало бы расходиться с ними при первой же правке.

    Переплату не запрещаем: клиент округлил вверх, доплатил за срочность — это
    жизнь, а не ошибка. Остаток тогда отрицательный, и это видно.
    """
    amount, prepaid = deal.amount, deal.prepaid or 0
    return {
        "amount": amount,
        "prepaid": prepaid,
        "remainder": None if amount is None else amount - prepaid,
        "is_paid": amount is not None and prepaid >= amount,
    }


def _company_id(db: Session, data: dict) -> int | None:
    """Фирма заявки из запроса, с проверкой существования.

    Проверяем, а не доверяем числу: несуществующий id прошёл бы в базу и всплыл
    бы только при выдаче бланка — пустой шапкой на уже напечатанной бумаге.
    """
    value = data.get("company_id")
    if not value:
        return None
    company_service.get_company(db, int(value))
    return int(value)


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
        # Фирму не подставляем автоматически, хотя основная известна. Пустая
        # ссылка означает «от основной» и следует за ней, если основную потом
        # сменят; проставленная — заморозила бы сегодняшний выбор во всех
        # заявках и разошлась бы со справочником при первой же смене.
        company_id=_company_id(db, data),
        stage=stage,
        sort_order=deals_repo.next_sort_order(db, stage),
        description=(data.get("description") or "").strip(),
        amount=parse_money(data.get("amount"), "amount"),
        prepaid=parse_money(data.get("prepaid"), "prepaid") or 0,
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
    # Прислали null — вернулись к «от основной фирмы». Это законное состояние,
    # а не «поле не трогали», поэтому проверяем наличие ключа, а не значения.
    if "company_id" in data:
        deal.company_id = _company_id(db, data)
    if "lost_reason" in data and data["lost_reason"] is not None:
        deal.lost_reason = data["lost_reason"].strip()[:MAX_LOST_REASON]
    # Сумму можно и снять: прислали null — вернулись к «ещё не назвали».
    if "amount" in data:
        deal.amount = parse_money(data["amount"], "amount")
    if "prepaid" in data:
        deal.prepaid = parse_money(data["prepaid"], "prepaid") or 0

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
