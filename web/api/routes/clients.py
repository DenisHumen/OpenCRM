from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import client_service, settings_service
from database.models import User
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from web.api import schemas
from web.api.deps import get_db, require_root, require_staff

router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("")
def list_clients(
    search: str | None = None,
    tag: str | None = None,
    manager_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    items, total = clients_repo.search(
        db, q=search, tag=tag, manager_id=manager_id, page=page, per_page=per_page
    )
    return schemas.paginated([schemas.client_out(c) for c in items], total, page, per_page)


@router.post("", status_code=201)
def create_client(
    payload: schemas.ClientIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client = client_service.create_client(db, payload.model_dump(), user)
    return schemas.client_out(client)


@router.get("/{client_id}")
def get_client(
    client_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client = client_service.get_client(db, client_id)
    notes, _total = clients_repo.list_notes(db, client_id, page=1, per_page=10)
    files = clients_repo.list_files(db, client_id)
    data = schemas.client_out(client)
    data["recent_notes"] = [schemas.note_out(n) for n in notes]
    data["files"] = [schemas.file_out(f) for f in files]
    # Заявки клиента прямо в карточке: без них «что мы для него делали» —
    # вопрос к памяти, а не к системе.
    data["deals"] = [schemas.deal_out(d) for d in deals_repo.for_client(db, client_id)]
    data["currency"] = settings_service.get_all(db).get("currency", "USD")
    return data


@router.patch("/{client_id}")
def update_client(
    client_id: int,
    payload: schemas.ClientPatchIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    return schemas.client_out(client_service.update_client(db, client_id, data))


@router.delete("/{client_id}")
def delete_client(
    client_id: int,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client_service.delete_client(db, client_id, user)
    return {"message": "Client deleted"}


@router.post("/{client_id}/restore")
def restore_client(
    client_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    return schemas.client_out(client_service.restore_client(db, client_id))


# --- заметки ---

@router.get("/{client_id}/notes")
def list_notes(
    client_id: int,
    kind: str | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client_service.get_client(db, client_id)
    notes, total = clients_repo.list_notes(
        db, client_id, page=page, per_page=per_page, kind=kind, deal_id=deal_id
    )
    return schemas.paginated([schemas.note_out(n) for n in notes], total, page, per_page)


@router.post("/{client_id}/notes", status_code=201)
def add_note(
    client_id: int,
    payload: schemas.NoteIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    note = client_service.add_note(
        db,
        client_id,
        user,
        payload.kind,
        payload.body,
        payload.happened_at,
        direction=payload.direction,
        deal_id=payload.deal_id,
    )
    return schemas.note_out(note)


@router.delete("/{client_id}/notes/{note_id}")
def delete_note(
    client_id: int,
    note_id: int,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client_service.delete_note(db, client_id, note_id, user)
    return {"message": "Note deleted"}


# --- файлы ---

@router.get("/{client_id}/files")
def list_files(
    client_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client_service.get_client(db, client_id)
    return {"items": [schemas.file_out(f) for f in clients_repo.list_files(db, client_id)]}


@router.post("/{client_id}/files", status_code=201)
async def upload_file(
    client_id: int,
    file: UploadFile,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    content = await file.read()
    record = client_service.add_file(
        db, client_id, user, file.filename or "file", content, file.content_type or ""
    )
    return schemas.file_out(record)


@router.get("/{client_id}/files/{file_id}/download")
def download_file(
    client_id: int,
    file_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    record = client_service.get_file(db, client_id, file_id)
    path = client_service.file_path_on_disk(record)
    if not path.exists():
        raise errors.NotFoundError("File is missing on disk", code="file_missing")
    return FileResponse(
        path,
        media_type=record.mime or "application/octet-stream",
        filename=record.original_name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{client_id}/files/{file_id}")
def delete_file(
    client_id: int,
    file_id: int,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    client_service.delete_file(db, client_id, file_id, user)
    return {"message": "File deleted"}
