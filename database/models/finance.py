"""Финансы: статьи, операции, бюджеты.

**Прибыль не хранится**: она равна `SUM` операций за период, а хранимый итог
разойдётся с историей (откат, операция задним числом), и какая из двух цифр
верна — узнать неоткуда. То же про факт по бюджету и разбивку по статьям.

**Знак живёт на операции, направление — на статье.** Доход плюсом, расход
минусом: прибыль это `SUM(amount_minor)` без единого `CASE`. «Сколько
потрачено» — по направлению СТАТЬИ: возврат аренды по знаку уехал бы в выручку.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import text_default

#: Направление статьи. Ключи стабильные — уходят в базу и в фильтры, подписи
#: переводятся на фронтенде.
DIRECTION_INCOME = "income"
DIRECTION_EXPENSE = "expense"
DIRECTIONS = (DIRECTION_INCOME, DIRECTION_EXPENSE)

#: Особый род статьи: налоги и зарплата.
#:
#: Одним полем с тремя значениями: два флажка разрешают «и налог, и зарплата»,
#: и отчёт «налоги / зарплата / прочее» посчитал бы такую статью дважды.
PURPOSE_GENERAL = "general"
PURPOSE_TAX = "tax"
PURPOSE_SALARY = "salary"
PURPOSES = (PURPOSE_GENERAL, PURPOSE_TAX, PURPOSE_SALARY)

#: Потолок одной суммы в минорных единицах — двадцать миллионов в валюте счёта.
#:
#: Это то, что помещается в колонку: `Integer` в MySQL — INT, 4 байта, то есть
#: 2 147 483 647 минорных единиц. Итог не ограничен — `SUM` сам расширяется до
#: BIGINT; понадобится больше на строку — BigInteger добавочной миграцией.
MAX_AMOUNT_MINOR = 2_000_000_000

#: От чего считается начисление. Закрытый набор ключей, как `DIRECTIONS`.
#:
#: Два флажка разрешили бы «и процент с прихода, и сумма на заказ», и отчёт по
#: виду начисления посчитал бы правило дважды. Процент с прихода: заполнен
#: `rate_bp`, `source_category_id` — по желанию.
BASE_INCOME_PERCENT = "income_percent"
#: Фиксированная сумма на закрытый заказ: упаковка, доставка. Заполнен
#: `amount_minor`.
BASE_PER_ORDER = "per_order"
BASES = (BASE_INCOME_PERCENT, BASE_PER_ORDER)

#: Ставка в БАЗИСНЫХ ПУНКТАХ — сотых долях процента: 5% = 500, эквайринг = 140.
#:
#: Единица в имени — по образцу `price_minor`: дробный процент существует, а
#: `float` запрещён. Потолок ровно 100%: начисление больше прихода — не налог и
#: не комиссия, а опечатка на два нуля, отвечать на неё надо отказом.
MAX_RATE_BP = 10_000

#: Множитель ставки: 100 (процент) × 100 (сотые доли) = 10 000.
RATE_SCALE = 10_000

#: Чем операция уточняет другую: `reversal` — отмена проведения зеркальной
#: операцией, `adjustment` — поправка суммы на месте (упаковка вышла дороже).
#:
#: Одним полем: «и отмена, и поправка» — состояние, которого не бывает.
CORRECTION_REVERSAL = "reversal"
CORRECTION_ADJUSTMENT = "adjustment"
CORRECTIONS = (CORRECTION_REVERSAL, CORRECTION_ADJUSTMENT)

#: Чем вызвано начисление. Пусто у платежей и у всего, что завёл человек руками.
#:
#: `order_closed` снимается ОТМЕНОЙ ПРОВЕДЕНИЯ, `payment` (налог с оборота) —
#: ВОЗВРАТОМ ПЛАТЕЖА, и ничем другим. Набор закрытый: новый вид начисления
#: добавляет сюда значение, а не колонку.
ORIGIN_ORDER_CLOSED = "order_closed"
ORIGIN_PAYMENT = "payment"
ORIGINS = (ORIGIN_ORDER_CLOSED, ORIGIN_PAYMENT)


class FinanceRule(Base):
    """Правило начисления: налог с прихода, стандартный расход на заказ.

    Одна таблица, а не `tax_rates` + `fixed_costs`: эквайринг не то и не другое,
    и под него понадобилась бы третья. Вид — один ключ `base`, следующий
    добавляется значением; какая из двух величин заполнена, держит сервис.

    Не JSON в `site_settings`: нужны `WHERE`/`JOIN`, на правило ссылается
    начисление, `settings_repo.write` молча гасит чужую правку, а `schema_check`
    полей внутри JSON не видит вовсе.

    `is_active = False` — «сейчас не считаем», `deleted_at` — «правила больше
    нет», мягко: на него ссылаются начисления. Уникальности имени нет — как у
    статей, частичного индекса в MySQL не существует.
    """

    __tablename__ = "finance_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    #: `income_percent` | `per_order` — см. BASES.
    base: Mapped[str] = mapped_column(String(24))
    # RESTRICT: снести статью вместе с правилом значит бесшумно переписать то,
    # куда ложились деньги. Штатный путь — закрытие статьи, и оно отказывает,
    # пока правило живо (`finance_service.close_category`).
    category_id: Mapped[int] = mapped_column(
        ForeignKey("finance_categories.id", ondelete="RESTRICT"), index=True
    )
    #: С какого вида дохода считаем. Пусто — со всякого прихода.
    source_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Ставка в базисных пунктах. Пусто у правил с фиксированной суммой.
    rate_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Фиксированная сумма в минорных единицах. Пусто у процентных правил.
    #:
    #: Имя с «amount» намеренно: ревизор схемы (`tests/test_schema_conventions`)
    #: ищет денежные колонки по имени и не даст сделать её дробной.
    amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Отдельного тумблера «считать ли налог» в настройках не нужно: выключенное
    #: правило и есть выключенный расчёт.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    #: Порядок применения, шаг 10 — как у работ на доске: вставить между двумя
    #: соседями можно, не переписывая всех.
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    note: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        # Правила читают ровно одним способом: живые, в порядке применения.
        # Отдельный индекс по `is_active` отдал бы их в произвольном порядке.
        Index("ix_finance_rules_active_sort", "is_active", "sort_order"),
    )


class FinanceCategory(Base):
    """Статья доходов или расходов: «Выручка», «Аренда», «Зарплата», «Налоги».

    Единственная ось, по которой раскладывается прибыль, поэтому обязательна у
    каждой операции: операция без статьи попала бы в итог, но выпала из разбивки,
    и сумма строк разошлась бы с числом сверху.

    Удаление мягкое: на статью ссылаются операции и бюджеты. Уникальности имени
    нет — обычный `UNIQUE` запретил бы завести «Аренду» заново после закрытия, а
    «уникально среди незакрытых» — частичный индекс, которого в MySQL нет.
    """

    __tablename__ = "finance_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    #: `income` | `expense` — см. DIRECTIONS.
    direction: Mapped[str] = mapped_column(String(16))
    #: `general` | `tax` | `salary` — см. PURPOSES.
    purpose: Mapped[str] = mapped_column(
        String(16), default=PURPOSE_GENERAL, server_default=PURPOSE_GENERAL
    )
    note: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    #: Мягкое удаление: закрытая статья пропадает из выбора, но её прошлые
    #: операции остаются и продолжают считаться в прибыли за прошлые периоды.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        # Справочник читают одним способом: доходные, потом расходные, внутри по
        # алфавиту. Составной индекс отдаёт список уже разложенным.
        Index("ix_finance_categories_direction_name", "direction", "name"),
    )


class FinanceOperation(Base):
    """Одна операция: пришли деньги или ушли. Складывая их, получаем прибыль.

    Операция не правится и не удаляется — ошибку исправляют обратной. Иначе
    прибыль за прошлый квартал зависела бы от того, когда её спросили. Поэтому
    `finance.edit` и `finance.delete` не объявлены вовсе (`core/permissions.py`).

    Связи с заявкой, клиентом и фирмой необязательны: «Аренда за август» ни к
    какой заявке не относится, а выдуманная связь хуже отсутствующей — по ней
    потом строят отчёт.
    """

    __tablename__ = "finance_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: удалить статью вместе с операциями значит бесшумно переписать
    # прибыль за прошлые периоды. Штатный путь — закрытие статьи (`deleted_at`).
    category_id: Mapped[int] = mapped_column(
        ForeignKey("finance_categories.id", ondelete="RESTRICT")
    )
    # Знаковое: доход +, расход −. Прибыль = SUM(amount_minor), поэтому вычитать
    # при чтении ничего не нужно и перепутать знак негде.
    amount_minor: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Когда деньги двинулись на самом деле, отдельно от `created_at`: заполняют
    # пачкой в конце недели, а период отчёта считается по первой дате.
    happened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # SET NULL у всех трёх связей: удаление карточки не отменяет того, что деньги
    # получены. CASCADE менял бы прибыль за прошлый месяц сам собой.
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- чем вызвана и по чему посчитана -------------------------------------
    #
    # Снимковые колонки: сумму правила правят НА МЕСТЕ (в справочнике 80 стало
    # 140), поэтому ссылка после правки покажет новую цифру, а начислено было по
    # старой. На «почему тут 5%, а там 7%» отвечает операция, а не справочник.

    # RESTRICT: снести правило вместе с историей начислений значит переписать
    # прошлое. Штатный путь — закрыть правило (`deleted_at`), как статью.
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_rules.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Чем вызвано начисление (см. ORIGINS); пусто у заведённого руками.
    #:
    #: По нему отбирается то, что отыграется при отмене проведения заказа.
    #: Снимок, а не взгляд на `base` живого правила: правило правят на месте, и
    #: отбор по справочнику после правки молча перестал бы находить своё.
    origin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: Снимок ставки на момент начисления, в базисных пунктах. Пусто у платежей
    #: (их ничто не порождало) и у поправок (они ничего не считали).
    rate_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: С какой суммы считали. Обратно из суммы и ставки не выводится: округление
    #: необратимо, 617 могло получиться из целого диапазона баз.
    base_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # SET NULL: удаление бумаги не отменяет того, что деньги получены. Без ссылки
    # отгрузку заказа нельзя ни назвать, ни отменить точно.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    #: Какую операцию эта уточняет. SET NULL — по той же причине, что и везде:
    #: цепочку правок разорвать можно, деньги отменить нельзя.
    corrects_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_operations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: `reversal` | `adjustment` — см. CORRECTIONS. Пусто у обычной операции.
    correction: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        # Разбивка прибыли по статьям — `SUM(amount_minor) GROUP BY category_id`
        # за период, и сумма обязана лежать в самом индексе: иначе за каждым
        # числом база лезет в таблицу — сто тысяч чтений на строку отчёта.
        Index(
            "ix_finance_ops_category_happened",
            "category_id",
            "happened_at",
            "amount_minor",
        ),
        # Прибыль целиком и журнал за период — тот же довод без статьи: отбор по
        # дате, сумма и здесь читается прямо из индекса.
        Index("ix_finance_ops_happened_amount", "happened_at", "amount_minor"),
        # «Получено по заказу» — `SUM` по бланку со статьёй; врезка «Деньги»
        # открывается на каждый заказ, сумма опять обязана лежать в индексе.
        Index("ix_finance_ops_document", "document_id", "category_id", "amount_minor"),
    )


class FinanceBudget(Base):
    """План по статье на период. Факт рядом с ним НЕ хранится — он считается.

    Вторая колонка «сколько вышло» ломает то же, что хранимый остаток: операция
    задним числом её не поправит, и человек увидит «план 50 000, факт 30 000»
    там, где по операциям выходит 44 000.

    План всегда положительный, в терминах своей статьи: «50 000» у расходной —
    «потратить не больше». Период — пара дат: месяц закрывает девять случаев из
    десяти, а десятый (квартал, «до конца проекта») им не описывается вовсе.
    """

    __tablename__ = "finance_budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT по той же причине, что у операции: бюджет — это история решений,
    # и удаление статьи не должно уносить её молча.
    category_id: Mapped[int] = mapped_column(
        ForeignKey("finance_categories.id", ondelete="RESTRICT")
    )
    # Границы включительные обе. Полуоткрытый интервал правильнее для моментов
    # времени (`report_service.parse_period`), но здесь это ДАТЫ из календаря:
    # показать 1 сентября концом августа значит сбивать с толку каждый раз.
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    # Имя с «amount» намеренно: ревизор схемы (`tests/test_schema_conventions`)
    # ищет денежные колонки по имени; названная `planned` прошла бы мимо молча.
    planned_amount_minor: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Тот редкий инвариант «ровно один», который держит СУБД: мягкого
        # удаления у бюджета нет, а значит и закрытых строк, мешающих обычному
        # UNIQUE. Второй план на тот же август — два ответа на один вопрос.
        UniqueConstraint(
            "category_id", "period_start", "period_end", name="uq_finance_budget_period"
        ),
        # Экран бюджетов открывают периодом («покажи август»), а не статьёй.
        Index("ix_finance_budgets_period", "period_start", "period_end"),
    )
