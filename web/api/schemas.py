"""Pydantic-схемы запросов и сериализация моделей в JSON-ответы."""

from datetime import datetime

from pydantic import BaseModel, Field

from core.services import media_service
from database.models import (
    Board,
    Client,
    ClientFile,
    ClientNote,
    ShareLink,
    ShareView,
    User,
    Work,
)


# --- запросы ---

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class ProfileIn(BaseModel):
    name: str | None = None
    locale: str | None = None


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class ClientIn(BaseModel):
    name: str
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    messenger: str | None = None
    tags: str | list[str] | None = None
    manager_id: int | None = None


class ClientPatchIn(BaseModel):
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    messenger: str | None = None
    tags: str | list[str] | None = None
    manager_id: int | None = None


class NoteIn(BaseModel):
    kind: str = "note"
    body: str
    happened_at: datetime | None = None


class BoardIn(BaseModel):
    title: str
    description: str | None = None
    client_id: int | None = None


class BoardPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None
    client_id: int | None = None
    cover_work_id: int | None = None
    is_published: bool | None = None


class WorkPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None


class WorkOrderIn(BaseModel):
    work_ids: list[int]


class ShareIn(BaseModel):
    expires_at: datetime | None = None
    pin: str | None = None


class SharePatchIn(BaseModel):
    is_active: bool | None = None
    expires_at: datetime | None = None
    pin: str | None = None


class SettingsPatchIn(BaseModel):
    values: dict[str, str]


# --- сериализация ---

def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "locale": user.locale,
        "must_change_password": user.must_change_password,
        "created_at": _iso(user.created_at),
        "approved_at": _iso(user.approved_at),
    }


def client_out(client: Client) -> dict:
    return {
        "id": client.id,
        "name": client.name,
        "company": client.company,
        "phone": client.phone,
        "email": client.email,
        "messenger": client.messenger,
        "tags": [t for t in client.tags.split(",") if t],
        "manager_id": client.manager_id,
        "created_at": _iso(client.created_at),
        "updated_at": _iso(client.updated_at),
        "deleted_at": _iso(client.deleted_at),
    }


def note_out(note: ClientNote) -> dict:
    return {
        "id": note.id,
        "client_id": note.client_id,
        "author_id": note.author_id,
        "kind": note.kind,
        "body": note.body,
        "happened_at": _iso(note.happened_at),
        "created_at": _iso(note.created_at),
    }


def file_out(file: ClientFile) -> dict:
    return {
        "id": file.id,
        "client_id": file.client_id,
        "uploaded_by": file.uploaded_by,
        "original_name": file.original_name,
        "mime": file.mime,
        "size_bytes": file.size_bytes,
        "created_at": _iso(file.created_at),
        "download_url": f"/api/v1/clients/{file.client_id}/files/{file.id}/download",
    }


def board_out(board: Board, works_count: int | None = None) -> dict:
    data = {
        "id": board.id,
        "title": board.title,
        "description": board.description,
        "client_id": board.client_id,
        "cover_work_id": board.cover_work_id,
        "created_by": board.created_by,
        "is_published": board.is_published,
        "created_at": _iso(board.created_at),
        "updated_at": _iso(board.updated_at),
    }
    if works_count is not None:
        data["works_count"] = works_count
    return data


def work_out(work: Work) -> dict:
    return {
        "id": work.id,
        "board_id": work.board_id,
        "kind": work.kind,
        "title": work.title,
        "description": work.description,
        "sort_order": work.sort_order,
        "status": work.status,
        "original_name": work.original_name,
        "mime": work.mime,
        "size_bytes": work.size_bytes,
        "width": work.width,
        "height": work.height,
        "duration_sec": work.duration_sec,
        "blurhash": work.blurhash,
        "created_at": _iso(work.created_at),
        "media": media_service.work_media_urls(work) if work.status == "ready" else None,
    }


def share_out(link: ShareLink, stats: dict | None = None) -> dict:
    from core.services.share_service import public_url

    data = {
        "id": link.id,
        "board_id": link.board_id,
        "token": link.token,
        "url": public_url(link),
        "is_active": link.is_active,
        "expires_at": _iso(link.expires_at),
        "has_pin": link.pin_hash is not None,
        "created_by": link.created_by,
        "created_at": _iso(link.created_at),
        "revoked_at": _iso(link.revoked_at),
    }
    if stats:
        data.update(stats)
    return data


def view_out(view: ShareView) -> dict:
    return {
        "id": view.id,
        "viewed_at": _iso(view.viewed_at),
        "visitor": view.ip_hash[:12],  # анонимный идентификатор посетителя
        "user_agent": view.user_agent,
    }


def media_file_out(item: dict) -> dict:
    """Строка менеджера файлов: даты — в ISO, остальное как есть."""
    return {
        **item,
        "created_at": _iso(item.get("created_at")),
        "last_viewed_at": _iso(item.get("last_viewed_at")),
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def paginated(items: list[dict], total: int, page: int, per_page: int) -> dict:
    return {"items": items, "total": total, "page": page, "per_page": per_page}
