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
from core.services import audit_service, company_service, modules_service
from core.utils import konets_dnya, now_utc, to_utc_naive
from database.models import (
    Document,
    FinanceBudget,
    FinanceCategory,
    FinanceOperation,
    FinanceRule,
    User,
)
from database.models.audit import SOURCE_MANUAL
from database.models.document import KIND_RETURN, KIND_SALES_ORDER, STATUS_CANCELLED
from database.models.finance import (
    BASE_INCOME_PERCENT,
    BASES,
    CORRECTION_ADJUSTMENT,
    DIRECTION_INCOME,
    DIRECTIONS,
    MAX_AMOUNT_MINOR,
    MAX_RATE_BP,
    ORIGIN_ORDER_CLOSED,
    ORIGIN_PAYMENT,
    PURPOSE_GENERAL,
    PURPOSE_SALARY,
    PURPOSE_TAX,
    PURPOSES,
    RATE_SCALE,
)
from database.repositories import documents as documents_repo
from database.repositories import finance as finance_repo

#: Что записывается в журнал действий.
#:
#: Строки вида «объект.действие» — список действий открытый нарочно (см. шапку
#: `audit_service`): интерфейс, не знающий действия, показывает его ключ как
#: есть. Операция попадает в журнал по той же причине, что и движение склада:
#: это изменение денег, а журнал существует ради вопроса «с чьей руки».
ACTION_OPERATION_ADDED = "finance.operation_added"
ACTION_BUDGET_SET = "finance.budget_set"
#: Приняли деньги (или вернули их). Отдельным действием от «завели операцию»:
#: спрашивают об этом иначе — «когда с нас взяли» и «когда мы отдали», — и
#: искать это в общей куче операций пришлось бы по комментарию.
ACTION_PAYMENT_RECEIVED = "finance.payment_received"
#: Деньги вернули клиенту по возврату: доходная операция с минусом.
ACTION_REFUND_MADE = "finance.refund_made"
#: Правило заведено, поправлено или закрыто. Ставка налога — то, из-за чего
#: через год спорят с бухгалтером; менять её молча нельзя.
ACTION_RULE_CHANGED = "finance.rule_changed"
#: Сумму начисления поправили на месте: «было 80, стало 140, кто и когда».
ACTION_ACCRUAL_ADJUSTED = "finance.accrual_adjusted"

#: Типы объектов для журнала. Свои, а не общий «finance»: вопрос «куда делась
#: статья» и вопрос «кто завёл этот расход» задают порознь, и отбор в журнале
#: должен их различать.
ENTITY_CATEGORY = "finance_category"
ENTITY_OPERATION = "finance_operation"
ENTITY_BUDGET = "finance_budget"
ENTITY_RULE = "finance_rule"

#: Сколько знаков имени статьи помещается в колонку.
MAX_CATEGORY_NAME = 200
#: Столько же у правила — колонка та же ширины.
MAX_RULE_NAME = 200


# --- деньги ---


def parse_amount_minor(
    value,
    field: str = "amount",
    *,
    allow_negative: bool = True,
    allow_zero: bool = False,
) -> int:
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

    `allow_zero` — единственное исключение, и оно про поправку суммы. «Упаковки
    в этот раз не было» — законное новое значение начисления, и поправить 80 на
    0 человек вправе; операции на ноль при этом всё равно не появится, потому что
    в базу ляжет РАЗНИЦА (−80), а разница, равная нулю, отвергается отдельно
    (`nothing_to_adjust`).
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
    if amount == 0 and not allow_zero:
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


def accrue_minor(base_minor: int, rate_bp: int) -> int:
    """Начисление по ставке в базисных пунктах, целыми. Единственное место
    округления во всём блоке.

    **Правило названо вслух: округление к ближайшей минорной единице, ровная
    половина уходит ВВЕРХ ПО МОДУЛЮ, одинаково для прихода и для возврата.**
    Целочисленно, без `float`, без `Decimal`, без `round()`.

    Симметрия здесь не украшение. Несимметричное округление оставляет копейку
    налога на каждом отменённом платеже, и через год «отложено налога» перестаёт
    сходиться с нулём по полностью возвращённым заказам — а объяснить, откуда
    взялись эти копейки, будет уже нечем.

    Множитель 10 000, потому что ставка в сотых долях процента: 5% = 500 bp, и
    12 345 × 500 = 6 172 500; (6 172 500 + 5 000) // 10 000 = 617. Точное
    значение 617,25 — ближе к 617, туда и уходит.

    Выражение то же, что в `document_service.total_minor`, и оттуда же берётся
    второе правило: **округлять на ИТОГЕ, а не на каждой строке**. Здесь это
    значит, что налог считается от суммы платежа целиком, а не построчно по
    заказу — иначе ошибка копилась бы по числу позиций.

    Целочисленное деление `//` разрешено: сторож `test_v_bloke_net_ni_deleniya_
    ni_float` ищет по дереву `ast.Div`, а не `ast.FloorDiv`.
    """
    scaled = base_minor * rate_bp
    half = RATE_SCALE // 2
    if scaled >= 0:
        return (scaled + half) // RATE_SCALE
    return -((-scaled + half) // RATE_SCALE)


def parse_rate_bp(value, field: str = "rate_bp") -> int:
    """Ставка из запроса в базисных пунктах. Только целое.

    Проценты в базисные пункты переводит браузер, целочисленно и на самом краю —
    как копейки в модалке операции. Дробь сюда не доезжает: «5.5» здесь означало
    бы, что кто-то по дороге посчитал ставку через `float`.

    Ноль запрещён: правило со ставкой 0% ничего не начисляет, но выглядит
    работающим — и человек, увидевший его в списке, будет ждать налога, которого
    не будет. Не нужно начислять — выключи правило.
    """
    if isinstance(value, bool) or value is None or value == "":
        raise errors.ValidationError(
            f"{field} must be a whole number of basis points", code="bad_rate"
        )
    try:
        rate = int(value)
    except (TypeError, ValueError):
        raise errors.ValidationError(
            f"{field} must be a whole number of basis points", code="bad_rate"
        ) from None
    if rate <= 0:
        raise errors.ValidationError(f"{field} must be positive", code="bad_rate")
    if rate > MAX_RATE_BP:
        # Начисление больше самого прихода не бывает ни налогом, ни комиссией:
        # это опечатка на два нуля.
        raise errors.ValidationError(f"{field} is above 100%", code="rate_too_large")
    return rate


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

    **Единственный отказ — живое правило, смотрящее на эту статью.** И он здесь
    не ради строгости, а чтобы перенести отказ туда, где человек его понимает.
    Закрой мы статью «Упаковка», на которую смотрит правило, — и следующая
    отгрузка заказа падала бы с непонятной ошибкой посреди работы у стойки.
    Здесь же сказано прямо: сначала закройте правило.
    """
    category = get_category(db, category_id)
    in_use = finance_repo.rules_using_category(db, category.id)
    if in_use:
        raise errors.ValidationError(
            "A live rule points at this category: " + ", ".join(rule.name for rule in in_use),
            code="category_in_use_by_rule",
        )
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


# --- правила начисления ---
#
# Правило отвечает на один вопрос: «что начислить само, когда деньги двинулись».
# Видов начисления сегодня два (`BASES`), и оба живут в одной таблице — разбор,
# почему не две таблицы и не JSON в настройках, в докстроке модели `FinanceRule`.
#
# Инвариант «заполнено ровно то, что осмысленно при этом `base`» держит сервис,
# одной функцией на входе (`_clean_rule`), а не база — так же, как «ровно одна
# основная фирма» и «ровно один основной склад».


def list_rules(db: Session, include_closed: bool = False) -> list[FinanceRule]:
    return finance_repo.list_rules(db, include_closed=include_closed)


def get_rule(db: Session, rule_id: int, include_closed: bool = False) -> FinanceRule:
    rule = finance_repo.get_rule(db, rule_id, include_closed=include_closed)
    if rule is None:
        raise errors.NotFoundError("Rule not found", code="rule_not_found")
    return rule


def _clean_rule(db: Session, data: dict, current: FinanceRule | None = None) -> dict:
    """Разобрать правило целиком и отказать, если оно бессмысленно.

    Проверка стоит здесь, а не в базе, по той же причине, что и «ровно одна
    основная фирма»: описать «при `base = income_percent` заполнена `rate_bp`, а
    при `per_order` — `amount_minor`» можно только CHECK-ограничением, а его
    пришлось бы переписывать миграцией на каждое новое значение ключа. Ключей
    впереди ещё два (эквайринг, привязка к товару).

    Величина при этом ЧИСТИТСЯ, а не игнорируется: правило с процентом и суммой
    одновременно — это не «лишнее поле», а два разных намерения в одной строке, и
    через месяц никто не скажет, какое из них считалось.
    """
    fields: dict = {}
    base = (data.get("base") if data.get("base") is not None else getattr(current, "base", ""))
    base = (base or "").strip()
    if base not in BASES:
        raise errors.ValidationError(f"Unknown rule base: {base or '—'}", code="unknown_base")
    fields["base"] = base

    if data.get("name") is not None or current is None:
        name = (data.get("name") or "").strip()
        if not name:
            raise errors.ValidationError("Name is required", code="name_required")
        fields["name"] = name[:MAX_RULE_NAME]

    # Куда ложится начисленное. Статья обязана быть живой: правило, кладущее
    # деньги в закрытую статью, начисляло бы в раздел, которого нет в отчёте.
    if data.get("category_id") is not None or current is None:
        fields["category_id"] = get_category(db, data.get("category_id") or 0).id

    # С чего считаем. Пусто — со всякого прихода, и это не «не заполнили», а
    # осмысленное значение: налог с оборота обычно один на все виды дохода.
    if "source_category_id" in data:
        source_id = data.get("source_category_id")
        if source_id:
            source = get_category(db, source_id)
            if source.direction != DIRECTION_INCOME:
                raise errors.ValidationError(
                    "A rule can only watch an income category",
                    code="source_must_be_income",
                )
            fields["source_category_id"] = source.id
        else:
            fields["source_category_id"] = None

    if base == BASE_INCOME_PERCENT:
        given = data.get("rate_bp")
        if given is None and current is not None and current.base == base:
            fields["rate_bp"] = current.rate_bp
        else:
            fields["rate_bp"] = parse_rate_bp(given)
        # Сумма при проценте бессмысленна и стирается.
        fields["amount_minor"] = None
    else:
        given = data.get("amount")
        if given is None and current is not None and current.base == base:
            fields["amount_minor"] = current.amount_minor
        else:
            fields["amount_minor"] = parse_amount_minor(
                given, "amount", allow_negative=False
            )
        fields["rate_bp"] = None
        # Вид дохода к фиксированной сумме на заказ отношения не имеет: он
        # отвечает на вопрос «с какого прихода», а прихода здесь нет вовсе.
        fields["source_category_id"] = None

    if data.get("is_active") is not None:
        fields["is_active"] = bool(data.get("is_active"))
    if data.get("sort_order") is not None:
        fields["sort_order"] = int(data.get("sort_order"))
    if data.get("note") is not None:
        fields["note"] = (data.get("note") or "").strip()
    return fields


def create_rule(db: Session, data: dict, actor: User) -> FinanceRule:
    fields = _clean_rule(db, data)
    rule = finance_repo.add_rule(db, FinanceRule(**fields))
    _record_rule(db, rule, actor, before=None)
    return rule


def update_rule(db: Session, rule_id: int, data: dict, actor: User) -> FinanceRule:
    """Правка правила. Прошлые начисления она не трогает — и это главное здесь.

    Ставка и сумма лежат СНИМКОМ на каждой начисленной операции, поэтому смена
    5% на 7% меняет только то, что начислят завтра. Март остаётся мартом, и
    `profit()` за март возвращает то же число, что вчера. Без снимка правка
    переписывала бы прошлые отчёты задним числом — и это была бы не ошибка
    реализации, а неизбежное следствие устройства.
    """
    rule = get_rule(db, rule_id)
    was = _rule_text(rule)
    for field, value in _clean_rule(db, data, rule).items():
        setattr(rule, field, value)
    db.flush()
    _record_rule(db, rule, actor, before=was)
    return rule


def close_rule(db: Session, rule_id: int, actor: User) -> FinanceRule:
    """Закрыть правило: оно перестаёт начислять и уходит из списка.

    Мягко, как статью: на правило ссылаются уже сделанные начисления, и стереть
    строку справочника значит переписать этим прошлое. Ссылка стоит на RESTRICT,
    так что база настоящего удаления и не позволит.

    Отличие от выключателя названо в модели: `is_active = False` — «сейчас не
    считаем», сюда возвращаются одним нажатием; закрытие — «правила больше нет».
    """
    rule = get_rule(db, rule_id)
    rule.deleted_at = now_utc()
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=ENTITY_RULE,
        entity_id=rule.id,
        entity_label=rule.name,
    )
    return rule


def _rule_text(rule: FinanceRule) -> str:
    """Правило одной строкой для журнала: ставка или сумма, целыми.

    Ни валюты, ни процента со знаком: форматирование зависит от локали
    читающего и от настройки валюты, а обе могут смениться (тот же довод, что у
    `audit_service.money_text`).
    """
    value = rule.rate_bp if rule.base == BASE_INCOME_PERCENT else rule.amount_minor
    return f"{rule.base}:{value}:{'on' if rule.is_active else 'off'}"


def _record_rule(db: Session, rule: FinanceRule, actor: User, *, before: str | None) -> None:
    audit_service.record(
        db,
        action=ACTION_RULE_CHANGED,
        actor=actor,
        source=SOURCE_MANUAL,
        entity_type=ENTITY_RULE,
        entity_id=rule.id,
        entity_label=rule.name,
        before=before,
        after=_rule_text(rule),
    )


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


def create_operation(
    db: Session,
    data: dict,
    author: User,
    *,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
    action: str = ACTION_OPERATION_ADDED,
) -> FinanceOperation:
    """Завести операцию. Сумма приходит в терминах статьи, в базу ложится со знаком.

    Отрицательная сумма законна и означает возврат: «вернули аренду за
    неиспользованный месяц» — это минус по расходной статье, а не доход. Знак
    относительный, поэтому и переворачивается тем же выражением, что при чтении.

    **Приход сразу запускает процентные правила.** Это и есть «пришли деньги —
    налог отложился в тот же день»: он не вычисляется в отчёте, а ЗАПИСЫВАЕТСЯ
    операцией той же датой. Поэтому налог за март — это сумма мартовских
    операций, и поехать ей нечем: пересчёта не существует, потому что нечего
    пересчитывать.
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
            # Бланк уже проверен вызывающим: сюда он приходит от `take_payment`,
            # который сам его и нашёл. Обычная операция бланка не знает.
            document_id=data.get("document_id"),
            author_id=author.id,
        ),
    )
    audit_service.record(
        db,
        action=action,
        actor=author,
        source=source,
        source_ref=source_ref,
        entity_type=ENTITY_OPERATION,
        entity_id=operation.id,
        # Подпись — статья, а не номер: «finance_operation 42» не отвечает на
        # вопрос, о каких деньгах речь.
        entity_label=category.name,
        after=audit_service.money_text(operation.amount_minor),
    )
    _apply_income_rules(db, operation, category, author, source=source, source_ref=source_ref)
    return operation


def _apply_income_rules(
    db: Session,
    operation: FinanceOperation,
    category: FinanceCategory,
    author: User,
    *,
    source: str,
    source_ref: str,
) -> list[FinanceOperation]:
    """Начислить процент с прихода: налог с оборота.

    Правила отбираются по статье платежа — «пусто или совпало». На каждое
    подошедшее заводится своя операция по своей статье, той же датой, что у
    платежа, со снимками ставки и базы.

    **Начисленная операция правил не запускает.** Рекурсии нет и без этого —
    налог ложится в расходную статью, а правила смотрят на доходные, — но
    проверка стоит явно: правило, кладущее начисленное в доходную статью,
    законно (возврат переплаченного налога — доход), и без этой строки оно
    закрутило бы само себя.

    **Возврат сторнируется сам.** Отрицательный платёж даёт отрицательное
    начисление, потому что округление симметрично по модулю; отдельной ветки
    «а если возврат» здесь нет и не нужно.
    """
    if operation.rule_id is not None:
        return []
    if category.direction != DIRECTION_INCOME:
        return []

    made: list[FinanceOperation] = []
    for rule in finance_repo.rules_for_income(db, category.id):
        accrued = accrue_minor(operation.amount_minor, rule.rate_bp or 0)
        if accrued == 0:
            # Платёж в одну минорную единицу при ставке 5% даёт ноль. Операции
            # на ноль не заводим: она ничего не описывает, а строку в журнале
            # занимает.
            continue
        made.append(
            _accrue(
                db,
                rule,
                author,
                amount_minor=accrued,
                happened_at=operation.happened_at,
                comment=rule.name,
                # Начислено ПРИХОДОМ денег — и снимается тоже приходом, то есть
                # возвратом платежа. Отмена проведения заказа его не касается,
                # даже когда оба висят на одном бланке.
                origin=ORIGIN_PAYMENT,
                deal_id=operation.deal_id,
                client_id=operation.client_id,
                company_id=operation.company_id,
                document_id=operation.document_id,
                rate_bp=rule.rate_bp,
                base_amount_minor=operation.amount_minor,
                source=source,
                source_ref=source_ref,
            )
        )
    return made


def _accrue(
    db: Session,
    rule: FinanceRule,
    author: User,
    *,
    amount_minor: int,
    happened_at: datetime,
    comment: str,
    origin: str,
    deal_id: int | None = None,
    client_id: int | None = None,
    company_id: int | None = None,
    document_id: int | None = None,
    rate_bp: int | None = None,
    base_amount_minor: int | None = None,
    source: str,
    source_ref: str,
) -> FinanceOperation:
    """Одна операция, порождённая правилом. Единственное место, где они рождаются.

    Сумма приходит В ТЕРМИНАХ СТАТЬИ правила (налог 617 — это «начислено 617»), а
    в базу ложится со знаком, как и всё остальное.

    `origin` — снимок «чем вызвано», и он обязателен: по нему отбирают, что
    отыграть назад при отмене проведения заказа. Разбор, почему снимок, а не
    взгляд на `base` живого правила, — в докстроке колонки
    (`FinanceOperation.origin`).

    Статью берём включая закрытые. Закрыть статью, на которую смотрит живое
    правило, нельзя (`close_category`), так что закрытая здесь взяться может
    только из прямого DELETE в базе. Отказывать в этот момент нечестно: деньги
    уже двинулись, и остановить их записью справочника — значит уронить отгрузку
    из-за чужой правки.
    """
    category = get_category(db, rule.category_id, include_deleted=True)
    stored = in_terms_of(category.direction, amount_minor)
    if abs(stored) > MAX_AMOUNT_MINOR:
        # Правда о заказе, а не сбой: доменный отказ с кодом, а не пятисотка.
        raise errors.ValidationError(
            f"Accrual by rule {rule.name!r} is too large", code="money_too_large"
        )
    operation = finance_repo.add_operation(
        db,
        FinanceOperation(
            category_id=category.id,
            amount_minor=stored,
            comment=comment,
            happened_at=happened_at,
            deal_id=deal_id,
            client_id=client_id,
            company_id=company_id,
            document_id=document_id,
            rule_id=rule.id,
            origin=origin,
            rate_bp=rate_bp,
            base_amount_minor=base_amount_minor,
            author_id=author.id if author else None,
        ),
    )
    audit_service.record(
        db,
        action=ACTION_OPERATION_ADDED,
        actor=author,
        # Источник и «чем именно» едут вниз БЕЗ ИЗМЕНЕНИЙ: иначе автоматика
        # выглядела бы в журнале как «Иванов завёл операцию руками», а
        # разбираться будут именно в разнице между этими двумя.
        source=source,
        source_ref=source_ref,
        entity_type=ENTITY_OPERATION,
        entity_id=operation.id,
        entity_label=category.name,
        after=audit_service.money_text(operation.amount_minor),
    )
    return operation


# --- оплата -------------------------------------------------------------------
#
# **Платёж — это доходная операция, а не своя таблица.** Отдельная таблица
# `payments` была бы вторым местом, где лежат деньги: оборот пришлось бы считать
# двумя запросами и складывать, а «выручка» в системе уже существует в двух
# несвязанных видах — третий добил бы.
#
# У операции для этого уже есть всё нужное: `happened_at` («когда деньги
# двинулись на самом деле», отдельно от `created_at`), связи с заявкой, клиентом
# и фирмой, неизменяемость, исправление обратной операцией и индексы с суммой
# внутри. Добавилась одна ссылка — на бланк.
#
# **Деньги считаются по оплате, а не по отгрузке.** Предоплата ложится в день
# предоплаты, остаток — в день доплаты: это две операции с разными датами, и
# налог начисляется с каждой отдельно, с той суммы, что пришла.


def take_payment(db: Session, data: dict, author: User) -> FinanceOperation:
    """Принять оплату по бланку — или вернуть её.

    Одна ручка на оба действия, и решает знак суммы. Возврат — доходная операция
    с ОТРИЦАТЕЛЬНОЙ суммой по той же статье (модель это прямо разрешает:
    «возврат по расходной статье — это плюс по расходу, а не доход»). Правила
    отрабатывают и на ней: налог получается отрицательным и сторнируется сам,
    потому что округление симметрично по модулю.

    Заявка и клиент подтягиваются от бланка, а не спрашиваются заново: два места
    ввода одного и того же — верный способ получить платёж, привязанный не к той
    карточке.

    **Частичная оплата — это состояние, а не признак.** Ни `paid_at`, ни
    `is_paid`, ни `payments_total` в базе не появляется: получено — это `SUM`, а
    «оплачен» — остаток не больше нуля.
    """
    category = get_category(db, data.get("category_id") or 0)
    if category.direction != DIRECTION_INCOME:
        # Оплата — это приход. Расходная статья здесь означала бы, что кто-то
        # проводит платёж клиенту как выручку, и налог с оборота посчитался бы
        # не с того.
        raise errors.ValidationError(
            "A payment goes to an income category", code="payment_needs_income"
        )

    document = _document(db, data.get("document_id"))
    fields = dict(data)
    if document is not None:
        fields["document_id"] = document.id
        # Названное руками важнее подтянутого: платёж могут привязать к другой
        # заявке осознанно. Но пустое поле заполняется от бланка, а не остаётся
        # пустым — иначе врезка «Деньги по заявке» не увидела бы оплату.
        fields.setdefault("deal_id", None)
        fields.setdefault("client_id", None)
        if not fields.get("deal_id"):
            fields["deal_id"] = document.deal_id
        if not fields.get("client_id"):
            fields["client_id"] = document.client_id
    return create_operation(
        db,
        fields,
        author,
        source=SOURCE_MANUAL,
        source_ref=document.number if document is not None else "",
        action=ACTION_PAYMENT_RECEIVED,
    )


def _document(db: Session, value):
    """Бланк из запроса. Пусто — None; нет такого — 404 с внятной причиной.

    Блок бланков выключен — ссылку не принимаем вовсе: она указывала бы на
    раздел, которого у этого бизнеса нет, и «получено по заказу» показывать было
    бы негде. Платёж при этом заводится обычной операцией по заявке.
    """
    if not value:
        return None
    if not modules_service.is_enabled(db, "documents"):
        raise errors.ValidationError(
            "Documents are switched off — a payment cannot name a form",
            code="module_disabled",
        )
    from core.services import document_service

    return document_service.get(db, int(value))


# --- начисления по заказу -----------------------------------------------------
#
# Зовут отсюда подписчики событий заказа (`core/subscriptions.py`), а не сам
# заказ: `order_service` про финансы не знает и знать не должен — заказы обязаны
# работать при выключенном блоке денег.


def accrue_order_costs(
    db: Session,
    order,
    lines: list,
    actor: User,
    *,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> list[FinanceOperation]:
    """Отгруженный заказ списывает стандартные расходы: упаковка, доставка.

    Фиксированные суммы на заказ, по одной операции на правило, датой закрытия.
    Правил нет, все выключены или все закрыты — не делается ничего, и это не
    ошибка: у бизнеса, не заведшего ни одного правила, стандартных расходов не
    существует как явления.

    **Только заказ ПОКУПАТЕЛЮ.** Упаковка и доставка — расходы на отгрузку, и
    при приёмке от поставщика их не бывает: коробку и курьера оплачивает он.
    Событие при этом одно на оба вида заказа, и это правильно — складу интересны
    оба; отбирает здесь тот, кто знает, что такое стандартный расход.

    Позиции заказа сюда приезжают, но пока не участвуют: правила вида
    `per_line`/`per_product` (привязка к товару) — следующая задача, и когда она
    придёт, менять придётся только это тело.
    """
    if order.kind != KIND_SALES_ORDER:
        return []

    made: list[FinanceOperation] = []
    for rule in finance_repo.rules_per_order(db):
        amount = rule.amount_minor or 0
        if amount == 0:
            continue
        made.append(
            _accrue(
                db,
                rule,
                actor,
                amount_minor=amount,
                happened_at=now_utc(),
                comment=f"{rule.name} for order {order.number}",
                # Порождено ЗАКРЫТИЕМ ЗАКАЗА: отметка различает упаковку от
                # налога с платежа во врезке денег и в поправках.
                origin=ORIGIN_ORDER_CLOSED,
                deal_id=order.deal_id,
                client_id=order.client_id,
                document_id=order.id,
                source=source,
                source_ref=source_ref,
            )
        )
    return made


def refund_for_return(
    db: Session,
    vozvrat,
    order,
    category_id,
    actor: User,
    *,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> FinanceOperation | None:
    """Проведённый возврат отдаёт деньги клиенту — минусом по доходной статье.

    Тот же путь, что у возврата оплаты руками (`take_payment` с отрицательной
    суммой): налог с оборота сторнируется сам, правилом, ровно один раз.
    Начисления закрытия заказа (упаковка, доставка) НЕ снимаются: коробку и
    курьера оплатили, и возврат товара этих денег не возвращает.

    Операция висит на ВОЗВРАТЕ, а не на заказе: на заказе она уменьшила бы
    «получено», и карточка показала бы долг клиента там, где клиенту только
    что вернули деньги.

    Статью называет человек при проведении: возврат без статьи при включённых
    деньгах — отказ, а не молчаливая операция «куда-нибудь».
    """
    summa = vozvrat.refund_minor or 0
    if summa == 0:
        return None
    if not category_id:
        raise errors.ValidationError(
            "Pick an income category for the refund", code="refund_needs_category"
        )
    category = get_category(db, category_id)
    if category.direction != DIRECTION_INCOME:
        raise errors.ValidationError(
            "A refund goes back through an income category", code="refund_needs_income"
        )
    return create_operation(
        db,
        {
            "category_id": category.id,
            "amount": -summa,
            "comment": f"refund by return {vozvrat.number} for order {order.number}",
            "document_id": vozvrat.id,
            "deal_id": vozvrat.deal_id,
            "client_id": vozvrat.client_id,
        },
        actor,
        source=source,
        source_ref=source_ref,
        action=ACTION_REFUND_MADE,
    )


def adjust_accrual(
    db: Session, operation_id: int, new_amount, author: User
) -> FinanceOperation:
    """Поправить сумму начисления НА МЕСТЕ: было 80, стало 140.

    `new_amount` — новая ИТОГОВАЯ сумма в терминах статьи, а не разница. Разницу
    считает сервер: вводить её руками — верный способ ошибиться на знак.

    Исходная операция не правится и не удаляется. Заводится корректирующая — на
    разницу, той же датой, что у исходной: поправка уточняет прошлое событие, а
    не заводит новое, и месяц закрытия заказа не должен разъезжаться сам собой.

    Экран показывает верную цифру не потому, что кто-то переписал число, а
    потому что он СУММИРУЕТ цепочку (`accrual_totals`). Третья правка (140 → 200)
    добавит ещё одну строку, и сумма снова будет верна без единой строки на
    уборку. Журнал операций при этом показывает ОБЕ строки: молча слить их значило
    бы спрятать факт правки от того, кто сводит кассу.

    Правило не трогается: следующий заказ снова возьмёт 80 — это ровно то, что
    просили («иногда упаковка стоит дороже»).
    """
    head = get_operation(db, operation_id)
    if head.rule_id is None or head.correction is not None:
        # Поправляют начисление, а не платёж и не другую поправку. Платёж
        # исправляют возвратом — это разговор с кассой, а не с калькулятором;
        # поправку поправляют, меняя итог у её головы.
        raise errors.ValidationError(
            "Only an accrual can be adjusted", code="not_an_accrual"
        )
    if finance_repo.reversal_of(db, head.id) is not None:
        raise errors.ValidationError(
            "This accrual has already been reversed", code="accrual_reverted"
        )

    category = get_category(db, head.category_id, include_deleted=True)
    was_named = in_terms_of(category.direction, finance_repo.accrual_total(db, head.id))
    now_named = parse_amount_minor(new_amount, "amount", allow_zero=True)
    difference = now_named - was_named
    if difference == 0:
        raise errors.ValidationError(
            "The accrual is already this amount", code="nothing_to_adjust"
        )

    operation = finance_repo.add_operation(
        db,
        FinanceOperation(
            category_id=category.id,
            amount_minor=in_terms_of(category.direction, difference),
            comment=f"adjustment: {head.comment or category.name}",
            happened_at=head.happened_at,
            deal_id=head.deal_id,
            client_id=head.client_id,
            company_id=head.company_id,
            document_id=head.document_id,
            rule_id=head.rule_id,
            # «Чем вызвано» — от головы: поправка уточняет её событие, а не
            # заводит своё. Иначе отмена заказа не нашла бы половину цепочки.
            origin=head.origin,
            # Ставки и базы у поправки нет: она ничего не считала, её назвал
            # человек. Ноль здесь соврал бы «посчитано по нулевой ставке».
            rate_bp=None,
            base_amount_minor=None,
            corrects_id=head.id,
            correction=CORRECTION_ADJUSTMENT,
            author_id=author.id,
        ),
    )
    audit_service.record(
        db,
        action=ACTION_ACCRUAL_ADJUSTED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=ENTITY_OPERATION,
        # Запись про ИСХОДНОЕ начисление, а не про поправку: спрашивают «почему
        # тут 140, а в правиле 80», и отвечать должна история той строки.
        entity_id=head.id,
        entity_label=category.name,
        before=audit_service.money_text(was_named),
        after=audit_service.money_text(now_named),
    )
    return operation


def money_of_document(db: Session, document_id: int) -> dict:
    """Деньги по бланку: получено, остаток, начисления. Ни одно число не хранится.

    - получено = `SUM` доходных операций с этим бланком;
    - сумма бланка = `document_service.total_minor` по его строкам;
    - остаток = сумма минус получено;
    - «оплачен» = остаток не больше нуля;
    - начисления = цепочки поправок, сложенные по головам.

    Начисления показываются ВСЕ, чем бы ни были вызваны: и упаковка от закрытия
    заказа, и налог от платежа. Отбор по `origin` нужен отмене проведения, а не
    врезке: человек, открывший заказ, спрашивает «сколько на нём висит денег», а
    не «что из этого отыграется, если отменить».

    Колонок `documents.paid_total`, `paid_at` и `is_paid` нет и не появится: все
    четыре числа рождаются заново на каждый запрос, и разойтись им не с чем.

    **Пустой остаток означает две разные вещи, и различает их `status`.**

    Первая — блок бланков выключен: суммы нет, сравнивать не с чем. Это не
    половина ответа, а правда о системе без бланков; `status` тогда тоже пуст.

    Вторая — бланк отменён. У отменённого «сколько ещё с человека взять» не
    равно нулю и не равно сумме: **вопрос не задан**. Раньше здесь честно
    считалось `сумма минус полученное`, и у отменённого заказа с возвращёнными
    деньгами получалась полная сумма заказа — то есть врезка показывала долг,
    которого нет. Ноль на его месте был бы враньём той же природы, только
    обратным: он читается как «рассчитались», а с отменённым не рассчитывались.

    То же и с `paid`: у отменённого бланка нет ни «оплачен», ни «не оплачен».
    Что там на самом деле с деньгами, говорит `received` — и его отменой не
    трогают: возврат денег и отмена бумаги это разные действия, и сделать можно
    только одно из двух.
    """
    received = finance_repo.received_of(db, document_id=document_id)
    heads = finance_repo.accruals_of_document(db, document_id)
    totals = finance_repo.accrual_totals(db, [row.id for row in heads])
    reverted = finance_repo.reverted_heads(db, [row.id for row in heads])
    categories = finance_repo.categories_by_ids(db, {row.category_id for row in heads})

    accruals = []
    for head in heads:
        category = categories.get(head.category_id)
        direction = category.direction if category else None
        stored = totals.get(head.id, 0)
        accruals.append(
            {
                "id": head.id,
                "rule_id": head.rule_id,
                "category_id": head.category_id,
                "category_name": category.name if category else "",
                "direction": direction,
                "purpose": category.purpose if category else PURPOSE_GENERAL,
                "comment": head.comment,
                "amount": in_terms_of(direction, stored) if direction else stored,
                "rate_bp": head.rate_bp,
                "base_amount": head.base_amount_minor,
                # Чем вызвано: «списано при отгрузке» или «отложено с платежа».
                # Снимаются они разными действиями — отменой проведения и
                # возвратом денег, — и различить их по имени статьи нельзя.
                "origin": head.origin,
                "happened_at": head.happened_at.isoformat() if head.happened_at else None,
                # Отменённое начисление остаётся в списке нулём, а не исчезает:
                # исчезнув, оно унесло бы с собой ответ на вопрос «а куда делась
                # упаковка».
                "reverted": head.id in reverted,
            }
        )

    bumaga = _document_total(db, document_id)
    total, document = (None, None) if bumaga is None else bumaga
    status = document.status if document is not None else None
    otmenen = status == STATUS_CANCELLED
    schitaem = total is not None and not otmenen
    # Возвращённое — отдельным числом, «получено» не трогает (docs/22 §5). У
    # возврата бумага — это сумма к возврату, а операции по нему отрицательные:
    # без этой ветки карточка возврата показывала бы долг клиента вдвое.
    refunded = 0
    if document is not None and document.kind == KIND_RETURN:
        total = int(document.refund_minor or 0)
        refunded = -received
        return {
            "document_id": document_id,
            "total": total,
            "status": status,
            "received": received,
            "refunded": refunded,
            "due": (total - refunded) if schitaem else None,
            "paid": (total - refunded <= 0) if schitaem else None,
            "accruals": accruals,
        }
    if document is not None:
        vozvraty = [v.id for v in documents_repo.vozvraty_po_zakazu(db, document.id)]
        if vozvraty:
            refunded = -finance_repo.received_of(db, document_ids=vozvraty)
    return {
        "document_id": document_id,
        "total": total,
        # Состояние бланка — чтобы различить два «остатка нет»: бланков нет
        # вовсе и бланк отменён. Разбор — в докстроке.
        "status": status,
        "received": received,
        "refunded": refunded,
        "due": (total - received) if schitaem else None,
        "paid": (total - received <= 0) if schitaem else None,
        "accruals": accruals,
    }


def money_of_deal(db: Session, deal_id: int) -> dict:
    """Сколько получено по заявке. Одно число, и оно тоже не хранится.

    Отдельно от `money_of_document`, потому что вопрос другой: у заявки бывает
    несколько бланков, а бывает ни одного (деньги приняли до того, как выписали
    бумагу). Сумма бланка здесь поэтому не считается — сравнивать не с чем.

    `deals.prepaid` эта функция НЕ трогает и не заменяет. Колонка остаётся: при
    выключенном блоке денег она единственное место, где вообще видно «сколько
    получено», а снять её значит разрушительная миграция и перенос старых
    значений в операции — датой, которой у них нет. Пока двух мест ввода не
    возникает за счёт интерфейса: при включённых финансах поле только для чтения.
    """
    return {"deal_id": deal_id, "received": finance_repo.received_of(db, deal_id=deal_id)}


def _document_total(db: Session, document_id: int) -> tuple[int, Document] | None:
    """Сумма бланка по его строкам и сама бумага. None — бланки выключены.

    Сумму считает `document_service.total_minor` — та самая единственная точка,
    где по строкам считаются деньги. Своего счёта здесь нет намеренно: второй
    означал бы два места, где округляют, и они разошлись бы на первой же скидке.

    Состояние возвращается вместе с суммой, а не отдельным вызовом: строка
    бланка уже прочитана, и второе обращение за ней означало бы два запроса за
    одним и тем же ради одного поля.
    """
    if not modules_service.is_enabled(db, "documents"):
        return None
    from core.services import document_service

    document = document_service.get(db, document_id)
    return document_service.total_minor(document_service.lines(db, document.id)), document


# --- чем меряем выручку -------------------------------------------------------
#
# Решение владельца от 19.08.2026: **банк один**. Деньги лежат в одном месте, с
# него списывают, туда добавляют — и источник правды о деньгах один, финансовые
# операции. Заявки и заказы деньги ПОРОЖДАЮТ, но сами счётом не являются.
#
# До этого счётов было два и они не сходились: заказ на 12 000 оплачен, касса
# полна, а главная показывала пустой месяц — потому что отчёт считал выигранные
# заявки, а заказ к заявке привязан не был. Владелец видит пустой месяц при
# полной кассе, перестаёт верить системе и заводит таблицу рядом.
#
# Ответ «по чему считаем» живёт ЗДЕСЬ и только здесь. Напиши его в отчётах, на
# главной и в утренней сводке по отдельности — через месяц они разойдутся, и
# вместо двух счётов станет три.

#: Считаем по кассе: сколько денег пришло.
BAZIS_KASSA = "cash"
#: Считаем по выигранным заявкам: кассы в системе нет.
BAZIS_ZAYAVKI = "deals"


def bazis_vyruchki(db: Session) -> str:
    """Чем меряется выручка при нынешних настройках.

    Выключенный блок «Финансы» означает, что операций не существует вовсе —
    считать нечего, и отчёт честно возвращается к сумме выигранных заявок. Это
    не «две формулы на выбор», а единственный ответ в каждом из двух случаев;
    важно лишь, чтобы экран назвал, какой из них сейчас, — молчаливая подмена
    одного счёта другим и есть та беда, с которой всё началось.
    """
    return BAZIS_KASSA if modules_service.is_enabled(db, "finance") else BAZIS_ZAYAVKI


def postupleniya_po_mesyatsam(
    db: Session,
    buckets: list[tuple[datetime, datetime]],
    only_manager_id: int | None = None,
) -> dict[int, dict[str, int]] | None:
    """Поступления в кассу по месяцам. `None` — кассы в системе нет.

    Отдельная обёртка над запросом, а не прямой вызов репозитория из отчётов:
    гейт по блоку обязан стоять в одном месте с ответом «чем меряем», иначе
    отчёт однажды спросит кассу при выключенном блоке и получит честный ноль —
    который прочитают как «денег не было».
    """
    if bazis_vyruchki(db) != BAZIS_KASSA:
        return None
    return finance_repo.postupleniya_po_mesyatsam(db, buckets, only_manager_id)


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
    end = konets_dnya(period_end, shift)
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
