from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.security import tokens
from core.services import storage_service
from core.utils import now_utc
from database.models import Client, ClientFile, ClientNote, User
from database.models.client import NOTE_KINDS
from database.models.user import ROLE_ROOT
from database.repositories import clients as clients_repo

# Внутренние документы клиентов: расширения, которые принимаем.
ALLOWED_CLIENT_FILE_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "csv",
    "zip", "rar", "7z", "jpg", "jpeg", "png", "webp", "gif", "svg", "psd",
    "ai", "fig", "sketch", "mp4", "webm", "mov",
}


def get_client(db: Session, client_id: int, include_deleted: bool = False) -> Client:
    client = clients_repo.get(db, client_id, include_deleted=include_deleted)
    if client is None:
        raise errors.NotFoundError("Client not found", code="client_not_found")
    return client


def create_client(db: Session, data: dict, author: User) -> Client:
    if not (data.get("name") or "").strip():
        raise errors.ValidationError("Name is required", code="name_required")
    client = Client(
        name=data["name"].strip(),
        company=(data.get("company") or "").strip(),
        phone=(data.get("phone") or "").strip(),
        email=(data.get("email") or "").strip(),
        messenger=(data.get("messenger") or "").strip(),
        tags=_normalize_tags(data.get("tags")),
        manager_id=data.get("manager_id") or author.id,
    )
    db.add(client)
    db.flush()
    return client


def update_client(db: Session, client_id: int, data: dict) -> Client:
    client = get_client(db, client_id)
    for field in ("name", "company", "phone", "email", "messenger"):
        if field in data and data[field] is not None:
            value = data[field].strip()
            if field == "name" and not value:
                raise errors.ValidationError("Name is required", code="name_required")
            setattr(client, field, value)
    if "tags" in data and data["tags"] is not None:
        client.tags = _normalize_tags(data["tags"])
    if "manager_id" in data:
        client.manager_id = data["manager_id"]
    client.updated_at = now_utc()
    return client


def delete_client(db: Session, client_id: int) -> None:
    client = get_client(db, client_id)
    client.deleted_at = now_utc()


def restore_client(db: Session, client_id: int) -> Client:
    client = get_client(db, client_id, include_deleted=True)
    client.deleted_at = None
    return client


def _normalize_tags(tags) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        parts = tags.split(",")
    else:
        parts = list(tags)
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return ",".join(dict.fromkeys(cleaned))  # без дубликатов, порядок сохранён


# --- заметки / история взаимодействий ---

def add_note(db: Session, client_id: int, author: User, kind: str, body: str, happened_at=None) -> ClientNote:
    get_client(db, client_id)
    if kind not in NOTE_KINDS:
        raise errors.ValidationError(f"kind must be one of {NOTE_KINDS}", code="bad_note_kind")
    if not body.strip():
        raise errors.ValidationError("Note body is required", code="body_required")
    note = ClientNote(
        client_id=client_id,
        author_id=author.id,
        kind=kind,
        body=body.strip(),
        happened_at=happened_at or now_utc(),
    )
    db.add(note)
    db.flush()
    return note


def delete_note(db: Session, client_id: int, note_id: int, actor: User) -> None:
    note = clients_repo.get_note(db, client_id, note_id)
    if note is None:
        raise errors.NotFoundError("Note not found", code="note_not_found")
    if actor.role != ROLE_ROOT and note.author_id != actor.id:
        raise errors.ForbiddenError("Only the author or root can delete a note", code="not_note_author")
    db.delete(note)


# --- файлы клиента (внутренние) ---

def _client_files_dir(client_id: int) -> Path:
    return get_settings().client_files_dir / str(client_id)


def file_path_on_disk(file: ClientFile) -> Path:
    ext = Path(file.original_name).suffix
    return _client_files_dir(file.client_id) / f"{file.file_uid}{ext}"


def add_file(db: Session, client_id: int, uploader: User, original_name: str, content: bytes, mime: str) -> ClientFile:
    get_client(db, client_id)
    ext = Path(original_name).suffix.lstrip(".").lower()
    if ext not in ALLOWED_CLIENT_FILE_EXTS:
        raise errors.ValidationError(f"File type .{ext} is not allowed", code="file_type_not_allowed")
    if len(content) > get_settings().max_upload_bytes:
        raise errors.ValidationError("File is too large", code="file_too_large")
    if not content:
        raise errors.ValidationError("File is empty", code="file_empty")
    if not storage_service.has_room_for(len(content)):
        raise errors.ValidationError(
            "Not enough free disk space on the server", code="disk_full"
        )

    file = ClientFile(
        client_id=client_id,
        uploaded_by=uploader.id,
        file_uid=tokens.new_file_uid(),
        original_name=Path(original_name).name[:255],
        mime=mime or "application/octet-stream",
        size_bytes=len(content),
    )
    db.add(file)
    db.flush()
    directory = _client_files_dir(client_id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path_on_disk(file).write_bytes(content)
    return file


def get_file(db: Session, client_id: int, file_id: int) -> ClientFile:
    file = clients_repo.get_file(db, client_id, file_id)
    if file is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    return file


def delete_file(db: Session, client_id: int, file_id: int) -> None:
    file = get_file(db, client_id, file_id)
    path = file_path_on_disk(file)
    db.delete(file)
    db.flush()
    if path.exists():
        path.unlink(missing_ok=True)
