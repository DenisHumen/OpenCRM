from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import maintenance_mode, settings_service, site_logo_service
from database.models import User
from web.api import schemas
from web.api.deps import get_current_user, get_db, require_root

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_root)])


class MaintenanceIn(BaseModel):
    enabled: bool
    note: str = ""


@router.get("")
def get_settings_(db: Session = Depends(get_db)):
    return settings_service.get_all(db)


@router.get("/maintenance")
def maintenance_status(db: Session = Depends(get_db)):
    return maintenance_mode.state(db)


@router.post("/maintenance")
def set_maintenance(
    payload: MaintenanceIn,
    # get_current_user, а не require_root: root'ом уже сделала зависимость
    # роутера, а здесь нужно только имя — кто закрыл сайт.
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Закрыть сайт на работы или открыть обратно. Доступно только root."""
    return maintenance_mode.set_mode(db, payload.enabled, payload.note, user.name or user.email)


@router.patch("")
def update_settings(payload: schemas.SettingsPatchIn, db: Session = Depends(get_db)):
    return settings_service.update(db, payload.values)


@router.post("/logo", status_code=201)
async def upload_logo(file: UploadFile, db: Session = Depends(get_db)):
    content = await file.read()
    path = settings_service.save_logo(db, file.filename or "logo.png", content)
    return {"brand_logo_path": path}


@router.delete("/logo")
def delete_logo(db: Session = Depends(get_db)):
    settings_service.clear_logo(db)
    return {"brand_logo_path": ""}


@router.post("/site-logo", status_code=201)
async def upload_site_logo(file: UploadFile, db: Session = Depends(get_db)):
    content = await file.read()
    path = settings_service.save_site_logo(db, file.filename or "site-logo.png", content)
    return {"studio_site_logo": path}


@router.post("/site-logo/fetch", status_code=201)
def fetch_site_logo(db: Session = Depends(get_db)):
    """Пробует достать логотип с сайта, указанного в настройках.

    Не получилось — `422 logo_fetch_failed`, и логотип загружается вручную.
    """
    site_url = settings_service.get_all(db).get("studio_site_url", "")
    content, filename = site_logo_service.fetch_logo(site_url)
    path = settings_service.save_site_logo(db, filename, content)
    return {"studio_site_logo": path}


@router.delete("/site-logo")
def delete_site_logo(db: Session = Depends(get_db)):
    settings_service.clear_site_logo(db)
    return {"studio_site_logo": ""}


@router.post("/og-image", status_code=201)
async def upload_og_image(file: UploadFile, db: Session = Depends(get_db)):
    content = await file.read()
    path = settings_service.save_og_default(db, file.filename or "og.png", content)
    return {"og_default_image": path}


@router.delete("/og-image")
def delete_og_image(db: Session = Depends(get_db)):
    settings_service.clear_og_default(db)
    return {"og_default_image": ""}
