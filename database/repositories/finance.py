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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import FinanceBudget, FinanceCategory, FinanceOperation
from database.models.finance import DIRECTION_EXPENSE, DIRECTION_INCOME
from database.query import page_of

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
