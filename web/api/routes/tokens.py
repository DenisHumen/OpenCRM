"""Токены сотрудников на экране «API». Устройство — `core/services/token_service.py`."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import token_service
from database.models import User
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/tokens", tags=["tokens"])

manage = Depends(require_perm("settings", "manage"))


class TokenIn(BaseModel):
    name: str
    user_id: int | None = None
    days: int = token_service.SROK_UMOLCHANIE
    tolko_chtenie: bool = False


@router.get("", dependencies=[manage])
def list_tokens(db: Session = Depends(get_db)):
    """Все токены, отозванные и истёкшие тоже: «был ли токен у агента» должно иметь ответ."""
    return {
        "items": token_service.spisok(db),
        "alive": token_service.zhivyh(db),
        "sroki": list(token_service.SROKI),
        "v_minutu": token_service.V_MINUTU,
    }


@router.post("", status_code=201)
def create_token(
    payload: TokenIn,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Выдать токен. Строка — в этом ответе и больше нигде."""
    otvet, raw = token_service.vypustit(db, actor, payload.model_dump())
    otvet["token"] = raw
    return otvet


@router.post("/{token_id}/revoke")
def revoke_token(
    token_id: int,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    return token_service.otozvat(db, actor, token_id)
