"""Змейка со страницы обслуживания: приём счёта и таблица лидеров.

Оба маршрута публичные и без входа в систему: играют посетители сайта, пока он
обновляется, и никакого аккаунта у них нет. Поэтому здесь нет ни одного действия
над данными студии — только своя таблица результатов.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import arcade_service
from web.api.deps import client_ip, get_db

router = APIRouter(prefix="/arcade", tags=["arcade"])


class ScoreIn(BaseModel):
    name: str = ""
    score: int
    played_ms: int = 0


@router.get("/leaderboard")
def leaderboard(db: Session = Depends(get_db)):
    return {"items": arcade_service.leaderboard(db)}


@router.post("/scores", status_code=201)
def submit_score(payload: ScoreIn, request: Request, db: Session = Depends(get_db)):
    return arcade_service.submit(
        db, payload.name, payload.score, payload.played_ms, client_ip(request)
    )
