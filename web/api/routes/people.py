"""Коллеги — минимальный список для выбора ответственного.

Отдельно от `/staff` намеренно: тот раздел про управление аккаунтами и открыт
только root. Назначать сделку на коллегу должен любой сотрудник, но знать при
этом чужие адреса, статусы и даты одобрения ему незачем — здесь только то, без
чего не нарисовать выпадающий список.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database.models import User
from database.repositories import users as users_repo
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/people", tags=["people"])


@router.get("")
def list_people(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    return {
        "items": [
            {"id": u.id, "name": u.name, "avatar_url": u.avatar_path or None}
            for u in users_repo.list_staff(db, status="active")
        ]
    }
