from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.services import files_service, maintenance_service, storage_service
from database.models import User
from web.api import schemas
from web.api.deps import get_db, require_perm, require_staff

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/storage")
def storage_status(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Место на диске. Видят все сотрудники: именно они загружают файлы."""
    return storage_service.status(db)


@router.post("/storage/purge")
def purge_storage(_: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)):
    """Окончательно удалить мягко удалённые доски и клиентов вместе с файлами."""
    result = maintenance_service.purge_soft_deleted(db, older_than_days=0)
    result["storage"] = storage_service.status(db)
    return result


@router.get("/files")
def list_media_files(_: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)):
    """Менеджер файлов (root): все медиафайлы работ с размером и датами."""
    items = [schemas.media_file_out(f) for f in files_service.list_media_files(db)]
    return {"items": items, "storage": storage_service.status(db)}


@router.delete("/files/{work_id}")
def delete_media_file(work_id: int, _: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)):
    """Удалить одну работу вместе с файлами (root)."""
    files_service.delete_media_file(db, work_id)
    return {"message": "File deleted", "storage": storage_service.status(db)}
