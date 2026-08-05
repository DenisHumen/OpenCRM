"""Воронка: этапы и готовые наборы под отрасль.

Читать этапы должен любой сотрудник — без них не нарисовать доску. Менять
воронку — только root: это структура работы всей студии, и случайное
переименование задевает всех сразу.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import pipeline_service
from database.models import User
from web.api import schemas
from web.api.deps import get_db, require_root, require_staff

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class StageIn(BaseModel):
    name: str
    kind: str = "open"
    after: str | None = None


class StagePatchIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    color: str | None = None
    sort_order: int | None = None


class PresetIn(BaseModel):
    preset: str


@router.get("/stages")
def list_stages(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    return {"items": [schemas.stage_out(s) for s in pipeline_service.list_stages(db)]}


@router.get("/presets")
def list_presets(_: User = Depends(require_root)):
    return {
        "items": [
            {"key": key, "name": preset["name"], "hint": preset["hint"],
             "stages": [name for _k, name, _kind in preset["stages"]]}
            for key, preset in pipeline_service.PRESETS.items()
        ]
    }


@router.post("/preset")
def apply_preset(
    payload: PresetIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    stages = pipeline_service.apply_preset(db, payload.preset)
    return {"items": [schemas.stage_out(s) for s in stages]}


@router.post("/stages", status_code=201)
def create_stage(
    payload: StageIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    stage = pipeline_service.create_stage(db, payload.name, payload.kind, payload.after)
    return schemas.stage_out(stage)


@router.patch("/stages/{key}")
def update_stage(
    key: str,
    payload: StagePatchIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    stage = pipeline_service.update_stage(db, key, payload.model_dump(exclude_unset=True))
    return schemas.stage_out(stage)


@router.delete("/stages/{key}")
def archive_stage(
    key: str,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """Убрать этап с доски. Сделки из него переезжают на первый открытый."""
    pipeline_service.archive_stage(db, key)
    return {"message": "Stage archived"}
