from fastapi import APIRouter, Depends, UploadFile
from sqlalchemy.orm import Session

from core.services import settings_service
from database.models import User
from web.api import schemas
from web.api.deps import get_db, require_root

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_root)])


@router.get("")
def get_settings_(db: Session = Depends(get_db)):
    return settings_service.get_all(db)


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


@router.post("/og-image", status_code=201)
async def upload_og_image(file: UploadFile, db: Session = Depends(get_db)):
    content = await file.read()
    path = settings_service.save_og_default(db, file.filename or "og.png", content)
    return {"og_default_image": path}


@router.delete("/og-image")
def delete_og_image(db: Session = Depends(get_db)):
    settings_service.clear_og_default(db)
    return {"og_default_image": ""}
