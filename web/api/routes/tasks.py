"""Напоминания: списки, счётчики, отметка о выполнении."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import task_service
from database.models import User
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_module("tasks"))]
)


class TaskIn(BaseModel):
    title: str
    # urgent | high | normal | low — перечень в `database/models/task.py`.
    vazhnost: str | None = None
    note: str | None = None
    # Абсолютный момент со смещением: «18:00 по Киеву», а не «18:00» вообще.
    due_at: datetime | None = None
    assignee_id: int | None = None
    client_id: int | None = None
    deal_id: int | None = None


class TaskPatchIn(BaseModel):
    title: str | None = None
    vazhnost: str | None = None
    note: str | None = None
    due_at: datetime | None = None
    assignee_id: int | None = None
    client_id: int | None = None
    deal_id: int | None = None
    is_done: bool | None = None


def _out(db: Session, tasks: list) -> list[dict]:
    """Имена рядом с номерами: в списке «Заявка» и «Клиент» без названий
    отвечали лишь на вопрос «есть ли», а спрашивают «какая»."""
    names = {
        u.id: u.name
        for u in users_repo.get_many(db, {t.assignee_id for t in tasks if t.assignee_id})
    }
    klienty = clients_repo.names_by_ids(db, [t.client_id for t in tasks if t.client_id])
    zayavki = {d.id: d.title for d in deals_repo.by_ids(db, {t.deal_id for t in tasks if t.deal_id})}
    nomera = [t.id for t in tasks]
    vlozheniya = task_service.files_counts(db, nomera)
    zametki = task_service.zametki_est(db, nomera)
    return [
        schemas.task_out(
            t,
            names.get(t.assignee_id),
            klienty.get(t.client_id),
            zayavki.get(t.deal_id),
            files_count=vlozheniya.get(t.id, 0),
            note_est=t.id in zametki,
        )
        for t in tasks
    ]


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


@router.get("/{task_id}")
def get_task(
    task_id: int,
    _: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    """Карточка напоминания: то же, что в списке, плюс вложения. Отдельной
    точкой, потому что список из двухсот строк не должен тянуть их все."""
    task = task_service.get_task(db, task_id)
    vlozheniya = task_service.files(db, task.id)
    data = _out(db, [task])[0]
    # Подробности и вложения — только здесь: список от них берёт «есть ли».
    data["note"] = task.note
    data["files"] = [schemas.task_file_out(f) for f in vlozheniya]
    data["files_count"] = len(vlozheniya)
    return data


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
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    task = task_service.update(db, task_id, payload.model_dump(exclude_unset=True), user)
    return _out(db, [task])[0]


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    user: User = Depends(require_perm("tasks", "delete")),
    db: Session = Depends(get_db),
):
    task_service.delete(db, task_id, user)
    return {"message": "Task deleted"}


@router.post("/{task_id}/files", status_code=201)
async def upload_file(
    task_id: int,
    file: UploadFile,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    content = await file.read()
    record = task_service.add_file(db, task_id, user, file.filename or "file", content)
    return schemas.task_file_out(record)


@router.get("/{task_id}/files/{file_id}/download")
def download_file(
    task_id: int,
    file_id: int,
    _: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    record = task_service.get_file(db, task_id, file_id)
    path = task_service.file_path_on_disk(record)
    if not path.exists():
        raise errors.NotFoundError("File is missing on disk", code="file_missing")
    # Картинкой, а не вложением: её смотрят прямо в карточке. Тип — из
    # расширения, уже сверенного с содержимым при приёме.
    return FileResponse(
        path,
        media_type=task_service.mime_dlya_otdachi(record),
        filename=record.original_name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{task_id}/files/{file_id}")
def delete_file(
    task_id: int,
    file_id: int,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    task_service.delete_file(db, task_id, file_id, user)
    return {"message": "File deleted"}
