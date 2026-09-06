"""Блок «Глобус»: планета одним ответом и докачка подробностей.

Роут тонкий: где чья точка и какие связи — в `core/services/globus_service.py`,
очертания — в `globus_karta_service.py`, улицы и дома — в
`globus_ulitsy_service.py`.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.services import (
    globus_karta_service,
    globus_service,
    globus_ulitsy_service,
    settings_service,
)
from database.models import User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/globe",
    tags=["globe"],
    dependencies=[Depends(require_module("globe"))],
)

#: Настройка «качать подробные очертания»: «1» — да.
KLYUCH_PODROBNO = "globe_detail"


@router.get("")
def globe(user: User = Depends(require_perm("globe", "view")), db: Session = Depends(get_db)):
    """Точки, связи, счётчики и разрез по странам — одним ответом."""
    return globus_service.kartina(db, user)


@router.get("/detail")
def detail_status(
    _: User = Depends(require_perm("globe", "view")), db: Session = Depends(get_db)
):
    """Что с подробными очертаниями. Заодно повод начать попытку — служба сама
    решает, не рано ли."""
    hotim = settings_service.get_all(db).get(KLYUCH_PODROBNO, "0") == "1"
    return {**globus_karta_service.sostoyanie(hotim), "streets": globus_ulitsy_service.zapas()}


@router.post("/detail")
def detail_start(
    _: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)
):
    """Включить докачку и попробовать прямо сейчас."""
    settings_service.update(db, {KLYUCH_PODROBNO: "1"})
    globus_karta_service.nachat()
    return {**globus_karta_service.sostoyanie(True), "streets": globus_ulitsy_service.zapas()}


@router.delete("/detail")
def detail_stop(
    _: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)
):
    """Выключить докачку и убрать скачанное: планета вернётся к вшитому."""
    settings_service.update(db, {KLYUCH_PODROBNO: "0"})
    globus_karta_service.zabyt()
    # Улицы уходят вместе с очертаниями: тумблер один, и «выключил докачку, а
    # в интернет всё равно ходит» — это не выключил.
    globus_ulitsy_service.zabyt()
    return {**globus_karta_service.sostoyanie(False), "streets": globus_ulitsy_service.zapas()}


@router.get("/map")
def globe_map(_: User = Depends(require_perm("globe", "view"))):
    """Скачанные очертания. Пустой список — их нет, и экран рисует вшитые.

    Пустой список, а не отказ: «подробных нет» — обычное состояние установки
    без интернета, а не беда, о которой надо сообщать красным.
    """
    put = globus_karta_service.fayl_kart()
    if not put.exists():
        return {"rings": []}
    return FileResponse(put, media_type="application/json")


@router.get("/streets")
def globe_streets(
    x: int = Query(default=0, ge=0),
    y: int = Query(default=0, ge=0),
    _: User = Depends(require_perm("globe", "view")),
    db: Session = Depends(get_db),
):
    """Улицы и дома одной плитки под сильным приближением.

    Плитка, которой нет, — обычное состояние, а не отказ: экран рисует то, что
    уже лежит, и спрашивает снова. Запрос наружу уходит только при включённой
    докачке — тумблер тот же, что у подробных очертаний.
    """
    hotim = settings_service.get_all(db).get(KLYUCH_PODROBNO, "0") == "1"
    return globus_ulitsy_service.plitka(globus_ulitsy_service.PLITKA_Z, x, y, hotim)
