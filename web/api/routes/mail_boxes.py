"""Веб-почта: ящик целиком — папки, письма, вложения, отправка (docs/bloki/32-pochtovyy-server.md).

Раздел закрыт блоком `mail` и правом `mail.view`, ящик — списком доступа
(`mail_access.proverit`): чужой ящик отвечает 404, как несуществующий. Писать
(отправка, черновик, загрузка вложения) — `mail.create` и право отправлять из
этого ящика. Флаги, перенос, удаление и папки — работа читающего со своей
почтой, им хватает `mail.view` (`tests/test_route_guards.py`).
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import deal_service, webmail_service
from database.models import User
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm, require_staff

router = APIRouter(
    prefix="/mail/boxes",
    tags=["mail"],
    dependencies=[Depends(require_module("mail")), Depends(require_perm("mail", "view"))],
)

PAPKA = Query(default="INBOX", min_length=1, max_length=500)
MAX_TELO = 5_000_000


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent: str | None = Field(default=None, max_length=500)


class FolderRenameIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    new_name: str = Field(min_length=1, max_length=100)


class UidsIn(BaseModel):
    folder: str = Field(default="INBOX", min_length=1, max_length=500)
    uids: list[int] = Field(min_length=1, max_length=webmail_service.MAX_UIDS)


class FlagsIn(UidsIn):
    seen: bool | None = None
    flagged: bool | None = None


class MoveIn(UidsIn):
    to: str = Field(min_length=1, max_length=500)


class MessageRefIn(BaseModel):
    folder: str = Field(default="INBOX", min_length=1, max_length=500)
    uid: int = Field(ge=1)
    #: Ящик исходного письма; без него — ящик отправки (тот же UID в другом ящике — другое письмо).
    box_id: int | None = Field(default=None, ge=1)


class ComposeIn(BaseModel):
    to: list[str] = Field(default_factory=list, max_length=200)
    cc: list[str] = Field(default_factory=list, max_length=200)
    bcc: list[str] = Field(default_factory=list, max_length=200)
    subject: str = Field(default="", max_length=998)
    #: При `html` можно не слать: текстовая часть соберётся из него (тело вдвое легче).
    text: str = Field(default="", max_length=MAX_TELO)
    html: str | None = Field(default=None, max_length=MAX_TELO)
    #: id загрузок из `POST /mail/boxes/uploads`.
    attachments: list[str] = Field(default_factory=list, max_length=50)
    reply_to: MessageRefIn | None = None
    #: Пересылка: вложения исходного письма едут с новым.
    forward_of: MessageRefIn | None = None
    #: Черновик, который это письмо заменяет: после отправки или сохранения он снимается.
    draft_uid: int | None = Field(default=None, ge=1)
    client_id: int | None = None
    deal_id: int | None = None


def _imya_v_zagolovke(imya: str) -> str:
    """`filename` — ASCII для старых клиентов, `filename*` — настоящее имя (RFC 5987)."""
    prostoe = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in imya)
    return f"attachment; filename=\"{prostoe or 'file'}\"; filename*=utf-8''{quote(imya)}"


def _fayl(data: bytes, imya: str, tip: str) -> Response:
    return Response(
        content=data,
        media_type=webmail_service.bezopasnyy_tip(tip),
        headers={
            "Content-Disposition": _imya_v_zagolovke(imya),
            "X-Content-Type-Options": "nosniff",
            # Вложение — чужой файл: открытый напрямую HTML или SVG не должен
            # исполниться от имени CRM.
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("")
def list_boxes(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Ящики сотрудника — из базы, без похода на сервер."""
    return webmail_service.yashchiki(db, user)


# --- папки ---

@router.get("/{account_id}/folders")
def list_folders(account_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    return webmail_service.papki(db, user, account_id)


@router.post("/{account_id}/folders", status_code=201)
def create_folder(
    account_id: int, payload: FolderIn, user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    return webmail_service.sozdat_papku(db, user, account_id, payload.name, payload.parent)


@router.patch("/{account_id}/folders")
def rename_folder(
    account_id: int,
    payload: FolderRenameIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    return webmail_service.pereimenovat_papku(db, user, account_id, payload.path, payload.new_name)


@router.delete("/{account_id}/folders")
def delete_folder(
    account_id: int,
    path: str = Query(min_length=1, max_length=500),
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    webmail_service.udalit_papku(db, user, account_id, path)
    return {"ok": True}


# --- письма ---

@router.get("/{account_id}/messages")
def list_messages(
    account_id: int,
    folder: str = PAPKA,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=webmail_service.MAX_PER_PAGE),
    q: str | None = Query(default=None, max_length=MAX_SEARCH),
    unread: bool | None = None,
    flagged: bool | None = None,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Новые сверху. `q` ищет по всему письму; `from:…` и `subject:…` — в своём поле."""
    return webmail_service.pisma(
        db, user, account_id, folder=folder, page=page, per_page=per_page,
        q=q, unread=unread, flagged=flagged,
    )


@router.get("/{account_id}/messages/{uid}")
def get_message(
    account_id: int,
    uid: int,
    folder: str = PAPKA,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Письмо целиком: HTML очищен, внешние картинки — в `data-src`. Ставит «прочитано»."""
    return webmail_service.pismo(db, user, account_id, uid, folder)


@router.get("/{account_id}/messages/{uid}/html")
def get_message_html(
    account_id: int,
    uid: int,
    folder: str = PAPKA,
    images: bool = False,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Письмо страницей для рамки — со своей политикой: без скриптов, картинки по кнопке."""
    stranitsa, politika = webmail_service.html_dokument(
        db, user, account_id, uid, folder, kartinki=images
    )
    return Response(
        content=stranitsa,
        media_type="text/html; charset=utf-8",
        headers={
            # Свои заголовки раньше общих: посредник ставит политику приложения
            # через `setdefault` и эту не перепишет (`web/middleware.py`).
            "Content-Security-Policy": politika,
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "no-store",
        },
    )


@router.get("/{account_id}/messages/{uid}/attachments/{part}")
def get_attachment(
    account_id: int,
    uid: int,
    part: str = Path(pattern=r"^\d{1,4}(\.\d{1,4}){0,10}$"),
    folder: str = PAPKA,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    chast = webmail_service.vlozhenie(db, user, account_id, uid, part, folder)
    return _fayl(chast.data, chast.name, chast.content_type)


@router.get("/{account_id}/messages/{uid}/raw")
def get_raw(
    account_id: int,
    uid: int,
    folder: str = PAPKA,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    syroe = webmail_service.iskhodnik(db, user, account_id, uid, folder)
    return _fayl(syroe, f"message-{uid}.eml", "message/rfc822")


@router.post("/{account_id}/messages/flags")
def set_flags(
    account_id: int, payload: FlagsIn, user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    return webmail_service.flagi(
        db, user, account_id, payload.folder, payload.uids, seen=payload.seen, flagged=payload.flagged
    )


@router.post("/{account_id}/messages/move")
def move_messages(
    account_id: int, payload: MoveIn, user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    return webmail_service.peremestit(db, user, account_id, payload.folder, payload.uids, payload.to)


@router.post("/{account_id}/messages/delete")
def delete_messages(
    account_id: int, payload: UidsIn, user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    """В Корзину; из самой Корзины — насовсем."""
    return webmail_service.udalit(db, user, account_id, payload.folder, payload.uids)


# --- написать ---

@router.post("/{account_id}/send")
def send(
    account_id: int,
    payload: ComposeIn,
    user: User = Depends(require_perm("mail", "create")),
    db: Session = Depends(get_db),
):
    deal_service.proverit_vidimost(db, user, payload.deal_id)
    return webmail_service.otpravit(db, user, account_id, payload.model_dump())


@router.post("/{account_id}/drafts", status_code=201)
def save_draft(
    account_id: int,
    payload: ComposeIn,
    user: User = Depends(require_perm("mail", "create")),
    db: Session = Depends(get_db),
):
    deal_service.proverit_vidimost(db, user, payload.deal_id)
    return webmail_service.chernovik(db, user, account_id, payload.model_dump())


@router.post("/uploads", status_code=201)
def upload(file: UploadFile, user: User = Depends(require_perm("mail", "create"))):
    """Вложение к будущему письму: живёт сутки во временном каталоге сотрудника."""
    return webmail_service.zagruzit(user, file.filename, file.content_type, file.file)
