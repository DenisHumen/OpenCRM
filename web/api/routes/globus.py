"""Блок «Глобус»: планета одним ответом и докачка подробных очертаний.

Роут тонкий: где чья точка и какие связи — в `core/services/globus_service.py`,
докачка — в `globus_karta_service.py`.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.services import globus_karta_service, globus_service, settings_service
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
    return globus_karta_service.sostoyanie(hotim)


@router.post("/detail")
def detail_start(
    _: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)
):
    """Включить докачку и попробовать прямо сейчас."""
    settings_service.update(db, {KLYUCH_PODROBNO: "1"})
    globus_karta_service.nachat()
    return globus_karta_service.sostoyanie(True)


@router.delete("/detail")
def detail_stop(
    _: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)
):
    """Выключить докачку и убрать скачанное: планета вернётся к вшитому."""
    settings_service.update(db, {KLYUCH_PODROBNO: "0"})
    globus_karta_service.zabyt()
    return globus_karta_service.sostoyanie(False)


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
