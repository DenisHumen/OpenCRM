"""Запросы по напоминаниям.

Границы «сегодня» и «недели» приходят сюда готовым моментом времени, а не
считаются здесь. Так и должно быть: для человека «на сегодня» — это «до конца
рабочего дня», и решать, от чего отсчитывать, — дело сервиса, знающего про
часовые пояса. Репозиторий только спрашивает базу.

Счётчики считаются в базе, а не в Python. Прежний `len(list(...))` поднимал в
память каждую незакрытую задачу и делал это четыре раза за один заход на любую
страницу: на 13 тысячах задач счётчики в меню стоили 447 мс, тем же условием
через `count(*)` — 16 мс.
"""

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from database.models import Task

#: Виды списков, которыми пользуются каждый день.
SCOPE_OPEN = "open"
SCOPE_OVERDUE = "overdue"
SCOPE_TODAY = "today"
SCOPE_WEEK = "week"
SCOPE_DONE = "done"


def get(db: Session, task_id: int) -> Task | None:
    return db.get(Task, task_id)


def _open_only(query: Select) -> Select:
    return query.where(Task.done_at.is_(None))


def search(
    db: Session,
    *,
    scope: str = SCOPE_OPEN,
    now,
    until=None,
    assignee_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    limit: int,
) -> list[Task]:
    """Список напоминаний. `until` — граница срока для «сегодня» и «недели»."""
    query = select(Task)

    if scope == SCOPE_DONE:
        query = query.where(Task.done_at.is_not(None)).order_by(Task.done_at.desc())
    else:
        query = _open_only(query)
        if scope == SCOPE_OVERDUE:
            query = query.where(Task.due_at.is_not(None), Task.due_at < now)
        elif until is not None:
            query = query.where(Task.due_at.is_not(None), Task.due_at < until)
        # Сначала то, у чего есть срок, потом бессрочное: задача без срока не
        # должна оттеснять просроченную наверх.
        query = query.order_by(Task.due_at.is_(None), Task.due_at.asc(), Task.id.desc())

    if assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)
    if client_id is not None:
        query = query.where(Task.client_id == client_id)
    if deal_id is not None:
        query = query.where(Task.deal_id == deal_id)

    return list(db.scalars(query.limit(limit)))


def counters(db: Session, *, user_id: int, now, today_until) -> dict[str, int]:
    """Счётчики для навигации: без них в задачи заходят «на всякий случай».

    Считаются мимо потолка списка нарочно: дело счётчика — сказать, сколько
    есть всего, а не сколько поместилось на экран.
    """

    def count(*conditions) -> int:
        query = _open_only(select(func.count(Task.id)))
        for condition in conditions:
            query = query.where(condition)
        return db.scalar(query) or 0

    # Ничей — тоже мой: задача без исполнителя лежит на том, кто её увидит, и
    # спрятать её из счётчика значило бы дать ей потеряться совсем.
    mine = or_(Task.assignee_id == user_id, Task.assignee_id.is_(None))
    return {
        "overdue": count(Task.due_at.is_not(None), Task.due_at < now),
        "today": count(Task.due_at.is_not(None), Task.due_at < today_until),
        "mine": count(mine),
        "open": count(),
    }
