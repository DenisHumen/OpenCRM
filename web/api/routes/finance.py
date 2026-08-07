"""Финансы: операции, статьи, бюджеты, прибыль.

Роутер закрыт `require_module("finance")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отдавать деньги
фирмы тому, кто помнит адрес.

Ни один обработчик здесь не складывает операции сам — прибыль и факт по бюджету
приходят из агрегатов репозитория (`database/repositories/finance.py`).

Прав у блока три, и `view_amounts` среди них нет намеренно. У склада и заявок
суммы отделены от раздела, потому что «ведёт заявку, но не видит маржу» —
осмысленная просьба. Здесь она бессмысленна: финансы это деньги целиком, и
раздел без сумм — пустая таблица. Тот же довод, по которому отчёт о выручке
закрыт правом на суммы целиком (`routes/reports.py`).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import finance_service, report_service, settings_service
from database.models import User
from database.models.finance import DIRECTIONS
from database.repositories import finance as finance_repo
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/finance",
    tags=["finance"],
    dependencies=[Depends(require_module("finance"))],
)


# --- тела запросов ---
#
# Схемы объявлены здесь, а не в общем `web/api/schemas.py`. Там живут схемы,
# которые нужны нескольким роутерам и сериализаторам сразу; эти три не нужны
# никому, кроме этого файла, и вынос их в общий модуль на девятьсот строк
# добавил бы блоку связей ровно там, где он обязан выключаться без следа.


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    #: `income` | `expense`. При правке не принимается — разбор в сервисе.
    direction: str | None = None
    purpose: str | None = None
    note: str | None = None


class OperationIn(BaseModel):
    category_id: int
    #: Сумма в минорных единицах, в терминах статьи: у расходной «50 000»
    #: значит «потратили». Отрицательная — возврат.
    amount: int
    happened_at: datetime | None = None
    comment: str | None = None
    deal_id: int | None = None
    client_id: int | None = None
    company_id: int | None = None


class BudgetIn(BaseModel):
    category_id: int
    period_start: str
    period_end: str
    #: План в минорных единицах, всегда положительный.
    planned: int
    note: str | None = None


class BudgetPatchIn(BaseModel):
    """Правка плана: только сумма и заметка.

    Ни статьи, ни периода: план на август по аренде и план на сентябрь по
    рекламе — разные намерения, а не одно исправленное (разбор в
    `finance_service.update_budget`). Полей просто нет, чтобы отказ не
    приходилось объяснять на каждое.
    """

    planned: int | None = None
    note: str | None = None


class Period:
    """Период отчёта — общими параметрами для всех маршрутов сразу.

    Разбирает его `report_service.parse_period`, а не своя копия: правило «конец
    периода разворачивается в начало следующего дня» и поправка на зону браузера
    одни на всю систему. Второй экземпляр этого правила разошёлся бы с первым, и
    «прибыль за август» в финансах перестала бы совпадать с выручкой за август в
    отчётах — при одинаковых датах на обоих экранах.
    """

    def __init__(
        self,
        date_from: str | None = Query(default=None, alias="from"),
        date_to: str | None = Query(default=None, alias="to"),
        # Минуты из Date#getTimezoneOffset(): сколько прибавить к местному
        # времени, чтобы получить UTC. Для Киева летом это -180.
        tz_offset: int = Query(default=0, ge=-840, le=840),
    ):
        self.start, self.end, self.start_day, self.end_day = report_service.parse_period(
            date_from, date_to, tz_offset
        )
        self.tz_offset = tz_offset

    def envelope(self, db: Session) -> dict:
        """Период и валюта в каждом ответе: набор чисел без подписи периода
        потом невозможно соотнести ни с чем."""
        return {
            "from": self.start_day.isoformat(),
            "to": self.end_day.isoformat(),
            "currency": settings_service.get_all(db).get("currency", "USD"),
        }


# --- сериализация ---


def _category_out(category) -> dict:
    return {
        "id": category.id,
        "name": category.name,
        "direction": category.direction,
        "purpose": category.purpose,
        "note": category.note,
        "closed": category.deleted_at is not None,
    }


def _operation_out(operation, category, author_name: str | None) -> dict:
    """Строка журнала. Сумма отдаётся В ТЕРМИНАХ СТАТЬИ, а не как лежит в базе.

    В базе расход хранится минусом, чтобы прибыль была простым `SUM`. Отдать
    наружу тот же минус значило бы заставить каждый экран помнить про это
    соглашение и переворачивать знак самому — то есть завести правило в
    стольких экземплярах, сколько будет читателей API.

    Направление при этом едет отдельным полем, и по нему видно, доход это или
    расход. Отрицательная сумма у расходной статьи означает ровно одно —
    возврат.
    """
    direction = category.direction if category else None
    return {
        "id": operation.id,
        "category_id": operation.category_id,
        "category_name": category.name if category else "",
        "direction": direction,
        "purpose": category.purpose if category else None,
        "amount": (
            finance_service.in_terms_of(direction, operation.amount_minor)
            if direction
            else operation.amount_minor
        ),
        "comment": operation.comment,
        "happened_at": operation.happened_at.isoformat() if operation.happened_at else None,
        "deal_id": operation.deal_id,
        "client_id": operation.client_id,
        "company_id": operation.company_id,
        "author_id": operation.author_id,
        "author_name": author_name,
    }


def _decorate(db: Session, operations: list) -> list[dict]:
    """Дописывает к операциям статью и имя автора — двумя запросами на выдачу.

    «По какой статье» и «кто завёл» — первые два вопроса к любой строке журнала,
    а по запросу на строку журнал из двухсот операций сделал бы четыреста
    обращений к базе.
    """
    categories = finance_repo.categories_by_ids(db, {op.category_id for op in operations})
    author_ids = {op.author_id for op in operations if op.author_id}
    names = {u.id: u.name for u in users_repo.get_many(db, author_ids)} if author_ids else {}
    return [
        _operation_out(op, categories.get(op.category_id), names.get(op.author_id))
        for op in operations
    ]


# --- статьи ---


@router.get("/categories")
def list_categories(
    include_closed: bool = False,
    user: User = Depends(require_perm("finance", "view")),
    db: Session = Depends(get_db),
):
    # Закрытые по запросу: журнал за прошлый год ссылается на статьи, которых в
    # выборе уже нет, и без них его строки остались бы без подписи.
    items = finance_service.list_categories(db, include_deleted=include_closed)
    return {"items": [_category_out(row) for row in items]}


@router.post("/categories", status_code=201)
def create_category(
    payload: CategoryIn,
    user: User = Depends(require_perm("finance", "manage")),
    db: Session = Depends(get_db),
):
    return _category_out(finance_service.create_category(db, payload.model_dump()))


@router.patch("/categories/{category_id}")
def update_category(
    category_id: int,
    payload: CategoryIn,
    user: User = Depends(require_perm("finance", "manage")),
    db: Session = Depends(get_db),
):
    return _category_out(
        finance_service.update_category(db, category_id, payload.model_dump(exclude_unset=True))
    )


@router.delete("/categories/{category_id}")
def close_category(
    category_id: int,
    user: User = Depends(require_perm("finance", "manage")),
    db: Session = Depends(get_db),
):
    finance_service.close_category(db, category_id, user)
    return {"ok": True}


# --- операции ---


@router.get("/operations")
def list_operations(
    period: Period = Depends(),
    category_id: int | None = None,
    direction: str | None = None,
    deal_id: int | None = None,
    client_id: int | None = None,
    company_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("finance", "view")),
    db: Session = Depends(get_db),
):
    if direction is not None and direction not in DIRECTIONS:
        # Неизвестное направление отсекаем здесь, а не отдаём пустой список:
        # опечатка в параметре выглядела бы как «за август не было расходов».
        direction = None
    items, total = finance_service.list_operations(
        db,
        category_id=category_id,
        direction=direction,
        deal_id=deal_id,
        client_id=client_id,
        company_id=company_id,
        start=period.start,
        end=period.end,
        page=page,
        per_page=per_page,
    )
    data = schemas.paginated(_decorate(db, items), total, page, per_page)
    data.update(period.envelope(db))
    return data


@router.post("/operations", status_code=201)
def create_operation(
    payload: OperationIn,
    user: User = Depends(require_perm("finance", "create")),
    db: Session = Depends(get_db),
):
    operation = finance_service.create_operation(db, payload.model_dump(), user)
    category = finance_repo.get_category(db, operation.category_id, include_deleted=True)
    return _operation_out(operation, category, user.name)


# --- прибыль ---


@router.get("/profit")
def profit(
    period: Period = Depends(),
    user: User = Depends(require_perm("finance", "view")),
    db: Session = Depends(get_db),
):
    """Доход минус расход за период, с разбивкой по статьям.

    Считается запросом и нигде не хранится — отдельного маршрута «пересчитать
    прибыль» нет и быть не может: пересчитывать нечего, число рождается заново
    на каждый запрос.
    """
    return {**period.envelope(db), **finance_service.profit(db, period.start, period.end)}


# --- бюджеты ---


@router.get("/budgets")
def list_budgets(
    period: Period = Depends(),
    user: User = Depends(require_perm("finance", "view")),
    db: Session = Depends(get_db),
):
    items = finance_service.budgets(db, period.start_day, period.end_day, period.tz_offset)
    return {**period.envelope(db), "items": items}


@router.post("/budgets", status_code=201)
def create_budget(
    payload: BudgetIn,
    user: User = Depends(require_perm("finance", "manage")),
    db: Session = Depends(get_db),
):
    budget = finance_service.create_budget(db, payload.model_dump(), user)
    return {"id": budget.id}


@router.patch("/budgets/{budget_id}")
def update_budget(
    budget_id: int,
    payload: BudgetPatchIn,
    user: User = Depends(require_perm("finance", "manage")),
    db: Session = Depends(get_db),
):
    budget = finance_service.update_budget(
        db, budget_id, payload.model_dump(exclude_unset=True), user
    )
    return {"id": budget.id, "planned": budget.planned_amount_minor, "note": budget.note}


@router.delete("/budgets/{budget_id}")
def delete_budget(
    budget_id: int,
    user: User = Depends(require_perm("finance", "manage")),
    db: Session = Depends(get_db),
):
    finance_service.delete_budget(db, budget_id, user)
    return {"ok": True}
