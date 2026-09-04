"""Ключи доступа сайта на экране настроек. Устройство — `core/services/api_key_service.py`."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import api_key_service
from database.models import User
from database.models.api_key import SCOPES, STOCK_MODES
from database.repositories import api_keys as keys_repo
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/settings/api-keys", tags=["api-keys"])

manage = Depends(require_perm("settings", "manage"))


class ApiKeyIn(BaseModel):
    name: str
    scopes: list[str]
    warehouse_id: int | None = None
    days: int = api_key_service.DEFAULT_DAYS
    stock_mode: str = "bucket"
    few_threshold_milli: int = 5000
    rate_per_min: int = 120
    max_reserve_minutes: int = 1440
    ttl_sec: int = 60


class ApiKeyPatchIn(BaseModel):
    name: str | None = None
    stock_mode: str | None = None
    few_threshold_milli: int | None = None
    rate_per_min: int | None = None
    max_reserve_minutes: int | None = None
    ttl_sec: int | None = None


class RotateIn(BaseModel):
    grace_hours: int = api_key_service.GRACE_HOURS


@router.get("", dependencies=[manage])
def list_api_keys(db: Session = Depends(get_db)):
    """Все ключи, включая отозванные и просроченные — серым, но видны: пропавшая
    строка не отвечает на вопрос «а был ли у нас ключ для маркетплейса»."""
    return {
        "items": api_key_service.list_keys(db),
        "alive": api_key_service.alive_count(db),
        "scopes": list(SCOPES),
        "stock_modes": list(STOCK_MODES),
        "header": api_key_service.HEADER,
    }


@router.post("", status_code=201)
def create_api_key(
    payload: ApiKeyIn,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Выдать ключ. Строка ключа — в этом ответе и больше нигде."""
    key, raw = api_key_service.create(db, actor, payload.model_dump())
    otvet = api_key_service.key_out(key, keys_repo.scopes_of(db, key.id))
    otvet["key"] = raw
    return otvet


@router.patch("/{key_id}")
def update_api_key(
    key_id: int,
    payload: ApiKeyPatchIn,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    key = api_key_service.update(db, actor, key_id, payload.model_dump(exclude_unset=True))
    return api_key_service.key_out(key, keys_repo.scopes_of(db, key.id))


@router.post("/{key_id}/revoke")
def revoke_api_key(
    key_id: int,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    key = api_key_service.revoke(db, actor, key_id)
    return api_key_service.key_out(key, keys_repo.scopes_of(db, key.id))


@router.post("/{key_id}/rotate", status_code=201)
def rotate_api_key(
    key_id: int,
    payload: RotateIn | None = None,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Новый ключ с теми же полями; старый живёт ещё сутки, чтобы сайт не лёг."""
    key, raw = api_key_service.rotate(
        db, actor, key_id, grace_hours=payload.grace_hours if payload else api_key_service.GRACE_HOURS
    )
    otvet = api_key_service.key_out(key, keys_repo.scopes_of(db, key.id))
    otvet["key"] = raw
    return otvet
