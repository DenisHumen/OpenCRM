"""Напоминания: перезвонить, отправить счёт, забрать технику."""

from datetime import timedelta

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import references
from core.utils import now_utc, to_utc_naive
from database.models import Task, User
from database.repositories import tasks as tasks_repo

MAX_TITLE = 300

#: Потолок списка напоминаний. Счётчики в меню считают мимо него: их дело —
#: сказать, сколько есть всего, а не сколько поместилось на экран.
LIST_LIMIT = 200


def get_task(db: Session, task_id: int) -> Task:
    task = tasks_repo.get(db, task_id)
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
        assignee_id=(
            references.user(
                db, data["assignee_id"], code="assignee_not_found", message="Assignee not found"
            )
            if "assignee_id" in data
            else author.id
        ),
        client_id=references.client(db, data.get("client_id")),
        deal_id=references.deal(db, data.get("deal_id")),
        created_by=author.id,
    )
    db.add(task)
    db.flush()
    _skazat_ispolnitelyu(db, task, author)
    return task


def update(db: Session, task_id: int, data: dict, author: User | None = None) -> Task:
    task = get_task(db, task_id)
    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise errors.ValidationError("Title is required", code="title_required")
        task.title = title[:MAX_TITLE]
    if "due_at" in data:
        task.due_at = to_utc_naive(data["due_at"])
    if "assignee_id" in data:
        task.assignee_id = references.user(
            db, data["assignee_id"], code="assignee_not_found", message="Assignee not found"
        )
    if "client_id" in data:
        task.client_id = references.client(db, data["client_id"])
    if "deal_id" in data:
        task.deal_id = references.deal(db, data["deal_id"])
    if "is_done" in data and data["is_done"] is not None:
        # Дата закрытия отвечает сразу на два вопроса: сделано ли и когда.
        task.done_at = now_utc() if data["is_done"] else None
    db.flush()
    if "assignee_id" in data:
        _skazat_ispolnitelyu(db, task, author)
    return task


def _skazat_ispolnitelyu(db: Session, task: Task, author: User | None) -> None:
    """Напоминание повесили на другого — он узнаёт об этом сразу, а не в срок."""
    from core.services import notification_service
    from database.repositories import users as users_repo

    if not task.assignee_id or (author is not None and task.assignee_id == author.id):
        return
    komu = users_repo.get_by_id(db, task.assignee_id)
    if komu is not None:
        notification_service.notify(db, [komu], "task_assigned", {"title": task.title}, "/tasks")


def delete(db: Session, task_id: int) -> None:
    db.delete(get_task(db, task_id))
    db.flush()


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
    horizon = {"today": timedelta(days=1), "week": timedelta(days=7)}.get(scope)
    return tasks_repo.search(
        db,
        scope=scope,
        now=now,
        until=now + horizon if horizon else None,
        assignee_id=assignee_id,
        client_id=client_id,
        deal_id=deal_id,
        limit=limit,
    )


def summary(db: Session, user: User) -> dict:
    """Счётчики для навигации: без них в задачи заходят «на всякий случай»."""
    now = now_utc()
    return tasks_repo.counters(
        db, user_id=user.id, now=now, today_until=now + timedelta(days=1)
    )
