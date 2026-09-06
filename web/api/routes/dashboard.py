from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import (
    finance_service,
    modules_service,
    order_service,
    permissions_service,
    pipeline_service,
    settings_service,
    task_service,
    vidzhety_service,
)
from core.utils import now_utc
from database.models import User
from database.models.document import OPEN_ORDER_STATUSES, ORDER_KINDS
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo
from database.repositories import stats as stats_repo
from database.repositories import svodka as svodka_repo
from database.repositories import vozvraty as vozvraty_repo
from database.repositories import warehouse as warehouse_repo
from web.api import cards, schemas
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    now = now_utc()

    # Окно просмотров считается ОДИН раз на все запросы блока: пока плитка брала
    # скользящие 168 часов, а график — семь календарных суток, число над графиком
    # не сходилось с суммой столбиков под ним нигде, кроме полуночи.
    views_start, views_end = stats_repo.views_window(7, now)
    # Прошлая неделя — те же семь календарных суток, только сдвинутые: сравнивать
    # календарную неделю со скользящей значило бы считать рост от разной длины.
    prev_start = views_start - timedelta(days=7)

    clients_total, clients_this_month, clients_this_week, clients_without_deals = stats_repo.clients_totals(db)

    # Сводка сужается вместе с блоками, а не отказывает: выключили доски — их
    # слагаемых в ответе нет, остальное считается как считалось. Пустые значения,
    # а не отсутствующие ключи: форма ответа одна при любом наборе блоков.
    boards_total = boards_published = views_7d = views_prev_7d = unique_7d = 0
    last_view = None
    views_by_day: list[dict] = stats_repo.views_by_day(db, views_start, views_end)
    boards_payload: list[dict] = []
    if modules_service.is_enabled(db, "boards"):
        boards_total, boards_published = stats_repo.boards_totals(db)
        views_7d = stats_repo.views_in_range(db, views_start, views_end)
        views_prev_7d = stats_repo.views_in_range(db, prev_start, views_start)
        unique_7d = stats_repo.unique_viewers_in_range(db, views_start, views_end)
        last_view = stats_repo.last_view_at(db)

        recent_boards, _total = boards_repo.search(db, page=1, per_page=4)
        boards_payload = cards.board_cards(db, recent_boards)
    else:
        # Дни остаются, счётчики обнуляются: пустой массив сузил бы не слагаемое,
        # а саму ось графика. Список берём тем же вызовом: арифметика календаря
        # в двух местах разъедется раньше, чем окупится сэкономленный запрос.
        views_by_day = [{**day, "count": 0} for day in views_by_day]

    # Последние карточки — ТОЛЬКО тому, кому карточки вообще открыты: сводка
    # сужается тем же правом, что раздел. Без права имя, телефон и почта пяти
    # клиентов сами ехали на первый экран — а телефоны и уносят при уходе.
    recent_clients = []
    if permissions_service.has(db, user, "clients", "view"):
        recent_clients, _total = clients_repo.search(db, page=1, per_page=5)

    # Деньги с начала месяца, а не за последние 30 дней: владелец сверяет их с
    # месячной отчётностью, а скользящее окно ни с чем не сходится.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Сужается тем же правом, что список и канбан: спрятать чужие карточки и
    # оставить их сумму значит не запретить ничего — оборот и был целью.
    mine_only = permissions_service.deals_scope(db, user)
    money = deals_repo.money_summary(db, month_start, only_manager_id=mine_only)
    # Сколько ПРИШЛО в кассу с начала месяца — по решению «банк один» это и есть
    # выручка. Отдельным числом рядом с суммой выигранных заявок: «продали на» и
    # «получили» — разные вопросы, расхождение не ошибка. Пусто — кассы нет.
    basis = finance_service.bazis_vyruchki(db)
    kassa = finance_service.postupleniya_po_mesyatsam(
        db, [(month_start, now)], only_manager_id=mine_only
    )
    money["received_since"] = None if kassa is None else kassa.get(0, {}).get("total", 0)
    # Плитки с деньгами пустеют вместе с правом на суммы. Пустые значения, а не
    # отсутствующие ключи — то же правило, что у выключенных блоков выше.
    if not permissions_service.sees_amounts(db, user):
        # Пустеют деньги, но не счётчики: «сколько сделок я выиграл» — не сумма,
        # и человек и так видит это число в своём списке.
        money = {key: (None if key not in ("won_count",) else value)
                 for key, value in money.items()}

    # Воронка целиком, включая пустые этапы: «в согласовании ноль» — тоже ответ,
    # а показывай только непустые — и провал в середине станет невидимым.
    counts = deals_repo.stage_counts(db, only_manager_id=mine_only)
    # Сумма этапа рядом с числом — тем же правом, что плитки денег: без него
    # `None`, а не ноль, чтобы пустое не читалось как «на ноль».
    summy_vidny = permissions_service.sees_amounts(db, user)
    stages = [
        {
            "key": stage.key,
            "name": stage.name,
            "kind": stage.kind,
            "count": counts.get(stage.key, (0, 0))[0],
            "amount": counts.get(stage.key, (0, 0))[1] if summy_vidny else None,
        }
        for stage in pipeline_service.list_stages(db)
    ]

    # Задачи — того, кто смотрит: «мои на сегодня» отвечают на вопрос «с чего
    # начать», а общий список по всей фирме на него не отвечает.
    my_tasks: list[dict] = []
    if modules_service.is_enabled(db, "tasks"):
        my_tasks = [
            schemas.task_out(task)
            for task in task_service.search(
                db, scope="today", assignee_id=user.id, limit=6
            )
        ]

    # Блоки ниже — только при включённом блоке и праве на него; выключенный
    # уходит ключом `None`/пустым списком, форма ответа одна.
    orders_week = None
    recent_orders: list[dict] = []
    if modules_service.is_enabled(db, "orders") and permissions_service.has(db, user, "orders", "view"):
        nedelya = vozvraty_repo.svodka(db, now - timedelta(days=7), now)
        order_amounts = permissions_service.sees_amounts(db, user, "orders")
        orders_week = {
            "shipped_count": nedelya["shipped_count"],
            "returns_count": nedelya["count"],
            "refund_amount": nedelya["refund"] if order_amounts else None,
            # Просроченные открытые — красным: их разбирают первыми.
            "overdue_count": documents_repo.prosrocheno_zakazov(db, ORDER_KINDS, OPEN_ORDER_STATUSES, now),
        }
        svezhie, _vsego = documents_repo.search(db, kinds=ORDER_KINDS, page=1, per_page=5)
        rows = documents_repo.lines_by_documents(db, [o.id for o in svezhie])
        imena = clients_repo.names_by_ids(db, [o.client_id for o in svezhie if o.client_id])
        recent_orders = [
            {
                "id": o.id,
                "number": o.number,
                "kind": o.kind,
                "status": o.status,
                "client_name": imena.get(o.client_id),
                "total": order_service.total_minor(rows.get(o.id, [])) if order_amounts else None,
                "updated_at": o.updated_at.isoformat() if o.updated_at else None,
            }
            for o in svezhie
        ]

    low_stock: list[dict] = []
    low_stock_total = 0
    if modules_service.is_enabled(db, "warehouse") and permissions_service.has(db, user, "warehouse", "view"):
        malo, low_stock_total = warehouse_repo.malo_ili_konchilos(db, limit=5)
        low_stock = [
            {
                "id": product.id,
                "name": product.name,
                "unit": product.unit,
                "stock_milli": milli,
                "min_stock_milli": product.min_stock_milli,
                "out": milli <= 0,
            }
            for product, milli in malo
        ]

    tasks_counters = task_service.summary(db, user) if modules_service.is_enabled(db, "tasks") else None

    calls_24h = None
    if modules_service.is_enabled(db, "telephony") and permissions_service.has(db, user, "telephony", "view"):
        calls_24h = svodka_repo.zvonki(db, now - timedelta(hours=24), now)

    return {
        "currency": settings_service.get_all(db).get("currency", "USD"),
        # Долг по открытым заявкам — из той же сводки денег, что и плитки выше:
        # без права на суммы он уже пуст.
        "money_due": money["due"],
        "orders_week": orders_week,
        "recent_orders": recent_orders,
        "low_stock": low_stock,
        "low_stock_total": low_stock_total,
        "tasks_counters": tasks_counters,
        "calls_24h": calls_24h,
        "money_in_work": money["in_work"],
        "money_won_this_month": money["won_since"],
        # Чем меряется выручка — экран берёт ось отсюда, а не из своей карты
        # блоков: два ответа на один вопрос дадут два числа под одной подписью.
        "money_basis": basis,
        "money_received_this_month": money["received_since"],
        "avg_check": money["avg_check"],
        "won_count_this_month": money["won_count"],
        "deals_by_stage": stages,
        "my_tasks": my_tasks,
        "clients_total": clients_total,
        "clients_this_month": clients_this_month,
        "clients_this_week": clients_this_week,
        "clients_without_deals": clients_without_deals,
        "boards_total": boards_total,
        "boards_published": boards_published,
        "views_7d": views_7d,
        "views_prev_7d": views_prev_7d,
        "unique_viewers_7d": unique_7d,
        "views_by_day": views_by_day,
        "last_view_at": last_view.isoformat() if last_view else None,
        "recent_boards": boards_payload,
        "recent_clients": [schemas.client_out(c) for c in recent_clients],
    }


class WidgetIn(BaseModel):
    """Виджет раскладки: вид, ширина (пусто — по реестру), параметры — у ключа сайта."""

    kind: str = Field(max_length=40)
    w: int | None = None
    params: dict = Field(default_factory=dict)


class LayoutIn(BaseModel):
    widgets: list[WidgetIn] = Field(default_factory=list, max_length=vidzhety_service.POTOLOK + 1)


def _reestr() -> dict:
    """Реестр виджетов для экрана: ширины, блок и право. Одна карта на обоих —
    вторая копия на фронтенде разошлась бы с этой молча."""
    return {
        kind: {"w": opis["w"], "shiriny": list(opis["shiriny"]), "odin": opis["odin"],
               "module": opis["module"], "perm": opis["perm"]}
        for kind, opis in vidzhety_service.REESTR.items()
    }


@router.get("/layout")
def dashboard_layout(user: User = Depends(require_staff)):
    """Раскладка сводки того, кто спрашивает; `null` — умолчание экрана."""
    return {"layout": vidzhety_service.chitat(user), "kinds": _reestr()}


@router.put("/layout")
def save_dashboard_layout(
    payload: LayoutIn, user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    """Сохранить раскладку. Неизвестный виджет, чужой блок или право, второй
    такой же, ключ сайта без ключа — отказ с кодом, а не молчаливая потеря."""
    raskladka = vidzhety_service.sohranit(db, user, [w.model_dump() for w in payload.widgets])
    return {"layout": raskladka, "kinds": _reestr()}


@router.delete("/layout")
def reset_dashboard_layout(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Вернуть умолчание экрана."""
    vidzhety_service.sbrosit(db, user)
    return {"layout": None, "kinds": _reestr()}
