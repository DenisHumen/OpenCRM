"""Настройки приёма заявок с сайта.

Сам приём — публичный и живёт в `web/public/leads.py`. Здесь только то, чем его
настраивают, и устроено оно ровно как настройки телефонии: ключ показывается
**один раз** при создании, наружу отдаётся лишь факт «задан или нет».

Довод тот же, что у секрета вебхука АТС: ключ, который можно посмотреть в
интерфейсе, рано или поздно уезжает в скриншот переписки с подрядчиком. Показать
его один раз неудобно, зато честно — потерявший заводит новый, и старый
перестаёт работать в ту же секунду.

Пустой ключ означает, что приёма нет вовсе: публичная ручка отвечает так, будто
её не существует. Это и есть «закрыть форму» — отдельного выключателя не нужно.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import lead_service
from database.models import User
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/leads", tags=["leads"])


class ManagerIn(BaseModel):
    #: Кому достаются карточка, заявка и напоминание. `None` — владельцу системы.
    manager_id: int | None = None


@router.get("/settings")
def get_lead_settings(
    _: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    return lead_service.public_settings(db)


@router.post("/settings/key", status_code=201)
def regenerate_intake_key(
    _: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Новый ключ приёма. Возвращается **один раз** — как секрет вебхука АТС."""
    return {"key": lead_service.regenerate_intake_key(db)}


@router.delete("/settings/key")
def close_intake(
    _: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Закрыть приём: без ключа публичная ручка отвечает как несуществующая."""
    lead_service.clear_intake_key(db)
    return lead_service.public_settings(db)


@router.patch("/settings/manager")
def set_manager(
    payload: ManagerIn,
    _: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    lead_service.set_manager(db, payload.manager_id)
    return lead_service.public_settings(db)
