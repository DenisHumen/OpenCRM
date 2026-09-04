"""Уведомления сотруднику: список своих, счётчик, отметка о прочтении.

Вне блоков и без отдельного права: уведомление адресовано конкретному человеку
и уже отфильтровано его правами при записи (`notification_service.adresaty`).
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import notification_service
from database.models import User
from web.api.deps import get_current_user, get_db

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ReadIn(BaseModel):
    #: Пусто — прочитать все свои.
    ids: list[int] | None = None


@router.get("")
def list_notifications(
    page: int = Query(default=1, ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = notification_service.spisok(db, user, page)
    return {"items": items, "total": total, "page": page, "per_page": notification_service.NA_STRANITSE}


@router.get("/summary")
def summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Счётчик для колокольчика: спрашивают часто, без списка."""
    return {"unread": notification_service.neprochitano(db, user)}


@router.post("/read")
def mark_read(
    payload: ReadIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"marked": notification_service.prochitat(db, user, payload.ids)}
