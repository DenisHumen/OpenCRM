"""Настройки рабочего пространства, нужные всем сотрудникам.

Полные настройки читает только root — и правильно: там и режим обслуживания, и
контакты, и адреса. Но валюта и то, как этот бизнес называет свои записи, нужны
на каждом экране любому сотруднику, иначе суммы показываются без обозначения, а
разделы называются чужими словами.

Отдельная точка вместо копирования этих полей в ответы заявок, досок и клиентов:
копий уже набралось три, и они начали расходиться.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.services import settings_service
from database.models import User
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("")
def workspace(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    return {
        "brand_name": values.get("brand_name", ""),
        "currency": values.get("currency", "USD"),
        "deal_term": values.get("deal_term", "deal"),
        # Сюда, а не в `GET /settings`: тот закрыт правом `settings.view`, а
        # открывать ли поток — знать должна каждая вкладка.
        "realtime_enabled": values.get("realtime_enabled", "1") == "1",
    }
