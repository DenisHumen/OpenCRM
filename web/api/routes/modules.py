"""Какие блоки системы включены.

Читать может любой сотрудник: интерфейс должен знать, что показывать в меню, а
что скрыть. Переключать — только root: это решение уровня «каким бизнесом мы
занимаемся», а не личная настройка менеджера.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import modules_service
from database.models import User
from database.repositories import users as users_repo
from web.api.deps import get_db, require_root, require_staff

router = APIRouter(prefix="/modules", tags=["modules"])


class ModuleIn(BaseModel):
    enabled: bool


def _with_authors(db: Session, items: list[dict]) -> list[dict]:
    """Подставляет имя того, кто переключил.

    Без имени запись «выключено 3 августа» отвечает на половину вопроса: когда
    раздел пропал из меню, спрашивают не только «когда», но и «кто».
    """
    ids = {item["updated_by"] for item in items if item["updated_by"]}
    names = {u.id: u.name for u in users_repo.get_many(db, ids)} if ids else {}
    for item in items:
        item["updated_by_name"] = names.get(item.pop("updated_by"))
    return items


@router.get("")
def list_modules(
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    return {"items": _with_authors(db, modules_service.details(db))}


@router.post("/{key}")
def switch_module(
    key: str,
    payload: ModuleIn,
    user: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    modules_service.set_enabled(db, key, payload.enabled, user)
    return {"items": _with_authors(db, modules_service.details(db))}
