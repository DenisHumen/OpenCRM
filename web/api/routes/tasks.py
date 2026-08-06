"""Напоминания: списки, счётчики, отметка о выполнении."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import task_service
from database.models import User
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_module("tasks"))]
)


class TaskIn(BaseModel):
    title: str
    # Абсолютный момент со смещением: «18:00 по Киеву», а не «18:00» вообще.
    due_at: datetime | None = None
    assignee_id: int | None = None
    client_id: int | None = None
    deal_id: int | None = None


class TaskPatchIn(BaseModel):
    title: str | None = None
    due_at: datetime | None = None
    assignee_id: int | None = None
    client_id: int | None = None
    deal_id: int | None = None
    is_done: bool | None = None


def _out(db: Session, tasks: list) -> list[dict]:
    names = {
        u.id: u.name
        for u in users_repo.get_many(db, {t.assignee_id for t in tasks if t.assignee_id})
    }
    return [schemas.task_out(t, names.get(t.assignee_id)) for t in tasks]


@router.get("")
def list_tasks(
    scope: str = Query(default="open"),
    assignee_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    _: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    tasks = task_service.search(
        db, scope=scope, assignee_id=assignee_id, client_id=client_id, deal_id=deal_id
    )
    return {"items": _out(db, tasks)}


@router.get("/summary")
def summary(user: User = Depends(require_perm("tasks", "view")), db: Session = Depends(get_db)):
    """Счётчики для навигации. Отдельной точкой, потому что их спрашивают
    часто и без списка: полоса с числом просроченных висит на каждом экране."""
    return task_service.summary(db, user)


@router.post("", status_code=201)
def create_task(
    payload: TaskIn,
    user: User = Depends(require_perm("tasks", "create")),
    db: Session = Depends(get_db),
):
    task = task_service.create(db, payload.model_dump(exclude_unset=True), user)
    return _out(db, [task])[0]


@router.patch("/{task_id}")
def update_task(
    task_id: int,
    payload: TaskPatchIn,
    _: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    task = task_service.update(db, task_id, payload.model_dump(exclude_unset=True))
    return _out(db, [task])[0]


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    _: User = Depends(require_perm("tasks", "delete")),
    db: Session = Depends(get_db),
):
    task_service.delete(db, task_id)
    return {"message": "Task deleted"}
