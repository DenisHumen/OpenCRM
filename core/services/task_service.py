"""Напоминания: перезвонить, отправить счёт, забрать технику."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.utils import now_utc
from database.models import Task, User

MAX_TITLE = 300

#: Потолок списка напоминаний. Счётчики в меню считают мимо него: их дело —
#: сказать, сколько есть всего, а не сколько поместилось на экран.
LIST_LIMIT = 200


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Приводит срок к тому виду, в каком время лежит в базе: naive UTC.

    Клиент присылает абсолютный момент со смещением («18:00 по Киеву»), и
    сохранить его как есть нельзя: SQLite сложит вместе aware и naive значения,
    а сравнение «просрочено ли» начнёт врать на величину смещения. Ошибка при
    этом тихая — напоминание просто сработает не тогда.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        # Пришло без зоны — считаем, что это уже UTC. Другого разумного
        # предположения нет: гадать о зоне отправителя хуже, чем принять
        # соглашение и записать его здесь.
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    return task


def create(db: Session, data: dict, author: User) -> Task:
    title = (data.get("title") or "").strip()
    if not title:
        raise errors.ValidationError("Title is required", code="title_required")

    task = Task(
        title=title[:MAX_TITLE],
        due_at=to_utc_naive(data.get("due_at")),
        # Не указали исполнителя — задача на авторе. «Ничья» задача не делается
        # никем: каждый считает, что её возьмёт кто-то другой.
        assignee_id=data["assignee_id"] if "assignee_id" in data else author.id,
        client_id=data.get("client_id"),
        deal_id=data.get("deal_id"),
        created_by=author.id,
    )
    db.add(task)
    db.flush()
    return task


def update(db: Session, task_id: int, data: dict) -> Task:
    task = get_task(db, task_id)
    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise errors.ValidationError("Title is required", code="title_required")
        task.title = title[:MAX_TITLE]
    if "due_at" in data:
        task.due_at = to_utc_naive(data["due_at"])
    if "assignee_id" in data:
        task.assignee_id = data["assignee_id"]
    if "client_id" in data:
        task.client_id = data["client_id"]
    if "deal_id" in data:
        task.deal_id = data["deal_id"]
    if "is_done" in data and data["is_done"] is not None:
        # Дата закрытия отвечает сразу на два вопроса: сделано ли и когда.
        task.done_at = now_utc() if data["is_done"] else None
    db.flush()
    return task


def delete(db: Session, task_id: int) -> None:
    db.delete(get_task(db, task_id))
    db.flush()


def _open_only(query):
    return query.where(Task.done_at.is_(None))


def search(
    db: Session,
    scope: str = "open",
    assignee_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    limit: int = LIST_LIMIT,
) -> list[Task]:
    """Списки, которыми пользуются каждый день.

    `scope`: open | overdue | today | week | done. Границы «сегодня» и «недели»
    считаются от текущего момента, а не от полуночи по UTC: для человека
    «на сегодня» — это «до конца рабочего дня», и сдвигать эту границу на
    произвольное число часов из-за зоны сервера нельзя.
    """
    now = now_utc()
    query = select(Task)

    if scope == "done":
        query = query.where(Task.done_at.is_not(None)).order_by(Task.done_at.desc())
    else:
        query = _open_only(query)
        if scope == "overdue":
            query = query.where(Task.due_at.is_not(None), Task.due_at < now)
        elif scope == "today":
            query = query.where(Task.due_at.is_not(None), Task.due_at < now + timedelta(days=1))
        elif scope == "week":
            query = query.where(Task.due_at.is_not(None), Task.due_at < now + timedelta(days=7))
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


def summary(db: Session, user: User) -> dict:
    """Счётчики для навигации: без них в задачи заходят «на всякий случай»."""
    now = now_utc()

    def count(*conditions) -> int:
        # Считаем в базе, а не в Python. Прежний `len(list(...))` поднимал в
        # память каждую незакрытую задачу и делал это четыре раза за один заход
        # на любую страницу: на 13 тысячах задач счётчики в меню стоили 447 мс,
        # тем же условием через `count(*)` — 16 мс.
        query = _open_only(select(func.count(Task.id)))
        for condition in conditions:
            query = query.where(condition)
        return db.scalar(query) or 0

    mine = or_(Task.assignee_id == user.id, Task.assignee_id.is_(None))
    return {
        "overdue": count(Task.due_at.is_not(None), Task.due_at < now),
        "today": count(Task.due_at.is_not(None), Task.due_at < now + timedelta(days=1)),
        "mine": count(mine),
        "open": count(),
    }
