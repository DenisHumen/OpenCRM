"""Переписка с клиентами через бота фирмы: пока настройки подключения.

Все ручки закрыты блоком ``telegram`` — выключили блок, и раздела нет ни в
меню, ни в API. Это правило проекта, а не удобство: спрятать пункт меню
недостаточно, адрес остаётся рабочим и лежит в закладках.

Приём сообщений от телеграма появится отдельной ручкой и БЕЗ зависимости от
блока — по той же причине, по какой так сделан вебхук АТС: телеграм не умеет
узнать, что канал выключили, и продолжит доставлять; отвечать ему пятисоткой
значило бы копить у него очередь повторов.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import telegram_service
from database.models import User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/telegram",
    tags=["telegram"],
    dependencies=[Depends(require_module("telegram"))],
)


class NastroykiVhod(BaseModel):
    """Что можно задать с экрана.

    Все поля необязательные, и это не небрежность: экран сохраняет по разделам,
    а пустой токен означает «не меняй». Отправить обратно значение, которого он
    не получал, экран не может — токен ему не отдают.
    """

    token: str | None = None
    digest_chat: str | None = None
    bot_username: str | None = None


@router.get("/settings")
def nastroyki(
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    """Состояние подключения. Токен наружу не отдаётся — только его хвост."""
    return telegram_service.nastroyki(db)


@router.put("/settings")
def zadat_nastroyki(
    data: NastroykiVhod,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    # `exclude_unset` важен: без него незаполненные поля приехали бы как `None`
    # и стёрли то, чего человек не трогал. Ровно та же беда, что у частичной
    # правки карточки.
    return telegram_service.zadat(db, data.model_dump(exclude_unset=True))


@router.delete("/settings")
def otklyuchit(
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    """Отключить бота. Переписка остаётся — отключение про связь, а не про данные."""
    return telegram_service.otklyuchit(db)
