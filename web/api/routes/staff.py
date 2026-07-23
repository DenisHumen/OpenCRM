from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.services import auth_service
from database.models import User
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_root

router = APIRouter(prefix="/staff", tags=["staff"], dependencies=[Depends(require_root)])


@router.get("")
def list_staff(status: str | None = Query(default=None), db: Session = Depends(get_db)):
    users = users_repo.list_staff(db, status=status)
    return {"items": [schemas.user_out(u) for u in users]}


@router.post("/{user_id}/approve")
def approve(user_id: int, db: Session = Depends(get_db)):
    return schemas.user_out(auth_service.approve(db, user_id))


@router.post("/{user_id}/reject")
def reject(user_id: int, db: Session = Depends(get_db)):
    auth_service.reject(db, user_id)
    return {"message": "Registration rejected"}


@router.post("/{user_id}/disable")
def disable(user_id: int, db: Session = Depends(get_db)):
    return schemas.user_out(auth_service.disable(db, user_id))


@router.post("/{user_id}/enable")
def enable(user_id: int, db: Session = Depends(get_db)):
    return schemas.user_out(auth_service.enable(db, user_id))


@router.post("/{user_id}/reset-password")
def reset_password(user_id: int, db: Session = Depends(get_db)):
    user, temp_password = auth_service.reset_password(db, user_id)
    return {"user": schemas.user_out(user), "temp_password": temp_password}
