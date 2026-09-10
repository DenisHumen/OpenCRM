"""Файлы: дерево всего, что лежит на диске, свои папки и своя загрузка.

Раздел раньше жил в системных настройках под `settings.manage` и показывал
только работы досок плоским списком. Стал блоком со своими правами: смотреть
дерево, приносить файлы, убирать их и (в следующем заходе) выпускать наружу по
ссылке. ЧТО видно в дереве, решают права на разделы, из которых файлы пришли, —
разбор в `core/services/fayly_service.py`.
"""

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import fayly_service, storage_service
from database.models import User
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/tree")
def derevo(
    actor: User = Depends(require_perm("files", "view")),
    db: Session = Depends(get_db),
):
    """Дерево со счётчиками и состояние диска: полоса занятого места внизу
    панели берётся отсюда же, а не вторым запросом."""
    return {"tree": fayly_service.derevo(db, actor), "storage": storage_service.status(db)}


@router.get("")
def spisok(
    node: str = Query(default=fayly_service.VSE, max_length=64),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    actor: User = Depends(require_perm("files", "view")),
    db: Session = Depends(get_db),
):
    return fayly_service.soderzhimoe(db, actor, node, page, per_page)


class PapkaIn(BaseModel):
    name: str = Field(max_length=120)
    parent_id: int | None = None


@router.post("/folders", status_code=201)
def zavesti_papku(
    payload: PapkaIn,
    actor: User = Depends(require_perm("files", "create")),
    db: Session = Depends(get_db),
):
    papka = fayly_service.sozdat_papku(db, actor, payload.parent_id, payload.name)
    return {"id": papka.id, "name": papka.name, "parent_id": papka.parent_id}


@router.delete("/folders/{folder_id}")
def snyat_papku(
    folder_id: int,
    actor: User = Depends(require_perm("files", "delete")),
    db: Session = Depends(get_db),
):
    fayly_service.udalit_papku(db, actor, folder_id)
    return {"message": "Folder deleted", "storage": storage_service.status(db)}


@router.post("", status_code=201)
async def zalit(
    file: UploadFile,
    folder_id: int | None = Query(default=None),
    actor: User = Depends(require_perm("files", "create")),
    db: Session = Depends(get_db),
):
    """Принять файл в свою папку.

    Обычной формой, а не кусками: кусочная загрузка — это другой способ класть
    байты на диск, и второй такой в системе означал бы две приёмки с разными
    проверками. Понадобится она — понадобится всем, кто грузит видео, включая
    доски; это отдельная работа, а не свойство этого экрана.
    """
    content = await file.read()
    fayl = fayly_service.prinyat(db, actor, folder_id, file.filename or "file", content)
    return {
        "id": f"stored:{fayl.id}",
        "name": fayl.original_name,
        "size_bytes": fayl.size_bytes,
        "folder_id": fayl.folder_id,
    }


@router.get("/{file_id}/download")
def skachat(
    file_id: int,
    _: User = Depends(require_perm("files", "view")),
    db: Session = Depends(get_db),
):
    """Отдать свой файл сотруднику. Чужие файлы отсюда не отдаются: у них есть
    своя ручка у своей карточки, и вторая дорога к тем же байтам разошлась бы
    с первой на первой же правке прав."""
    fayl = fayly_service.poluchit(db, file_id)
    return FileResponse(
        fayly_service.fayl_na_diske(fayl),
        media_type=fayl.mime,
        filename=fayl.original_name,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/{file_id}")
def udalit(
    file_id: int,
    actor: User = Depends(require_perm("files", "delete")),
    db: Session = Depends(get_db),
):
    fayly_service.udalit(db, actor, file_id)
    return {"message": "File deleted", "storage": storage_service.status(db)}
