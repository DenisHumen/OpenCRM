from sqlalchemy import Integer, func, insert, literal, or_, select, update
from sqlalchemy.orm import Session

from core.utils import divide_money
from database.models import Client, Deal, DealStageChange, PipelineStage
from database.models.pipeline import CLOSED_KINDS, KIND_OPEN, KIND_WON
from database.query import contains, contains_norm, page_of, page_without_total
from database.repositories import pipeline as pipeline_repo


def get(db: Session, deal_id: int, include_deleted: bool = False) -> Deal | None:
    deal = db.get(Deal, deal_id)
    if deal is None:
        return None
    if deal.deleted_at is not None and not include_deleted:
        return None
    return deal


def _search_stmt(
    q: str | None = None,
    stage: str | None = None,
    client_id: int | None = None,
    manager_id: int | None = None,
    include_closed: bool = True,
    only_manager_id: int | None = None,
):
    """Условия отбора заявок — одни и те же для списка и для палитры.

    **Имя клиента ищется подзапросом, а не соединением.** Прежний
    `join(Client, Client.id == Deal.client_id)` заставлял планировщик вести
    запрос ОТ клиентов: 200 000 проходов по клиентам и на каждый — поиск в
    `ix_deals_client_id`. С подзапросом остаётся один проход по заявкам.
    Замерено на большой базе (400 000 заявок): 2457 → 440 мс на счёт, 2481 →
    449 мс на шесть строк; найденное совпало до строки.

    Смысл при этом не изменился ни на запись. `deals.client_id` — NOT NULL с
    внешним ключом, поэтому внутреннее соединение не теряло строк и не
    задваивало их. Мягко удалённых клиентов оно тоже не отсеивало — подзапрос
    сознательно повторяет это и не фильтрует `Client.deleted_at`, иначе
    множество найденного поехало бы.

    **По клиенту ищется по-прежнему ИМЯ, а не вся его склейка.** Склейка стоит
    в подзапросе первым условием — дешёвым предварительным отсевом, — а второе,
    точное, повторяет прежний `contains(Client.name, …)`. Так набор найденного
    остаётся ровно тем же: замерено на большой базе, «ООО» по одной склейке
    давало 32 792 заявки вместо нуля, потому что «ООО» стоит в НАЗВАНИИ ФИРМЫ
    клиента, а по нему заявки никогда не искали.

    Платы за точность почти нет: `lower(Client.name)` считается только для
    строк, прошедших первое условие, — а их единицы или тысячи вместо двухсот
    тысяч. Отсев при этом честен: имя целиком входит в склейку, поэтому всё, что
    находит точное условие, находит и предварительное.

    Подзапрос не коррелирован: MySQL материализует его один раз, диалектных
    веток здесь нет.
    """
    stmt = select(Deal).where(Deal.deleted_at.is_(None))
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    if q:
        needle = q.strip()
        # Ищем и по названию клиента: в жизни спрашивают «что там по Ромашке»,
        # а не «как называлась та сделка».
        po_klientu = Deal.client_id.in_(
            select(Client.id).where(
                # Сначала дешёвый отсев по склейке, затем точное условие по
                # имени — разбор в докстроке.
                contains_norm(Client.search_text, needle),
                contains(Client.name, needle),
            )
        )
        stmt = stmt.where(or_(contains_norm(Deal.search_text, needle), po_klientu))
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

    return stmt.order_by(Deal.updated_at.desc())


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
    stmt = _search_stmt(q, stage, client_id, manager_id, include_closed, only_manager_id)
    return page_of(db, stmt, page=page, per_page=per_page)


def search_top(
    db: Session,
    q: str | None = None,
    *,
    page: int = 1,
    per_page: int = 6,
    only_manager_id: int | None = None,
) -> tuple[list[Deal], bool]:
    """Страница заявок для командной палитры — без счёта найденного.

    `only_manager_id` обязателен здесь ровно так же, как в `search`: без него
    палитра стала бы самым удобным способом обойти `deals.view_others` —
    список показывает три карточки, а Ctrl+K по тому же слову все тридцать.
    Сужение прикладывается к КАЖДОЙ странице, а не только к первой: иначе вторая
    страница стала бы дырой в том же праве, только менее заметной.

    `page` заведён по беде: палитра показывала шесть заявок и продолжения не
    имела вовсе — «есть ещё» она говорила, а достать это «ещё» было нечем.
    """
    return page_without_total(
        db,
        _search_stmt(q, only_manager_id=only_manager_id),
        page=page,
        per_page=per_page,
    )


def by_stage(
    db: Session,
    stage: str,
    *,
    page: int = 1,
    per_page: int = 100,
    only_manager_id: int | None = None,
) -> tuple[list[Deal], int]:
    """Страница колонки канбана и сколько в ней всего.

    Порядок — заданный руками, при равенстве свежие выше.

    Прежде колонка отдавалась одним куском с пределом в две сотни, и `total`
    не считался вовсе: заявка номер двести один на этап просто не попадала на
    доску. Признака этому не было — колонка выглядела полной, а итог над ней
    считается отдельным запросом по всем заявкам этапа и потому оставался
    верным, то есть даже расхождение сумм пропажу не выдавало.
    """
    stmt = select(Deal).where(Deal.deleted_at.is_(None), Deal.stage == stage)
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    return page_of(
        db,
        stmt.order_by(Deal.sort_order.asc(), Deal.id.desc()),
        page=page,
        per_page=per_page,
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
    # Ключи этапов вместо соединения со справочником — по той же причине, что и
    # в отчётах: сводка отбирает заявки узким окном по `closed_at`, а
    # соединение уводит план на справочник этапов. Разбор и замеры — в
    # `pipeline_repo.kinds_by_key`. Справочник читается ОДИН раз на всю сводку:
    # четыре запроса ниже спрашивают его об одном и том же.
    kinds = pipeline_repo.kinds_by_key(db)

    def keys_of(kind: str) -> list[str]:
        return [key for key, k in kinds.items() if k == kind]

    def total(kind: str, closed_since=None) -> int:
        keys = keys_of(kind)
        if not keys:
            return 0
        query = select(func.coalesce(func.sum(Deal.amount), 0)).where(
            Deal.deleted_at.is_(None), Deal.stage.in_(keys)
        )
        if only_manager_id is not None:
            query = query.where(Deal.manager_id == only_manager_id)
        if closed_since is not None:
            query = query.where(Deal.closed_at >= closed_since)
        return int(db.scalar(query) or 0)

    won_keys = keys_of(KIND_WON)

    # Знаменатель среднего чека — сделки с НАЗВАННОЙ суммой. `count(amount)`
    # не считает NULL, и это здесь главное: «сумму ещё не назвали» — не то же
    # самое, что «работа бесплатная». Возьми в знаменатель все выигранные — и
    # каждая сделка без цены будет тихо занижать средний чек.
    # Пустой список ключей SQLAlchemy превращает в заведомо ложное условие — то
    # есть «выигранных этапов нет» честно даёт ноль, а не всю таблицу.
    priced = select(func.count(Deal.amount)).where(
        Deal.deleted_at.is_(None),
        Deal.stage.in_(won_keys),
        Deal.closed_at >= since,
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
        .where(
            Deal.deleted_at.is_(None),
            Deal.stage.in_(won_keys),
            Deal.closed_at >= since,
        )
    )
    if only_manager_id is not None:
        counted = counted.where(Deal.manager_id == only_manager_id)
    won_count = int(db.scalar(counted) or 0)

    # К получению: цена открытых заявок минус предоплата, только где она
    # положительна — переплата соседей чужой долг не гасит. Здесь, а не своей
    # функцией: справочник этапов на сводку читается ровно один раз
    # (`tests/test_speed.py`), и второй запрос читал бы его снова.
    open_keys = keys_of(KIND_OPEN)
    due = 0
    if open_keys:
        dolg = select(func.coalesce(func.sum(Deal.amount - Deal.prepaid), 0)).where(
            Deal.deleted_at.is_(None),
            Deal.stage.in_(open_keys),
            Deal.amount.is_not(None),
            Deal.amount > Deal.prepaid,
        )
        if only_manager_id is not None:
            dolg = dolg.where(Deal.manager_id == only_manager_id)
        due = int(db.scalar(dolg) or 0)

    return {
        "in_work": total(KIND_OPEN),
        "due": due,
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
        # Делим тем же правилом, что и отчёт о выручке (`core/utils.divide_money`):
        # целочисленно и с округлением половины ОТ нуля. `round()` округляет
        # половину к чётному, и плитка сводки расходилась с отчётом за тот же
        # период на единицу — под одной подписью «Средний чек».
        "avg_check": divide_money(won_since, priced_won) if priced_won else None,
    }


def stage_counts(db: Session, only_manager_id: int | None = None) -> dict[str, tuple[int, int]]:
    """Сколько заявок в каждом этапе и на какую сумму: {этап: (число, сумма)}.
    Сужается тем же правом, что и суммы: воронка из чужих карточек — это тоже
    сведения о чужой работе."""
    stmt = select(Deal.stage, func.count(), func.coalesce(func.sum(Deal.amount), 0)).where(
        Deal.deleted_at.is_(None)
    )
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    rows = db.execute(stmt.group_by(Deal.stage)).all()
    return {stage: (int(count), int(summa or 0)) for stage, count, summa in rows}


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
    return list(db.scalars(stmt.order_by(Deal.created_at.desc(), Deal.id.desc())))


def in_stages(db: Session, keys) -> list[Deal]:
    """Заявки, стоящие в перечисленных этапах.

    Мягко удалённых берём тоже, и это намеренно: перестройка воронки убирает
    этап целиком, и заявка из корзины, оставленная ссылаться на исчезнувший
    ключ, при восстановлении оказалась бы нигде — ни на доске, ни в списке.
    """
    keys = [key for key in keys if key]
    if not keys:
        return []
    return list(db.scalars(select(Deal).where(Deal.stage.in_(keys))))


def take_stage(db: Session, deal: Deal, *, expected: str, values: dict) -> bool:
    """Сменить этап, пока он тот, что прочитали. False — кто-то успел раньше.

    Условие стоит в самом UPDATE, а не в проверке перед ним. Двое двигают одну
    заявку разом: оба читают `new`, оба пишут по строке журнала, обоим отвечают
    «готово». В базе остаётся последний, а в журнале две записи из одного этапа
    — `new → in_progress` и `new → done`. Перехода `in_progress → done` не было
    ни разу, и отчёт «сколько заявка простояла в этапе» считает по разорванной
    цепочке. Данные при этом целы, тем и неприятно: заметить нечего, пока не
    сверишь журнал с глазами.

    Условие снимает вопрос без блокировок и колонки версии. Отказ честнее
    молчаливой перезаписи: заявку за это время передвинул человек, и решать, что
    делать дальше, ему.
    """
    changed = db.execute(
        update(Deal).where(Deal.id == deal.id, Deal.stage == expected).values(**values)
    )
    if changed.rowcount == 0:
        return False
    # Объект в памяти помнит прежний этап: меняли строку запросом, мимо него.
    db.refresh(deal)
    return True


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


def pereselit_iz_etapov(
    db: Session, keys: list[str], target: str, user_id: int | None
) -> int:
    """Перевести все заявки со снятых этапов на целевой. Двумя запросами.

    Раньше это был цикл по заявкам: на каждую — присвоение `deal.stage` и вызов
    `add_stage_change`, а тот делает `db.flush()`. То есть на каждую заявку
    уходило по отдельному INSERT и UPDATE, и вдобавок все объекты поднимались в
    память. На боевом объёме (в шапке `tests/test_speed.py` он назван: 400 000
    заявок) смена пресета означала бы под миллион отдельных обращений в ОДНОЙ
    транзакции запроса, с блокировками на всей таблице заявок до самого конца.

    Порядок двух запросов — не деталь, а условие правильности. Журнал пишется
    ПЕРВЫМ, потому что берёт `from_stage` из ещё не изменённой строки; поменяй
    их местами — и в журнале окажется переход «из целевого этапа в целевой», то
    есть история переезда, которая ничего не говорит.

    `synchronize_session=False` не забывчивость: объекты заявок в этой сессии
    после массового UPDATE устареют, и полагаться на них нельзя — вызывающий
    после этого их не трогает, а следующий запрос читает базу заново.
    """
    keys = [key for key in keys if key]
    if not keys:
        return 0

    db.execute(
        insert(DealStageChange).from_select(
            ["deal_id", "from_stage", "to_stage", "changed_by"],
            select(
                Deal.id,
                Deal.stage,
                literal(target),
                literal(user_id, type_=Integer),
            ).where(Deal.stage.in_(keys)),
        )
    )
    itog = db.execute(
        update(Deal).where(Deal.stage.in_(keys)).values(stage=target),
        execution_options={"synchronize_session": False},
    )
    return itog.rowcount or 0


def by_ids(db: Session, deal_ids) -> list[Deal]:
    # Порядок задан: список уезжает на карточку товара держателями брони, и без
    # него две загрузки подряд показывают те же заявки в разном порядке.
    if not deal_ids:
        return []
    return list(db.scalars(select(Deal).where(Deal.id.in_(deal_ids)).order_by(Deal.id.asc())))


def zapert_zayavku(db: Session, deal_id: int) -> None:
    """Занять заявку до конца транзакции: остальные ждут своей очереди.

    **Зачем.** «У заявки не больше одного открытого заказа» держится запросом:
    частичных индексов в MySQL нет, а закрытых заказов у заявки бывает сколько
    угодно. Значит проверка в два шага — спросить и вставить, — и между ними
    окно.

    Замерено дуэлью: два нажатия «Собрать заказ» разом дают ДВА открытых заказа,
    пять раз из пяти. Цена не в лишней бумаге: заказ перенимает бронь заявки, и
    два заказа перенимают её дважды — три штуки в строках становятся шестью в
    брони, а продавец отказывает покупателю, глядя на товар на полке.

    **Замок на строку ЗАЯВКИ, а не на бумаги.** Запереть `documents` по заявке
    нельзя: `SELECT ... FOR UPDATE` берёт существующие строки, а спор идёт о
    НОВОЙ, и при `READ COMMITTED` промежутки не запираются. Строка заявки,
    наоборот, одна и та же для обоих. Тот же приём и тот же разбор, что у
    `warehouse.zapert_tovar`.
    """
    db.execute(select(Deal.id).where(Deal.id == deal_id).with_for_update()).all()


def svodka_klienta(db: Session, client_id: int, only_manager_id: int | None = None) -> dict:
    """Заявки клиента по виду этапа: сколько открыто и на сколько, сколько выиграно.

    Одним запросом с группировкой по этапу, а не перебором заявок: карточку
    открывают чаще, чем правят, и сумма обязана считаться там же, где живёт
    остаток склада, — запросом. Вид этапа складывается уже здесь, по
    справочнику (`pipeline_repo.kinds_by_key`), как в сводке.
    """
    kinds = pipeline_repo.kinds_by_key(db)
    stmt = (
        select(Deal.stage, func.count(), func.coalesce(func.sum(Deal.amount), 0))
        .where(Deal.client_id == client_id, Deal.deleted_at.is_(None))
        .group_by(Deal.stage)
    )
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    itog = {"open_count": 0, "open_amount": 0, "won_count": 0, "won_amount": 0, "lost_count": 0}
    for stage, skolko, summa in db.execute(stmt).all():
        kind = kinds.get(stage, KIND_OPEN)
        if kind == KIND_WON:
            itog["won_count"] += int(skolko or 0)
            itog["won_amount"] += int(summa or 0)
        elif kind in CLOSED_KINDS:
            itog["lost_count"] += int(skolko or 0)
        else:
            itog["open_count"] += int(skolko or 0)
            itog["open_amount"] += int(summa or 0)
    return itog


def svodka_po_klientam(db: Session, client_ids, only_manager_id: int | None = None) -> dict[int, dict]:
    """Заявки по каждому клиенту страницы одним запросом: {клиент: {open_count,
    open_amount, won_count}}. Запрос на строку списка превратил бы полсотни
    клиентов в полсотни обращений — та же беда, что закрыта у остатков склада."""
    ids = [int(i) for i in set(client_ids) if i]
    if not ids:
        return {}
    kinds = pipeline_repo.kinds_by_key(db)
    stmt = (
        select(Deal.client_id, Deal.stage, func.count(), func.coalesce(func.sum(Deal.amount), 0))
        .where(Deal.client_id.in_(ids), Deal.deleted_at.is_(None))
        .group_by(Deal.client_id, Deal.stage)
    )
    if only_manager_id is not None:
        stmt = stmt.where(Deal.manager_id == only_manager_id)
    itog: dict[int, dict] = {}
    for client_id, stage, skolko, summa in db.execute(stmt).all():
        yacheyka = itog.setdefault(int(client_id), {"open_count": 0, "open_amount": 0, "won_count": 0})
        kind = kinds.get(stage, KIND_OPEN)
        if kind == KIND_WON:
            yacheyka["won_count"] += int(skolko or 0)
        elif kind not in CLOSED_KINDS:
            yacheyka["open_count"] += int(skolko or 0)
            yacheyka["open_amount"] += int(summa or 0)
    return itog


def otkrytye_po_menedzheram(db: Session) -> dict[int, int]:
    """Сколько открытых заявок у каждого ответственного — одним запросом на штат."""
    kinds = pipeline_repo.kinds_by_key(db)
    otkrytye = [key for key, kind in kinds.items() if kind == KIND_OPEN]
    if not otkrytye:
        return {}
    ryady = db.execute(
        select(Deal.manager_id, func.count())
        .where(Deal.deleted_at.is_(None), Deal.manager_id.is_not(None), Deal.stage.in_(otkrytye))
        .group_by(Deal.manager_id)
    ).all()
    return {int(manager_id): int(skolko) for manager_id, skolko in ryady}
