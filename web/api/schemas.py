"""Pydantic-схемы запросов и сериализация моделей в JSON-ответы."""

from datetime import datetime

from pydantic import BaseModel, Field

from core.services import deal_service, media_service
from core.utils import is_online
from database.models import (
    Board,
    Client,
    ClientFile,
    ClientNote,
    Deal,
    DealStageChange,
    Document,
    PipelineStage,
    ShareLink,
    ShareView,
    Task,
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


class DealIn(BaseModel):
    title: str
    client_id: int
    manager_id: int | None = None
    stage: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    # Деньги — целыми в минимальных единицах (копейки, центы). int, а не float:
    # на дробных округление вылезает всегда, и сумма колонки канбана расходится
    # с суммой карточек.
    amount: int | None = None
    prepaid: int | None = None


class DealPatchIn(BaseModel):
    title: str | None = None
    client_id: int | None = None
    manager_id: int | None = None
    stage: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    lost_reason: str | None = None
    amount: int | None = None
    prepaid: int | None = None


class DealMoveIn(BaseModel):
    stage: str
    # Позиция внутри колонки. Не задана — карточка встаёт в конец.
    sort_order: int | None = None
    lost_reason: str | None = None


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
    # Входящее или исходящее — у звонка и письма. У заметки пусто.
    direction: str = ""
    deal_id: int | None = None


class BoardIn(BaseModel):
    title: str
    description: str | None = None
    client_id: int | None = None
    # Заявка, ради которой доска сделана. Необязательная: доски существовали
    # до заявок и обязаны работать без них.
    deal_id: int | None = None


class BoardPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None
    client_id: int | None = None
    deal_id: int | None = None
    cover_work_id: int | None = None
    is_published: bool | None = None


class WorkPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None
    project_url: str | None = None
    # какой фрагмент длинной работы видно на витрине; null — от верха
    preview_focus: float | None = None


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


class RoleUpdateIn(BaseModel):
    role: str


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
        "avatar_url": user.avatar_path or None,
        "last_seen_at": _iso(user.last_seen_at),
        "is_online": is_online(user.last_seen_at),
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


def document_out(document: Document) -> dict:
    import json as _json

    try:
        payload = _json.loads(document.payload or "{}")
    except ValueError:
        payload = {}
    return {
        "id": document.id,
        "number": document.number,
        "kind": document.kind,
        "locale": document.locale,
        "status": document.status,
        "client_id": document.client_id,
        "deal_id": document.deal_id,
        "payload": payload,
        "created_at": _iso(document.created_at),
        "updated_at": _iso(document.updated_at),
    }


def task_out(task: Task, assignee_name: str | None = None) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        # Время уходит в ISO с явным Z: без него браузер разберёт его как
        # местное, и срок уедет на величину смещения.
        "due_at": _iso(task.due_at),
        "assignee_id": task.assignee_id,
        "assignee_name": assignee_name,
        "client_id": task.client_id,
        "deal_id": task.deal_id,
        "is_done": task.done_at is not None,
        "done_at": _iso(task.done_at),
        "created_at": _iso(task.created_at),
    }


def stage_out(stage: PipelineStage) -> dict:
    return {
        "key": stage.key,
        "name": stage.name,
        "kind": stage.kind,
        "sort_order": stage.sort_order,
        "color": stage.color,
        "is_archived": stage.is_archived,
    }


def deal_out(deal: Deal, client_name: str | None = None, manager: User | None = None) -> dict:
    return {
        "id": deal.id,
        "title": deal.title,
        "client_id": deal.client_id,
        # Имя клиента и ответственного кладём в ответ, а не заставляем фронт
        # добирать их отдельным запросом на каждую карточку канбана. Без имени
        # ответственного доска не отвечает на первый же вопрос: кто это ведёт.
        "client_name": client_name,
        "manager_id": deal.manager_id,
        "manager_name": manager.name if manager else None,
        "manager_avatar": (manager.avatar_path or None) if manager else None,
        "stage": deal.stage,
        "sort_order": deal.sort_order,
        "description": deal.description,
        # Остаток и признак оплаты считаются, а не хранятся: лишнее поле в базе
        # разошлось бы с суммой и предоплатой при первой же правке.
        **deal_service.money_of(deal),
        "due_at": _iso(deal.due_at),
        "lost_reason": deal.lost_reason,
        "closed_at": _iso(deal.closed_at),
        "created_at": _iso(deal.created_at),
        "updated_at": _iso(deal.updated_at),
    }


def stage_change_out(change: DealStageChange, author_name: str | None = None) -> dict:
    return {
        "id": change.id,
        "from_stage": change.from_stage,
        "to_stage": change.to_stage,
        "author_name": author_name,
        "changed_at": _iso(change.changed_at),
    }


def note_out(note: ClientNote) -> dict:
    return {
        "id": note.id,
        "client_id": note.client_id,
        "author_id": note.author_id,
        "kind": note.kind,
        "direction": note.direction or None,
        "deal_id": note.deal_id,
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
        "deal_id": board.deal_id,
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
        "project_url": work.project_url or "",
        "sort_order": work.sort_order,
        "status": work.status,
        "original_name": work.original_name,
        "mime": work.mime,
        "size_bytes": work.size_bytes,
        "width": work.width,
        "height": work.height,
        "duration_sec": work.duration_sec,
        "blurhash": work.blurhash,
        # видимый фрагмент длинной работы: null — от верха
        "preview_focus": work.preview_focus,
        "created_at": _iso(work.created_at),
        "media": media_service.work_media_urls(work) if work.status == "ready" else None,
        # кандидаты по ширине для плитки витрины (см. media_service.work_srcset)
        "srcset": media_service.work_srcset(work) if work.status == "ready" else "",
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
