from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.services import (
    modules_service,
    permissions_service,
    pipeline_service,
    settings_service,
    task_service,
)
from core.utils import now_utc
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import stats as stats_repo
from web.api import cards, schemas
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    now = now_utc()
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    clients_total, clients_this_month = stats_repo.clients_totals(db)

    # Сводка сужается вместе с блоками, а не отказывает: выключили доски —
    # слагаемых про витрины в ответе просто нет, остальные числа считаются как
    # считались. Пустые значения, а не отсутствующие ключи: форма ответа одна
    # при любом наборе блоков.
    boards_total = boards_published = views_7d = views_prev_7d = unique_7d = 0
    last_view = None
    views_by_day: list[dict] = stats_repo.views_by_day(db, 7)
    boards_payload: list[dict] = []
    if modules_service.is_enabled(db, "boards"):
        boards_total, boards_published = stats_repo.boards_totals(db)
        views_7d = stats_repo.views_in_range(db, week_ago, now)
        views_prev_7d = stats_repo.views_in_range(db, two_weeks_ago, week_ago)
        unique_7d = stats_repo.unique_viewers_in_range(db, week_ago, now)
        last_view = stats_repo.last_view_at(db)

        recent_boards, _total = boards_repo.search(db, page=1, per_page=4)
        boards_payload = cards.board_cards(db, recent_boards)
    else:
        # Дни остаются, счётчики обнуляются: график просмотров рисуется по этому
        # списку, и пустой массив сузил бы не слагаемое, а саму ось. Считаем
        # список тем же вызовом, а не собираем даты заново — арифметика
        # календаря в двух местах разъедется раньше, чем окупится один запрос.
        views_by_day = [{**day, "count": 0} for day in views_by_day]

    recent_clients, _total = clients_repo.search(db, page=1, per_page=5)

    # Деньги с начала текущего месяца, а не за последние 30 дней: владелец
    # сверяет их с месячной отчётностью, и скользящее окно давало бы число,
    # которое ни с чем не сходится.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Сводка сужается тем же правом, что список и канбан: спрятать чужие
    # карточки и оставить их сумму значит не запретить ничего — узнать
    # оборот фирмы и было целью.
    mine_only = permissions_service.deals_scope(db, user)
    money = deals_repo.money_summary(db, month_start, only_manager_id=mine_only)
    # Плитки с деньгами пустеют вместе с правом на суммы. Пустые значения, а не
    # отсутствующие ключи — то же правило, что у выключенных блоков выше.
    #
    # Счётчики по этапам сужаются тем же правом — как и всё остальное на сводке.
    # (Комментарий здесь долго утверждал обратное — «остаются общими по фирме»,
    # — хотя код сужал их с самого начала. Комментарий, разошедшийся с кодом,
    # хуже отсутствующего: следующий читатель поверит ему и либо «починит»
    # правильное поведение, либо примет неверное решение о приватности.)
    if not permissions_service.sees_amounts(db, user):
        # Пустеют деньги, но не счётчики: «сколько сделок я выиграл» — не сумма,
        # и прятать его вместе с суммами значило отнимать у человека число,
        # которое он и так видит в своём списке.
        money = {key: (None if key not in ("won_count",) else value)
                 for key, value in money.items()}

    # Воронка целиком, включая пустые этапы: «в согласовании ноль» — это тоже
    # ответ, и чаще всего именно он и нужен. Показывай только непустые — и
    # провал в середине воронки станет невидимым.
    counts = deals_repo.stage_counts(db, only_manager_id=mine_only)
    stages = [
        {
            "key": stage.key,
            "name": stage.name,
            "kind": stage.kind,
            "count": counts.get(stage.key, 0),
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

    return {
        "currency": settings_service.get_all(db).get("currency", "USD"),
        "money_in_work": money["in_work"],
        "money_won_this_month": money["won_since"],
        "avg_check": money["avg_check"],
        "won_count_this_month": money["won_count"],
        "deals_by_stage": stages,
        "my_tasks": my_tasks,
        "clients_total": clients_total,
        "clients_this_month": clients_this_month,
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
