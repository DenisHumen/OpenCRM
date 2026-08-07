"""Финансы: статьи, операции, бюджеты, прибыль.

Правила блока, которые легко нарушить и тяжело потом починить:

1. **Прибыль нигде не хранится** — она равна сумме операций за период и
   приходит из репозитория одним запросом. То же относится к факту по бюджету
   и к разбивке по статьям.
2. **Деньги — целые в минорных единицах** (копейки, центы). Через `float` они не
   проходят ни на секунду: ни на входе, ни в промежуточных вычислениях, ни при
   переворачивании знака. Отсюда `int` везде и умножение на ±1 вместо `abs()`.
3. **Операция не правится и не удаляется.** Ошибку исправляют обратной
   операцией. Правка задним числом означала бы, что прибыль за прошлый квартал
   зависит от того, когда её спросили; ровно то же правило и по той же причине
   действует у движений склада.
4. **Направление статьи неизменно.** Знак операции ставится по направлению её
   статьи в момент записи; перевернуть направление задним числом значит молча
   превратить весь прошлый расход в доход.

Про «в терминах статьи». Наружу и внутрь суммы ездят так, как их называет
человек: у расходной статьи «50 000» — это «потратили пятьдесят тысяч». В базе
расход лежит минусом, чтобы прибыль была простым `SUM`. Перевод между двумя
видами — одно умножение на ±1, и делает его `in_terms_of` в обе стороны.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import references
from core import uniqueness
from core.services import audit_service, company_service
from core.utils import now_utc, to_utc_naive
from database.models import FinanceBudget, FinanceCategory, FinanceOperation, User
from database.models.audit import SOURCE_MANUAL
from database.models.finance import (
    DIRECTION_INCOME,
    DIRECTIONS,
    MAX_AMOUNT_MINOR,
    PURPOSE_GENERAL,
    PURPOSE_SALARY,
    PURPOSE_TAX,
    PURPOSES,
)
from database.repositories import finance as finance_repo

#: Что записывается в журнал действий.
#:
#: Строки вида «объект.действие» — список действий открытый нарочно (см. шапку
#: `audit_service`): интерфейс, не знающий действия, показывает его ключ как
#: есть. Операция попадает в журнал по той же причине, что и движение склада:
#: это изменение денег, а журнал существует ради вопроса «с чьей руки».
ACTION_OPERATION_ADDED = "finance.operation_added"
ACTION_BUDGET_SET = "finance.budget_set"

#: Типы объектов для журнала. Свои, а не общий «finance»: вопрос «куда делась
#: статья» и вопрос «кто завёл этот расход» задают порознь, и отбор в журнале
#: должен их различать.
ENTITY_CATEGORY = "finance_category"
ENTITY_OPERATION = "finance_operation"
ENTITY_BUDGET = "finance_budget"

#: Сколько знаков имени статьи помещается в колонку.
MAX_CATEGORY_NAME = 200


# --- деньги ---


def parse_amount_minor(value, field: str = "amount", *, allow_negative: bool = True) -> int:
    """Сумма из запроса в минорные единицы. Только целое.

    Дробей здесь не принимаем **вовсе**, в отличие от количества на складе:
    у денег ровно два знака после запятой, и умножение на сто в браузере
    безобидно (разбор — в `warehouse_service.parse_quantity`). Пришло «12.5» —
    это ошибка вызывающего, а не повод округлить: округлив, мы получили бы
    расхождение в копейку, которое потом ищут по всему отчёту.

    `bool` отсекаем до `int()` отдельно: в Python `True` — это единица, и
    `{"amount": true}` тихо превратилось бы в операцию на одну копейку.

    Ноль запрещён: операция на ноль ничего не описывает и в прибыли не меняет
    ничего, зато занимает строку в журнале и сбивает счётчик «сколько операций
    по статье».
    """
    if isinstance(value, bool) or value is None or value == "":
        raise errors.ValidationError(
            f"{field} must be a whole number of minor units", code="bad_money"
        )
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise errors.ValidationError(
            f"{field} must be a whole number of minor units", code="bad_money"
        ) from None
    if amount == 0:
        raise errors.ValidationError(f"{field} cannot be zero", code="zero_money")
    if amount < 0 and not allow_negative:
        raise errors.ValidationError(f"{field} cannot be negative", code="negative_money")
    if abs(amount) > MAX_AMOUNT_MINOR:
        raise errors.ValidationError(f"{field} is too large", code="money_too_large")
    return amount


def in_terms_of(direction: str, amount_minor: int) -> int:
    """Перевод суммы между «как называет человек» и «как лежит в базе».

    Функция одна на оба направления нарочно: это умножение на ±1, то есть
    обратное самому себе. Две функции с именами `to_stored` и `from_stored`
    выглядели бы честнее, но однажды разошлись бы — а разошедшись, дали бы
    расход, показанный доходом, без единого признака поломки.

    Умножение, а не `abs()`: возврат по расходной статье хранится ПЛЮСОМ, и
    `abs()` показал бы «потратили» там, где на самом деле вернули.
    """
    return amount_minor if direction == DIRECTION_INCOME else -amount_minor


# --- статьи ---


def list_categories(db: Session, include_deleted: bool = False) -> list[FinanceCategory]:
    return finance_repo.list_categories(db, include_deleted=include_deleted)


def get_category(db: Session, category_id: int, include_deleted: bool = False) -> FinanceCategory:
    category = finance_repo.get_category(db, category_id, include_deleted=include_deleted)
    if category is None:
        raise errors.NotFoundError("Category not found", code="category_not_found")
    return category


def _clean_name(value) -> str:
    name = (value or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    return name[:MAX_CATEGORY_NAME]


def _clean_purpose(value) -> str:
    """Признак «налоги / зарплата / обычная».

    Направление здесь НЕ проверяется, и это решение, а не пропуск. Признак
    отвечает на вопрос «к чему это относится», а не «куда двинулись деньги»:
    возврат переплаченного налога — доход с налоговым признаком, и запретить
    такую статью значило бы заставить завести её без признака, то есть потерять
    её из строки «сколько отдали государству».
    """
    purpose = (value or PURPOSE_GENERAL).strip()
    if purpose not in PURPOSES:
        raise errors.ValidationError(f"Unknown purpose: {purpose}", code="unknown_purpose")
    return purpose


def create_category(db: Session, data: dict) -> FinanceCategory:
    direction = (data.get("direction") or "").strip()
    if direction not in DIRECTIONS:
        raise errors.ValidationError(
            f"Unknown direction: {direction or '—'}", code="unknown_direction"
        )
    return finance_repo.add_category(
        db,
        FinanceCategory(
            name=_clean_name(data.get("name")),
            direction=direction,
            purpose=_clean_purpose(data.get("purpose")),
            note=(data.get("note") or "").strip(),
        ),
    )


def update_category(db: Session, category_id: int, data: dict) -> FinanceCategory:
    """Правка статьи. Направление не меняется — и это главное здесь.

    Знак операции ставится по направлению статьи в момент записи и хранится
    вместе с ней. Перевернуть направление задним числом значит объявить весь
    прошлый расход доходом: суммы в базе остались прежними, а отчёт стал
    показывать их с другой стороны. Отказ прямой и с причиной; кому нужна статья
    другого направления — заводит новую, а эту закрывает.
    """
    category = get_category(db, category_id)
    asked = (data.get("direction") or "").strip()
    if asked and asked != category.direction:
        raise errors.ValidationError(
            "A category cannot change its direction — close it and create another",
            code="direction_is_fixed",
        )
    if data.get("name") is not None:
        category.name = _clean_name(data.get("name"))
    if data.get("purpose") is not None:
        category.purpose = _clean_purpose(data.get("purpose"))
    if data.get("note") is not None:
        category.note = (data.get("note") or "").strip()
    db.flush()
    return category


def close_category(db: Session, category_id: int, actor: User) -> FinanceCategory:
    """Закрыть статью: она пропадает из выбора, но прошлое остаётся считаться.

    Мягко всегда, даже когда операций по статье ещё нет. Два пути удаления —
    «настоящее, пока пусто» и «мягкое, когда появилось» — означали бы, что
    результат нажатия одной и той же кнопки зависит от того, успел ли кто-то
    завести операцию секунду назад. Пустая закрытая статья не мешает никому: из
    списка она уходит, а место в таблице стоит дешевле неожиданности.
    """
    category = get_category(db, category_id)
    category.deleted_at = now_utc()
    db.flush()
    # Закрытие статьи — исчезновение раздела отчёта, и спрашивают об этом на
    # следующий день: «почему аренды больше нет в списке».
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=ENTITY_CATEGORY,
        entity_id=category.id,
        entity_label=category.name,
    )
    return category


# --- операции ---


def get_operation(db: Session, operation_id: int) -> FinanceOperation:
    operation = finance_repo.get_operation(db, operation_id)
    if operation is None:
        raise errors.NotFoundError("Operation not found", code="operation_not_found")
    return operation


def list_operations(db: Session, **filters):
    return finance_repo.list_operations(db, **filters)


def _company_id(db: Session, value) -> int | None:
    """Фирма из запроса, с проверкой существования.

    Своей функции в `core/references.py` у фирмы нет, поэтому спрашиваем сервис —
    как это делает `deal_service`. Проверяем, а не доверяем числу: несуществующий
    id доехал бы до вставки и упал нарушением внешнего ключа, то есть пятисоткой
    на обычную опечатку.
    """
    if not value:
        return None
    company_service.get_company(db, int(value))
    return int(value)


def create_operation(db: Session, data: dict, author: User) -> FinanceOperation:
    """Завести операцию. Сумма приходит в терминах статьи, в базу ложится со знаком.

    Отрицательная сумма законна и означает возврат: «вернули аренду за
    неиспользованный месяц» — это минус по расходной статье, а не доход. Знак
    относительный, поэтому и переворачивается тем же выражением, что при чтении.
    """
    category = get_category(db, data.get("category_id") or 0)
    named = parse_amount_minor(data.get("amount"), "amount")

    happened_at = to_utc_naive(data.get("happened_at")) or now_utc()

    operation = finance_repo.add_operation(
        db,
        FinanceOperation(
            category_id=category.id,
            amount_minor=in_terms_of(category.direction, named),
            comment=(data.get("comment") or "").strip(),
            happened_at=happened_at,
            deal_id=references.deal(db, data.get("deal_id")),
            client_id=references.client(db, data.get("client_id")),
            company_id=_company_id(db, data.get("company_id")),
            author_id=author.id,
        ),
    )
    audit_service.record(
        db,
        action=ACTION_OPERATION_ADDED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=ENTITY_OPERATION,
        entity_id=operation.id,
        # Подпись — статья, а не номер: «finance_operation 42» не отвечает на
        # вопрос, о каких деньгах речь.
        entity_label=category.name,
        after=audit_service.money_text(operation.amount_minor),
    )
    return operation


# --- прибыль ---


def profit(db: Session, start: datetime, end: datetime) -> dict:
    """Доход минус расход за период, с разбивкой по статьям.

    Оба числа приходят из базы (`totals` и `by_category`), в Python не считается
    ни одно из них. Разложить УЖЕ посчитанные полсотни строк по признаку
    «налоги / зарплата» здесь можно и нужно: это перебор итогов, а не подсчёт
    денег, и второй запрос ради него означал бы второй способ получить то же
    число — то есть будущее расхождение.

    Сумма разбивки сходится с прибылью по построению: обе стороны берут одни и
    те же операции одного и того же периода.
    """
    summary = finance_repo.totals(db, start, end)
    per_category = finance_repo.by_category(db, start, end)
    categories = finance_repo.categories_by_ids(db, per_category.keys())

    items: list[dict] = []
    by_purpose = {PURPOSE_TAX: 0, PURPOSE_SALARY: 0}
    for category_id, totals in per_category.items():
        category = categories.get(category_id)
        if category is None:
            # Ссылка стоит на RESTRICT, поэтому статьи без строки в справочнике
            # взяться неоткуда. Но врать в отчёте нельзя даже про невозможное:
            # деньги уже посчитаны в `summary`, и молча выбросить их из разбивки
            # значит показать две несходящиеся цифры рядом.
            items.append(
                {
                    "category_id": category_id,
                    "name": "",
                    "direction": None,
                    "purpose": PURPOSE_GENERAL,
                    "amount": totals["total_minor"],
                    "count": totals["count"],
                }
            )
            continue
        named = in_terms_of(category.direction, totals["total_minor"])
        if category.purpose in by_purpose:
            by_purpose[category.purpose] += named
        items.append(
            {
                "category_id": category_id,
                "name": category.name,
                "direction": category.direction,
                "purpose": category.purpose,
                "amount": named,
                "count": totals["count"],
            }
        )

    # Порядок: доходы выше расходов, внутри — от крупного к мелкому. Отчёт
    # читают сверху, и первым должно стоять то, что двигает итог сильнее всего.
    items.sort(key=lambda row: (row["direction"] != DIRECTION_INCOME, -row["amount"]))
    return {
        **summary,
        "items": items,
        # Налоги и зарплата — отдельной строкой: это первое, что спрашивают у
        # расходов, и ради этого признак и живёт в справочнике.
        "taxes": by_purpose[PURPOSE_TAX],
        "salaries": by_purpose[PURPOSE_SALARY],
    }


# --- бюджеты ---


def _period_bounds(
    period_start: date, period_end: date, tz_offset: int
) -> tuple[datetime, datetime]:
    """Границы периода бюджета в том же виде, что у отчётов: [start; end) в UTC.

    Смещение то же самое, что пришло на экран прибыли, и это принципиально:
    посчитай факт по бюджету в UTC, а прибыль — со смещением браузера, и два
    числа на одном экране разойдутся на выручку последнего вечера месяца. Такое
    расхождение объяснить нечем, а верить после него перестают обоим.

    Конец разворачивается в начало СЛЕДУЮЩЕГО дня — по тому же доводу, что и в
    `report_service.parse_period`: 23:59:59 отрезало бы операции, попавшие в
    последнюю долю секунды.
    """
    shift = timedelta(minutes=tz_offset)
    start = datetime.combine(period_start, datetime.min.time()) + shift
    end = datetime.combine(period_end + timedelta(days=1), datetime.min.time()) + shift
    return start, end


def budgets(db: Session, start_day: date, end_day: date, tz_offset: int = 0) -> list[dict]:
    """Планы, задевающие период, и факт по каждому — план против факта.

    **Факт считается по периоду САМОГО бюджета, а не по периоду экрана.** План
    на квартал, открытый на экране августа, обязан показывать квартальный факт:
    иначе «план 300 000, факт 90 000» читается как провал там, где идёт третий
    месяц из трёх.

    Отсюда запрос на каждый РАЗНЫЙ период, а не на каждый бюджет. Периодов на
    экране бывает один-два (месяц и квартал), а планов — по числу статей;
    запрос на строку превратил бы экран в полсотни обращений к базе, и эта
    ошибка в проекте уже разбиралась на остатках склада.
    """
    rows = finance_repo.list_budgets(db, start_day, end_day)
    if not rows:
        return []

    facts: dict[tuple[date, date], dict[int, dict[str, int]]] = {}
    for period in {(row.period_start, row.period_end) for row in rows}:
        start, end = _period_bounds(period[0], period[1], tz_offset)
        facts[period] = finance_repo.by_category(db, start, end)

    categories = finance_repo.categories_by_ids(db, {row.category_id for row in rows})
    result = []
    for row in rows:
        category = categories.get(row.category_id)
        direction = category.direction if category else DIRECTION_INCOME
        fact_raw = facts[(row.period_start, row.period_end)].get(row.category_id)
        fact = in_terms_of(direction, fact_raw["total_minor"]) if fact_raw else 0
        result.append(
            {
                "id": row.id,
                "category_id": row.category_id,
                "category_name": category.name if category else "",
                "direction": direction,
                "purpose": category.purpose if category else PURPOSE_GENERAL,
                "period_start": row.period_start.isoformat(),
                "period_end": row.period_end.isoformat(),
                "planned": row.planned_amount_minor,
                "fact": fact,
                # Остаток плана — здесь, а не в базе: третье число рядом с двумя
                # начало бы расходиться с ними при первой же операции задним
                # числом. Тот же довод, что у остатка к оплате у заявки.
                "left": row.planned_amount_minor - fact,
                "note": row.note,
            }
        )
    return result


def _clean_period(data: dict) -> tuple[date, date]:
    start = _as_date(data.get("period_start"), "period_start")
    end = _as_date(data.get("period_end"), "period_end")
    if end < start:
        raise errors.ValidationError("Period end is before its start", code="bad_period")
    return start, end


def _as_date(value, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        raise errors.ValidationError(
            f"{field} must be a date like 2026-08-01", code="bad_date"
        ) from None


def create_budget(db: Session, data: dict, actor: User) -> FinanceBudget:
    """Завести план по статье на период.

    План положительный всегда, в терминах своей статьи: «потратить не больше
    пятидесяти тысяч» — это 50 000, а не −50 000. Знак принадлежит операции, а
    не намерению, и пускать его сюда значило бы сравнивать план с фактом,
    держа в голове, у кого из них какой знак.
    """
    category = get_category(db, data.get("category_id") or 0)
    period_start, period_end = _clean_period(data)
    planned = parse_amount_minor(data.get("planned"), "planned", allow_negative=False)

    row = FinanceBudget(
        category_id=category.id,
        period_start=period_start,
        period_end=period_end,
        planned_amount_minor=planned,
        note=(data.get("note") or "").strip(),
    )
    # Второй план на тот же период — не «ещё один взгляд», а два ответа на один
    # вопрос. Ограничение стоит в базе, и между проверкой и вставкой есть то же
    # окно, что у номера бланка: две вкладки с одной формой — обычное дело.
    budget = uniqueness.insert_unique(
        db,
        row,
        taken=lambda candidate: finance_repo.budget_for(
            db, candidate.category_id, candidate.period_start, candidate.period_end
        )
        is not None,
        message="A budget for this category and period already exists",
        code="budget_exists",
    )
    _record_budget(db, budget, category.name, actor, before=None)
    return budget


def update_budget(db: Session, budget_id: int, data: dict, actor: User) -> FinanceBudget:
    """Правка плана: только сумма и заметка.

    Статью и период поменять нельзя. План на август по аренде и план на сентябрь
    по рекламе — разные намерения, а не одно исправленное; подменив у строки и
    статью, и период, мы получили бы «изменение», после которого в журнале от
    прежнего плана не осталось бы ничего, кроме номера. Кому нужен другой
    период — удаляет этот план и заводит новый.
    """
    budget = get_budget(db, budget_id)
    category = get_category(db, budget.category_id, include_deleted=True)
    was = budget.planned_amount_minor
    if data.get("planned") is not None:
        budget.planned_amount_minor = parse_amount_minor(
            data.get("planned"), "planned", allow_negative=False
        )
    if data.get("note") is not None:
        budget.note = (data.get("note") or "").strip()
    db.flush()
    if budget.planned_amount_minor != was:
        _record_budget(db, budget, category.name, actor, before=was)
    return budget


def get_budget(db: Session, budget_id: int) -> FinanceBudget:
    budget = finance_repo.get_budget(db, budget_id)
    if budget is None:
        raise errors.NotFoundError("Budget not found", code="budget_not_found")
    return budget


def delete_budget(db: Session, budget_id: int, actor: User) -> None:
    """Удалить план — по-настоящему, а не мягко.

    Бюджет это намерение, а не история денег: на него ничто не ссылается, и
    удаление ничего не переписывает. Мягкое удаление здесь означало бы вечно
    растущую таблицу закрытых планов, из которой к тому же надо было бы
    исключать закрытые в каждом запросе.
    """
    budget = get_budget(db, budget_id)
    category = get_category(db, budget.category_id, include_deleted=True)
    label, budget_id_before_delete = category.name, budget.id
    finance_repo.drop_budget(db, budget)
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=ENTITY_BUDGET,
        entity_id=budget_id_before_delete,
        entity_label=label,
    )


def _record_budget(
    db: Session, budget: FinanceBudget, label: str, actor: User, *, before: int | None
) -> None:
    audit_service.record(
        db,
        action=ACTION_BUDGET_SET,
        actor=actor,
        source=SOURCE_MANUAL,
        entity_type=ENTITY_BUDGET,
        entity_id=budget.id,
        entity_label=label,
        before=None if before is None else audit_service.money_text(before),
        after=audit_service.money_text(budget.planned_amount_minor),
    )
