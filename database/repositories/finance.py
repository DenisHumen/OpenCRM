"""Запросы блока «Финансы».

Здесь живёт главное правило блока: **прибыль считается запросом**.

Соблазн вытащить операции за период и сложить их в Python велик и ломается ровно
там, где это дороже всего: экран показывает первую страницу из пятидесяти
операций, а их за квартал три тысячи — и прибыль тихо оказывается неверной,
причём выглядит правдоподобно. Поэтому суммирует всегда база: `SUM(amount_minor)`
не зависит ни от пагинации, ни от того, что успело попасть в сессию.

То же самое относится к факту по бюджету и к разбивке по статьям. Оба приходят
из **одного и того же** агрегата (`by_category`), а не из двух похожих запросов:
две реализации одного числа однажды разойдутся, и на экране «план 50 000, факт
30 000» встанет рядом с «по статье 44 000», а объяснить расхождение будет нечем.
"""

from datetime import date, datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from database.models import (
    Client,
    Deal,
    FinanceBudget,
    FinanceCategory,
    FinanceOperation,
    FinanceRule,
)
from database.models.finance import (
    BASE_INCOME_PERCENT,
    BASE_PER_ORDER,
    CORRECTION_REVERSAL,
    DIRECTION_EXPENSE,
    DIRECTION_INCOME,
)
from database.query import page_of


def _chain():
    """Голова цепочки поправок: `COALESCE(corrects_id, id)`.

    Исходное начисление и все его поправки складываются в одну строку врезки
    «Деньги по заказу»: было 80, стало 140 — это две записи в базе и одна цифра
    на экране. Выражение живёт одной функцией, потому что группировка по нему
    нужна в двух запросах, а разойдясь, они дали бы две разные суммы по одной и
    той же упаковке.
    """
    return func.coalesce(FinanceOperation.corrects_id, FinanceOperation.id)


# --- статьи ---


def get_category(
    db: Session, category_id: int, include_deleted: bool = False
) -> FinanceCategory | None:
    category = db.get(FinanceCategory, category_id)
    if category is None:
        return None
    if category.deleted_at is not None and not include_deleted:
        return None
    return category


def list_categories(db: Session, include_deleted: bool = False) -> list[FinanceCategory]:
    """Справочник: сначала доходные, потом расходные, внутри — по алфавиту.

    Порядок задан явно и совпадает с составным индексом: доход и расход — это
    два разных списка в глазах того, кто заводит операцию, и перемешивать их
    значит заставлять читать направление у каждой строки.
    """
    stmt = select(FinanceCategory)
    if not include_deleted:
        stmt = stmt.where(FinanceCategory.deleted_at.is_(None))
    return list(
        db.scalars(
            stmt.order_by(
                # Доход первым: `expense` > `income` по алфавиту, поэтому
                # убывание даёт нужный порядок без отдельной колонки сортировки.
                FinanceCategory.direction.desc(),
                FinanceCategory.name.asc(),
                FinanceCategory.id.asc(),
            )
        )
    )


def categories_by_ids(db: Session, category_ids) -> dict[int, FinanceCategory]:
    """{id: статья} — для строк журнала и разбивки. Закрытые тоже.

    Закрытые обязаны находиться: операция старше закрытия статьи, и подписать её
    нечем, кроме имени той статьи, по которой она прошла.
    """
    category_ids = [i for i in set(category_ids) if i]
    if not category_ids:
        return {}
    rows = db.scalars(
        select(FinanceCategory).where(FinanceCategory.id.in_(category_ids))
    )
    return {row.id: row for row in rows}


def add_category(db: Session, row: FinanceCategory) -> FinanceCategory:
    db.add(row)
    db.flush()
    return row


def category_has_operations(db: Session, category_id: int) -> bool:
    """Были ли по статье операции. Статью с историей закрывают, а не удаляют."""
    return (
        db.scalar(
            select(FinanceOperation.id)
            .where(FinanceOperation.category_id == category_id)
            .limit(1)
        )
        is not None
    )


def category_has_budgets(db: Session, category_id: int) -> bool:
    return (
        db.scalar(
            select(FinanceBudget.id).where(FinanceBudget.category_id == category_id).limit(1)
        )
        is not None
    )


# --- операции ---


def get_operation(db: Session, operation_id: int) -> FinanceOperation | None:
    return db.get(FinanceOperation, operation_id)


def list_operations(
    db: Session,
    *,
    category_id: int | None = None,
    direction: str | None = None,
    deal_id: int | None = None,
    client_id: int | None = None,
    company_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[FinanceOperation], int]:
    """Журнал операций. Период — полуоткрытый интервал [start; end).

    Отбор по направлению идёт СОЕДИНЕНИЕМ со статьёй, а не по знаку суммы. Знак
    и направление — разные сведения (разбор в шапке модели): возврат по расходной
    статье хранится плюсом, и фильтр по знаку записал бы его в доходы.
    """
    stmt = select(FinanceOperation)
    if direction is not None:
        stmt = stmt.join(
            FinanceCategory, FinanceCategory.id == FinanceOperation.category_id
        ).where(FinanceCategory.direction == direction)
    if category_id is not None:
        stmt = stmt.where(FinanceOperation.category_id == category_id)
    if deal_id is not None:
        stmt = stmt.where(FinanceOperation.deal_id == deal_id)
    if client_id is not None:
        stmt = stmt.where(FinanceOperation.client_id == client_id)
    if company_id is not None:
        stmt = stmt.where(FinanceOperation.company_id == company_id)
    if start is not None:
        stmt = stmt.where(FinanceOperation.happened_at >= start)
    if end is not None:
        stmt = stmt.where(FinanceOperation.happened_at < end)
    stmt = stmt.order_by(
        FinanceOperation.happened_at.desc(), FinanceOperation.id.desc()
    )
    return page_of(db, stmt, page=page, per_page=per_page)


def add_operation(db: Session, row: FinanceOperation) -> FinanceOperation:
    db.add(row)
    db.flush()
    return row


# --- правила начисления ---


def get_rule(db: Session, rule_id: int, include_closed: bool = False) -> FinanceRule | None:
    rule = db.get(FinanceRule, rule_id)
    if rule is None:
        return None
    if rule.deleted_at is not None and not include_closed:
        return None
    return rule


def list_rules(db: Session, include_closed: bool = False) -> list[FinanceRule]:
    """Справочник правил в порядке применения.

    Порядок задан явно и совпадает с составным индексом: сначала действующие,
    внутри — по `sort_order`. Читают этот список ровно так, и второго порядка у
    него не бывает.
    """
    stmt = select(FinanceRule)
    if not include_closed:
        stmt = stmt.where(FinanceRule.deleted_at.is_(None))
    return list(
        db.scalars(
            stmt.order_by(
                FinanceRule.is_active.desc(),
                FinanceRule.sort_order.asc(),
                FinanceRule.id.asc(),
            )
        )
    )


def add_rule(db: Session, row: FinanceRule) -> FinanceRule:
    db.add(row)
    db.flush()
    return row


def _live_rules(base: str):
    """Отбор «правило считает прямо сейчас»: действующее и не закрытое.

    Два условия, а не одно, потому что они про разное: `is_active` — «сегодня не
    считаем», `deleted_at` — «правила больше нет». Собраны в одном месте, чтобы
    два входа ниже не разошлись: разойдясь, они дали бы налог, который
    начисляется, но в списке правил не показан.
    """
    return select(FinanceRule).where(
        FinanceRule.base == base,
        FinanceRule.is_active.is_(True),
        FinanceRule.deleted_at.is_(None),
    )


def rules_for_income(db: Session, category_id: int) -> list[FinanceRule]:
    """Правила, которые сработают на приход по этой статье.

    «Пусто или совпало» — это и есть «ставок можно завести несколько, под разные
    виды дохода»: правило без `source_category_id` считает со всякого прихода,
    правило с ним — только со своего.
    """
    stmt = _live_rules(BASE_INCOME_PERCENT).where(
        or_(
            FinanceRule.source_category_id.is_(None),
            FinanceRule.source_category_id == category_id,
        )
    )
    return list(db.scalars(stmt.order_by(FinanceRule.sort_order.asc(), FinanceRule.id.asc())))


def rules_per_order(db: Session) -> list[FinanceRule]:
    """Стандартные расходы, списываемые при закрытии любого заказа."""
    stmt = _live_rules(BASE_PER_ORDER)
    return list(db.scalars(stmt.order_by(FinanceRule.sort_order.asc(), FinanceRule.id.asc())))


def rules_using_category(db: Session, category_id: int) -> list[FinanceRule]:
    """Живые правила, смотрящие на эту статью — куда кладут или с чего считают.

    Нужно ровно для одного отказа: закрыть статью, на которую смотрит правило,
    нельзя. Отказ переносится на момент правки справочника, где человек его
    понимает, — вместо падения при закрытии заказа, где он выглядит поломкой.
    """
    return list(
        db.scalars(
            select(FinanceRule)
            .where(
                FinanceRule.deleted_at.is_(None),
                or_(
                    FinanceRule.category_id == category_id,
                    FinanceRule.source_category_id == category_id,
                ),
            )
            .order_by(FinanceRule.sort_order.asc(), FinanceRule.id.asc())
        )
    )


def count_by_rule(db: Session, rule_id: int) -> int:
    """Сколько раз правило сработало. Считается, а не хранится счётчиком.

    Хранимый счётчик — то же самое, что хранимый остаток склада: разойдётся с
    историей на первом же откате транзакции, и узнать, какое из двух чисел
    верное, будет неоткуда.
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(FinanceOperation)
            .where(FinanceOperation.rule_id == rule_id)
        )
        or 0
    )


# --- деньги по бланку: всё считается запросом ---


def operations_of_document(db: Session, document_id: int) -> list[FinanceOperation]:
    """Все денежные операции по бланку: платежи, начисления, поправки, отмены."""
    return list(
        db.scalars(
            select(FinanceOperation)
            .where(FinanceOperation.document_id == document_id)
            .order_by(FinanceOperation.happened_at.asc(), FinanceOperation.id.asc())
        )
    )


def accruals_of_document(
    db: Session, document_id: int, origin: str | None = None
) -> list[FinanceOperation]:
    """Начисления по бланку — только ГОЛОВЫ цепочек, без поправок и отмен.

    Голова — это то, что начислило правило; поправка и отмена уточняют её и
    отдельными строками врезки не показываются, а складываются в её сумму
    (`accrual_totals`).

    `origin` отбирает начисления по тому, ЧЕМ они вызваны. Врезке «Деньги по
    заказу» нужны все — и упаковка от отгрузки, и налог от платежа; отмене
    проведения — только порождённые закрытием этого заказа. Налог висит на том же
    бланке лишь потому, что его назвал платёж, и снять его отменой заказа значит
    снять его с оборота, который никуда не делся.
    """
    stmt = select(FinanceOperation).where(
        FinanceOperation.document_id == document_id,
        FinanceOperation.rule_id.is_not(None),
        FinanceOperation.correction.is_(None),
    )
    if origin is not None:
        stmt = stmt.where(FinanceOperation.origin == origin)
    return list(db.scalars(stmt.order_by(FinanceOperation.id.asc())))


def accrual_totals(db: Session, operation_ids) -> dict[int, int]:
    """Итог по каждой цепочке поправок: {голова: сумма как лежит в базе}.

    Один запрос на всю врезку, а не запрос на строку. Группировка по
    `COALESCE(corrects_id, id)` — это и есть «экран показывает не последнее
    записанное число, а сумму»: было 80, стало 140 — в базе −8000 и −6000, на
    экране 140, и третья правка добавит ещё строку без единой строки на уборку.
    """
    operation_ids = [i for i in set(operation_ids) if i]
    if not operation_ids:
        return {}
    head = _chain()
    rows = db.execute(
        select(head, func.coalesce(func.sum(FinanceOperation.amount_minor), 0))
        .where(head.in_(operation_ids))
        .group_by(head)
    ).all()
    return {int(head_id): int(total or 0) for head_id, total in rows}


def accrual_total(db: Session, operation_id: int) -> int:
    """Сколько по этой цепочке начислено сейчас, с учётом всех поправок."""
    return accrual_totals(db, [operation_id]).get(operation_id, 0)


def reversal_of(db: Session, operation_id: int) -> FinanceOperation | None:
    """Операция, отменяющая эту. Второй раз отменить одно и то же нельзя.

    Образец — `warehouses_repo.reversal_of` у перемещений: там тот же вопрос и
    тот же ответ.
    """
    return db.scalars(
        select(FinanceOperation).where(
            FinanceOperation.corrects_id == operation_id,
            FinanceOperation.correction == CORRECTION_REVERSAL,
        )
    ).first()


def reverted_heads(db: Session, operation_ids) -> set[int]:
    """Какие из этих начислений уже сторнированы — одним запросом на врезку.

    Отдельно от `reversal_of`, и по той же причине, что у перемещений склада
    (`warehouses_repo.reverted_ids`): «отменено ли вот это» спрашивают по одному,
    а «какие из десяти отменены» — списком, и запрос на строку превратил бы
    врезку в десяток обращений к базе.
    """
    operation_ids = [i for i in set(operation_ids) if i]
    if not operation_ids:
        return set()
    rows = db.scalars(
        select(FinanceOperation.corrects_id).where(
            FinanceOperation.corrects_id.in_(operation_ids),
            FinanceOperation.correction == CORRECTION_REVERSAL,
        )
    )
    return {row for row in rows if row is not None}


def received_of(
    db: Session,
    document_id: int | None = None,
    deal_id: int | None = None,
    document_ids=None,
) -> int:
    """Сколько денег получено по бланку или по заявке, в минорных единицах.

    Колонок `documents.paid_total`, `paid_at` и `is_paid` не существует и не
    появится: получено — это `SUM` доходных операций, остаток — сумма бланка
    минус она, «оплачен» — остаток не больше нуля. Хранимое число разошлось бы с
    историей на первом же возврате.

    Начисления сюда не попадают дважды: отбор идёт по направлению СТАТЬИ, а налог
    ложится в расходную. Но `rule_id IS NULL` стоит отдельным условием — на
    случай правила, кладущего начисленное в доходную статью (возврат
    переплаченного налога — законная такая статья). Деньги в кассу от этого не
    приходили.
    """
    if document_id is None and deal_id is None and document_ids is None:
        return 0
    if document_ids is not None:
        # По нескольким бумагам разом — возвраты одного заказа.
        document_ids = [int(i) for i in document_ids if i]
        if not document_ids:
            return 0
    stmt = (
        select(func.coalesce(func.sum(FinanceOperation.amount_minor), 0))
        .join(FinanceCategory, FinanceCategory.id == FinanceOperation.category_id)
        .where(*_postupleniya())
    )
    if document_id is not None:
        stmt = stmt.where(FinanceOperation.document_id == document_id)
    if document_ids is not None:
        stmt = stmt.where(FinanceOperation.document_id.in_(document_ids))
    if deal_id is not None:
        stmt = stmt.where(FinanceOperation.deal_id == deal_id)
    return int(db.scalar(stmt) or 0)


def received_of_client(db: Session, client_id: int, since=None) -> int:
    """Сколько денег пришло от клиента — тем же отбором, что и по бланку.

    По `client_id` операции, а не через заявки: платёж по заказу без заявки
    клиента называет сам, и через заявки он потерялся бы.
    """
    stmt = (
        select(func.coalesce(func.sum(FinanceOperation.amount_minor), 0))
        .join(FinanceCategory, FinanceCategory.id == FinanceOperation.category_id)
        .where(*_postupleniya(), FinanceOperation.client_id == client_id)
    )
    if since is not None:
        stmt = stmt.where(FinanceOperation.happened_at >= since)
    return int(db.scalar(stmt) or 0)


def _postupleniya():
    """Что считается пришедшими в кассу деньгами. Одно условие на весь файл.

    Вынесено отдельно, потому что тем же отбором считаются ДВА разных числа:
    «получено по этому заказу» во врезке и «поступило за месяц» в отчёте о
    выручке. Напиши их порознь — и разойдутся они не сразу, а на первом же
    правиле, кладущем начисленное в доходную статью; в чужой цифре это выглядит
    как ошибка отчёта, и искать её будут в отчёте.

    Оба условия обязательны. Направление СТАТЬИ отсекает расходы. `rule_id IS
    NULL` отсекает начисленное правилом: возврат переплаченного налога —
    законная доходная статья, но денег в кассу от него не приходило.
    """
    return (
        FinanceCategory.direction == DIRECTION_INCOME,
        FinanceOperation.rule_id.is_(None),
    )


def _moi_dengi(only_manager_id: int | None):
    """Сужение кассы до своих денег — двумя ступенями, а не одной.

    Сузить только по заявке значит воспроизвести ровно ту слепоту, ради которой
    всё это и затевалось: заказ законно живёт без заявки (у стойки сначала
    набивают позиции), и менеджер снова увидел бы ноль при полной кассе.
    Поэтому вторая ступень — клиент; та же пара осей, по которой уже считается
    отчёт по источникам.

    Деньги без заявки И без клиента (прохожий у стойки) не принадлежат никому и
    в суженный отчёт не попадают. Это не пропажа: их видит тот, у кого есть
    право на чужие заявки, — и об этом сказано на экране.
    """
    if only_manager_id is None:
        return ()
    return (
        or_(
            Deal.manager_id == only_manager_id,
            and_(
                FinanceOperation.deal_id.is_(None),
                Client.manager_id == only_manager_id,
            ),
        ),
    )


def postupleniya_po_mesyatsam(
    db: Session,
    buckets: list[tuple[datetime, datetime]],
    only_manager_id: int | None = None,
) -> dict[int, dict[str, int]]:
    """Сколько денег пришло в кассу по месяцам — одним запросом.

    Ось времени здесь `happened_at` («когда деньги двинулись»), а не
    `created_at`: задним числом внесённый платёж принадлежит своему дню, иначе
    закрытый месяц зависел бы от того, когда до него дошли руки.

    Границы месяцев приходят готовыми, а не считаются в SQL, — по тому же
    доводу, что у соседа (`reports.money_by_month`): `strftime` диалектен и
    считал бы месяц по UTC.

    Ключ результата — номер месяца в списке.
    """
    if not buckets:
        return {}

    bucket = case(
        *[
            (
                and_(
                    FinanceOperation.happened_at >= start,
                    FinanceOperation.happened_at < end,
                ),
                index,
            )
            for index, (start, end) in enumerate(buckets)
        ],
        else_=None,
    )
    stmt = (
        select(
            bucket.label("bucket"),
            func.count(),
            func.coalesce(func.sum(FinanceOperation.amount_minor), 0),
        )
        .join(FinanceCategory, FinanceCategory.id == FinanceOperation.category_id)
        .where(
            *_postupleniya(),
            FinanceOperation.happened_at >= buckets[0][0],
            FinanceOperation.happened_at < buckets[-1][1],
        )
        .group_by(bucket)
    )
    if only_manager_id is not None:
        # Присоединяем внешним: операция без заявки или без клиента обязана
        # дойти до условия, а не отсеяться самим соединением.
        stmt = stmt.outerjoin(Deal, Deal.id == FinanceOperation.deal_id).outerjoin(
            Client, Client.id == FinanceOperation.client_id
        )
        stmt = stmt.where(*_moi_dengi(only_manager_id))

    itog: dict[int, dict[str, int]] = {}
    for index, count, total in db.execute(stmt).all():
        if index is None:
            continue
        yacheyka = itog.setdefault(int(index), {"count": 0, "total": 0})
        yacheyka["count"] += int(count or 0)
        yacheyka["total"] += int(total or 0)
    return itog


# --- прибыль: считается запросом и нигде не хранится ---


def totals(db: Session, start: datetime, end: datetime) -> dict[str, int]:
    """Доход, расход и прибыль за период — одним запросом, в минорных единицах.

    Доход и расход разделяет направление СТАТЬИ, поэтому здесь соединение.
    Хранимый расход отрицателен, и наружу он отдаётся положительным числом:
    «потрачено 40 000» читается, «потрачено −40 000» — нет. Прибыль при этом
    остаётся простой суммой всего, что было, и считать её вычитанием
    приведённых чисел не нужно — обе дороги ведут к одному, и вторая короче.

    coalesce — потому что SUM по пустому набору даёт NULL, а «операций не было»
    означает ровно ноль, а не «неизвестно».
    """
    rows = db.execute(
        select(
            FinanceCategory.direction,
            func.coalesce(func.sum(FinanceOperation.amount_minor), 0),
        )
        .join(FinanceCategory, FinanceCategory.id == FinanceOperation.category_id)
        .where(
            FinanceOperation.happened_at >= start,
            FinanceOperation.happened_at < end,
        )
        .group_by(FinanceCategory.direction)
    ).all()

    by_direction = {direction: int(total or 0) for direction, total in rows}
    income = by_direction.get(DIRECTION_INCOME, 0)
    # Расход лежит минусом — переворачиваем целочисленным умножением, а не
    # abs(): возврат может увести расход по статье в плюс, и abs() показал бы
    # «потрачено» там, где на самом деле вернули больше, чем потратили.
    spent = -by_direction.get(DIRECTION_EXPENSE, 0)
    return {"income": income, "expense": spent, "profit": income - spent}


def by_category(db: Session, start: datetime, end: datetime) -> dict[int, dict[str, int]]:
    """Итог по каждой статье за период: {category_id: {"total_minor", "count"}}.

    Один запрос на всю разбивку, а не запрос на статью: справочник в живой
    системе разрастается до полусотни строк, и отчёт превратился бы в полсотни
    обращений к базе.

    Итог отдаётся ЗНАКОВЫМ, как он лежит: приводить его к «терминам статьи»
    здесь значило бы тянуть сюда направление и делать это в двух местах — тут и
    при записи операции. Переворачивает знак тот, кто показывает
    (`finance_service`), и делает это одним и тем же выражением на обе стороны.
    """
    rows = db.execute(
        select(
            FinanceOperation.category_id,
            func.coalesce(func.sum(FinanceOperation.amount_minor), 0),
            func.count(),
        )
        .where(
            FinanceOperation.happened_at >= start,
            FinanceOperation.happened_at < end,
        )
        .group_by(FinanceOperation.category_id)
    ).all()
    return {
        category_id: {"total_minor": int(total or 0), "count": int(count or 0)}
        for category_id, total, count in rows
    }


# --- бюджеты ---


def get_budget(db: Session, budget_id: int) -> FinanceBudget | None:
    return db.get(FinanceBudget, budget_id)


def list_budgets(
    db: Session, start: date | None = None, end: date | None = None
) -> list[FinanceBudget]:
    """Планы, пересекающиеся с периодом [start; end] (границы включительные).

    Именно пересекающиеся, а не «начавшиеся внутри»: квартальный план обязан
    попасть в выдачу, когда смотрят один месяц этого квартала. Иначе экран
    августа показал бы «плана нет» при живом плане на весь третий квартал.
    """
    stmt = select(FinanceBudget)
    if end is not None:
        stmt = stmt.where(FinanceBudget.period_start <= end)
    if start is not None:
        stmt = stmt.where(FinanceBudget.period_end >= start)
    return list(
        db.scalars(
            stmt.order_by(
                FinanceBudget.period_start.desc(),
                FinanceBudget.category_id.asc(),
                FinanceBudget.id.asc(),
            )
        )
    )


def budget_for(
    db: Session, category_id: int, period_start: date, period_end: date
) -> FinanceBudget | None:
    """Тот самый план — по статье и точным границам.

    Нужен ровно для одного отказа: «план на этот период уже есть». Проверка
    стоит и в базе (UNIQUE), и здесь — база отвечает на гонку, а этот запрос
    отвечает человеку словами вместо пятисотки.
    """
    return db.scalars(
        select(FinanceBudget).where(
            FinanceBudget.category_id == category_id,
            FinanceBudget.period_start == period_start,
            FinanceBudget.period_end == period_end,
        )
    ).first()


def add_budget(db: Session, row: FinanceBudget) -> FinanceBudget:
    db.add(row)
    db.flush()
    return row


def drop_budget(db: Session, row: FinanceBudget) -> None:
    db.delete(row)
    db.flush()
