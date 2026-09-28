"""Web Push: ключ, подписки этого сотрудника, кнопки системного уведомления (docs/bloki/31-web-push.md)."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import modules_service, permissions_service, push_service, task_service
from database.models import User
from database.models.user import STATUS_ACTIVE
from database.repositories import users as users_repo
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/push", tags=["push"])


class KlyuchiIn(BaseModel):
    p256dh: str
    auth: str


class PodpiskaIn(BaseModel):
    endpoint: str
    keys: KlyuchiIn
    nazvanie: str = ""


class DeystvieIn(BaseModel):
    token: str
    deystvie: Literal["done", "later"]


@router.get("/key")
def push_key(_: User = Depends(require_staff)):
    return {"key": push_service.otkrytyy_klyuch()}


@router.get("/subscriptions")
def my_subscriptions(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    return {"items": push_service.moi(db, user)}


@router.post("/subscriptions", status_code=201)
def subscribe(payload: PodpiskaIn, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    return push_service.podpiska_out(push_service.podpisat(db, user, payload.model_dump()))


@router.delete("/subscriptions/{sub_id}")
def unsubscribe(sub_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    push_service.otpisat(db, user, sub_id)
    return {"ok": True}


@router.post("/action")
def push_action(payload: DeystvieIn, db: Session = Depends(get_db)):
    """Кнопка в уведомлении. Сессии нет — есть подпись (напоминание, сотрудник, срок звонка)."""
    podpis = push_service.deystvie_proverit(payload.token)
    user = users_repo.get_by_id(db, podpis.user_id)
    if user is None or user.status != STATUS_ACTIVE:
        raise errors.AuthError("Bad action token", code="push_deystvie_ne_to")
    if not modules_service.is_enabled(db, "tasks"):
        raise errors.ForbiddenError("Module 'tasks' is switched off", code="module_disabled")
    pravo = "edit" if payload.deystvie == "done" else "view"
    if not permissions_service.has(db, user, "tasks", pravo):
        raise errors.ForbiddenError(f"Permission required: tasks.{pravo}", code="permission_denied")
    task = task_service.dostupnoe(db, user, podpis.task_id)
    # Кнопка старого звонка не закрывает следующий раз повторяющегося напоминания.
    if task.done_at is not None or push_service.srok_sek(task.due_at) != podpis.srok:
        raise errors.ConflictError("The reminder has moved on", code="zvonok_ustarel")
    if payload.deystvie == "done":
        task_service.zakryt(db, task, user)
    else:
        task_service.otlozhit(db, task, user, push_service.otlozhit_do())
    return {"ok": True, "deystvie": payload.deystvie}
