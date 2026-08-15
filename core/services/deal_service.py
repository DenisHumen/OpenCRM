"""Сделки: работа для клиента от заявки до закрытия."""

from sqlalchemy.orm import Session

from core import events
from core import exceptions as errors
from core import references
from core.services import audit_service, company_service, pipeline_service
from core.utils import now_utc, to_utc_naive
from database.models import Deal, User
from database.models.audit import SOURCE_MANUAL
from database.models.pipeline import CLOSED_KINDS, KIND_LOST
from database.repositories import deals as deals_repo

MAX_TITLE = 200
MAX_LOST_REASON = 200

#: Сделка сменила этап. Подробности: `deal`, `from_stage`, `to_stage` — ключи
#: этапов. Заведение сделки событием не считается: этапа до неё не было, менять
#: было нечего, — поэтому `from_stage` здесь всегда заполнен.
DEAL_STAGE_CHANGED = "deal.stage_changed"

# Потолок суммы в минимальных единицах. Не про ограничение бизнеса, а про две
# разные беды сразу.
#
# Первая — опечатка: лишний ноль в поле превращает отчёт за месяц в
# бессмыслицу, и заметить это тем труднее, чем позже смотришь.
#
# Вторая — вместимость колонки, и она здесь главная. Стояло `10**12`, то есть
# в 465 раз больше, чем держит `deals.amount` (`Integer`, он же INT в MySQL,
# предел 2 147 483 647). Всё, что между этими числами, проходило СВОЮ проверку
# и упиралось в чужую: MySQL отвечал 1264 «Out of range value for column
# 'amount'», pymysql поднимал `DataError`, а обработчика на этот класс нет —
# человек получал пятисотку без подсказки. Воспроизведено запросом на заведение
# заявки, см. `tests/test_potolki.py`.
#
# Число взято тем же способом, что и `MAX_AMOUNT_MINOR` в
# `database/models/finance.py`, где этот довод однажды уже вывели правильно:
# «это то, что помещается в колонку… узнать об этом отказом вставки на боевом
# сервере было бы худшим из вариантов». Ровно тот случай здесь и был.
#
# Круглое число, а не 2 147 483 647: полтораста миллионов запаса стоят дешевле,
# чем предел, стоящий впритык к чужому.
MAX_MONEY = 2_000_000_000


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
    client_id = references.client(db, client_id)

    stage = data.get("stage") or pipeline_service.first_open_key(db)
    pipeline_service.get_stage(db, stage)   # бросит unknown_stage, если этапа нет

    # «Поле не прислали» и «прислали пустым» — разные вещи. Не указали
    # ответственного — ставим автора, чтобы сделка не осталась ничьей случайно.
    # Указали пусто явно — значит так и хотели: общая очередь, разберут потом.
    manager_id = (
        references.user(
            db, data["manager_id"], code="manager_not_found", message="Manager not found"
        )
        if "manager_id" in data
        else author.id
    )

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
        due_at=to_utc_naive(data.get("due_at")),
        closed_at=now_utc() if pipeline_service.is_closed(db, stage) else None,
    )
    db.add(deal)
    db.flush()
    # Первая запись журнала — с пустым «откуда»: до создания этапа не было.
    deals_repo.add_stage_change(db, deal.id, "", stage, author.id)
    return deal


def update_deal(
    db: Session,
    deal_id: int,
    data: dict,
    author: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> Deal:
    deal = get_deal(db, deal_id)

    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise errors.ValidationError("Title is required", code="title_required")
        deal.title = title[:MAX_TITLE]
    if "description" in data and data["description"] is not None:
        deal.description = data["description"].strip()
    if "due_at" in data:
        # Смещение зоны приводим, а не отбрасываем: «18:00 по Киеву» — это
        # 15:00 UTC, и без приведения срок наступал на три часа позже, чем
        # человек назначил. В ответе при этом стояло присланное значение, то
        # есть API отвечал не тем, что записал.
        deal.due_at = to_utc_naive(data["due_at"])
    if "manager_id" in data:
        deal.manager_id = references.user(
            db, data["manager_id"], code="manager_not_found", message="Manager not found"
        )
    if "client_id" in data and data["client_id"]:
        deal.client_id = references.client(db, data["client_id"])
    # Прислали null — вернулись к «от основной фирмы». Это законное состояние,
    # а не «поле не трогали», поэтому проверяем наличие ключа, а не значения.
    if "company_id" in data:
        deal.company_id = _company_id(db, data)
    if "lost_reason" in data and data["lost_reason"] is not None:
        deal.lost_reason = data["lost_reason"].strip()[:MAX_LOST_REASON]
    # Сумму можно и снять: прислали null — вернулись к «ещё не назвали».
    # Старое значение забираем ДО присваивания: после него спросить уже не у
    # кого, а «сумма изменена» без «было 5 000, стало 500» не отвечает ни на
    # один вопрос, ради которого в журнал заходят.
    for field, action in (
        ("amount", audit_service.ACTION_DEAL_AMOUNT_CHANGED),
        ("prepaid", audit_service.ACTION_DEAL_PREPAID_CHANGED),
    ):
        if field not in data:
            continue
        was = getattr(deal, field)
        now = parse_money(data[field], field)
        if field == "prepaid":
            now = now or 0
        setattr(deal, field, now)
        if now == was:
            # Форма присылает поле целиком при каждом сохранении. Записывать
            # «изменил сумму с 5000 на 5000» значит утопить настоящие правки в
            # шуме — а искать их будут именно в этом журнале.
            continue
        db.flush()
        audit_service.record(
            db,
            action=action,
            actor=author,
            source=source,
            source_ref=source_ref,
            entity_type=audit_service.ENTITY_DEAL,
            entity_id=deal.id,
            entity_label=deal.title,
            before=audit_service.money_text(was),
            after=audit_service.money_text(now),
        )

    # Этап меняем через общий путь, чтобы журнал заполнялся и здесь тоже.
    if "stage" in data and data["stage"] and data["stage"] != deal.stage:
        move_stage(
            db,
            deal_id,
            data["stage"],
            author,
            lost_reason=data.get("lost_reason"),
            # Причина у двух путей разная, и разница видна в ленте: карточку
            # правят осознанно, карточку на доске перетаскивают мимоходом.
            reason="edited on the deal card",
            source=source,
            source_ref=source_ref,
        )
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
    reason: str = "moved on the board",
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> Deal:
    """Передвинуть сделку по воронке.

    Единственная точка смены этапа: журнал должен заполняться всегда, иначе
    отчёт «сколько сделка стоит в этапе» окажется дырявым ровно там, где кто-то
    поменял этап другим путём. По той же причине событие поднимается отсюда, а
    не из роутов: путей в эту функцию два, и подписчик не должен зависеть от
    того, каким из них пришли.
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

    values: dict = {
        "stage": stage,
        "sort_order": (
            sort_order if sort_order is not None else deals_repo.next_sort_order(db, stage)
        ),
    }
    if target.kind in CLOSED_KINDS:
        values["closed_at"] = now_utc()
        if target.kind == KIND_LOST and lost_reason is not None:
            values["lost_reason"] = lost_reason.strip()[:MAX_LOST_REASON]
    else:
        # Вернули из закрытых в работу — дата закрытия и причина больше не верны.
        values["closed_at"] = None
        values["lost_reason"] = ""

    # Этап меняем условием «пока он тот, что мы прочитали», а не присваиванием:
    # почему именно так и что ломалось без этого — в `deals_repo.take_stage`.
    if not deals_repo.take_stage(db, deal, expected=previous, values=values):
        raise errors.ConflictError(
            "The deal has already been moved by someone else — refresh and try again",
            code="stage_moved_meanwhile",
        )
    deals_repo.add_stage_change(db, deal.id, previous, stage, author.id)
    # Журнал пишем здесь, а не подписчиком на это же событие. Наблюдатель
    # работает под точкой отката и своё падение проглатывает — то есть смена
    # этапа могла бы состояться без записи о ней, молча и незаметно. Участник
    # не проглатывает, но участников зовут до того, как операция признана
    # состоявшейся, и запись появлялась бы раньше самого перехода.
    audit_service.record(
        db,
        action=audit_service.ACTION_DEAL_STAGE_CHANGED,
        actor=author,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_DEAL,
        entity_id=deal.id,
        entity_label=deal.title,
        before=previous,
        after=stage,
    )
    events.emit(
        DEAL_STAGE_CHANGED,
        db=db,
        actor=author,
        reason=reason,
        source=source,
        source_ref=source_ref,
        deal=deal,
        from_stage=previous,
        to_stage=stage,
    )
    return deal


def delete_deal(
    db: Session,
    deal_id: int,
    actor: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> None:
    deal = get_deal(db, deal_id)
    deal.deleted_at = now_utc()
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_DEAL,
        entity_id=deal.id,
        entity_label=deal.title,
    )


def board(db: Session, only_manager_id: int | None = None) -> list[dict]:
    """Канбан: колонки по этапам со сделками внутри.

    Состав и порядок колонок берём из воронки, а не из констант: у ремонта
    техники, салона и магазина этапы разные, и доска обязана показывать их.

    Колонки остаются все, даже если сотруднику видны не все заявки: пустая
    колонка — это ответ («в согласовании у меня ничего»), а исчезнувшая — повод
    решить, что этап убрали из воронки.
    """
    return [
        {
            "stage": stage,
            "deals": deals_repo.by_stage(db, stage.key, only_manager_id=only_manager_id),
        }
        for stage in pipeline_service.list_stages(db)
    ]


def ensure_visible(db: Session, deal: Deal, only_manager_id: int | None) -> Deal:
    """Отказать, если заявка чужая, а права видеть чужие нет.

    403 с названной причиной, а не 404. Молчаливое «не найдено» здесь было бы
    ложью дважды: заявка существует, и сотрудник о ней, скорее всего, знает —
    коллега прислал ссылку. Ответ «нужно право deals.view_others» говорит
    правду и подсказывает, что просить у того, кто раздаёт доступы.
    """
    if only_manager_id is None or deal.manager_id == only_manager_id:
        return deal
    raise errors.ForbiddenError(
        "Permission required: deals.view_others", code="permission_denied"
    )
