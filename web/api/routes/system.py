from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.services import audit_service, files_service, maintenance_service, storage_service
from database.models import User
from database.models.audit import SOURCE_MANUAL
from web.api import schemas
from web.api.deps import get_db, require_perm, require_staff

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/storage")
def storage_status(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Место на диске. Видят все сотрудники: именно они загружают файлы."""
    return storage_service.status(db)


@router.post("/storage/purge")
def purge_storage(
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Окончательно удалить мягко удалённые доски и клиентов вместе с файлами.

    Единственное место в системе, где данные исчезают безвозвратно, — поэтому
    оно пишется в журнал. Раньше не писалось вовсе: человек освобождал диск, а
    что при этом ушло, выяснить потом было не по чему.
    """
    result = maintenance_service.purge_soft_deleted(db, older_than_days=0)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_STORAGE_PURGED,
        entity_type=audit_service.ENTITY_MODULE,
        entity_label="storage",
        after=(
            f"досок {result['boards']}, работ {result['works']}, "
            f"клиентов {result['clients']}, заявок {result['deals']}, "
            f"файлов {result['client_files']}"
        ),
    )
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
