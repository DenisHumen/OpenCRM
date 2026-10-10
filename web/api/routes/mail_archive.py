"""Архив писем в CRM: копия каждого письма ящиков (docs/bloki/32-pochtovyy-server.md, «Архив писем в CRM»).

Читается из базы и storage, без похода на почтовый сервер: копия нужна ровно
тогда, когда сервера уже нет. Кто видит ящик (`mail_access`), видит и его
копию; токен с областью — только ящики области. Удалять копию и решать, вести
ли её, — `mail.manage`.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import mail_archive
from database.models import User
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm, require_staff
from web.api.routes import mail_boxes

router = APIRouter(
    prefix="/mail/archive",
    tags=["mail"],
    dependencies=[Depends(require_module("mail")), Depends(require_perm("mail", "view"))],
)


class ArchiveFlagIn(BaseModel):
    archive: bool


@router.get("")
def list_archive(
    box: int | None = None,
    folder: str | None = Query(default=None, max_length=500),
    q: str | None = Query(default=None, max_length=MAX_SEARCH),
    from_: str | None = Query(default=None, alias="from", max_length=MAX_SEARCH),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    removed: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=mail_archive.MAX_PER_PAGE),
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Новые сверху (по времени прихода). `q` — тема, отправитель, адресаты; `removed` — удалённые на сервере."""
    return mail_archive.spisok(
        db, user, box=box, folder=folder, q=q, from_=from_, date_from=date_from, date_to=date_to,
        removed=removed, page=page, per_page=per_page,
    )


@router.get("/stats")
def archive_stats(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """По ящикам: писем, байт, удалено на сервере, когда синхронизировано."""
    return mail_archive.svodka(db, user)


@router.put("/boxes/{account_id}")
def set_box_archive(
    account_id: int,
    payload: ArchiveFlagIn,
    user: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    """Вести ли копию ящика. Выключение копию не стирает."""
    return mail_archive.vklyuchit(db, user, account_id, payload.archive)


@router.delete("/boxes/{account_id}")
def purge_box_archive(
    account_id: int,
    user: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    """Стереть копию ящика целиком: письма в CRM и их файлы. Почту на сервере не трогает."""
    mail_archive.proverit_yashchik(db, user, account_id)
    return {"ok": True, "deleted": mail_archive.ochistit_yashchik(db, account_id)}


@router.get("/{archive_id}")
def get_archived(archive_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Копия целиком: тела (HTML уже очищен) и список вложений."""
    return mail_archive.pismo(db, user, archive_id)


@router.get("/{archive_id}/html")
def get_archived_html(
    archive_id: int,
    images: bool = False,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Страница копии для рамки — с той же политикой, что у веб-почты."""
    stranitsa, politika = mail_archive.html_dokument(db, user, archive_id, kartinki=images)
    return Response(
        content=stranitsa,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": politika,
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "no-store",
        },
    )


@router.get("/{archive_id}/raw")
def get_archived_raw(archive_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Письмо как пришло (`.eml`), байт в байт."""
    return mail_boxes._fayl(mail_archive.syroe(db, user, archive_id), f"message-{archive_id}.eml", "message/rfc822")


@router.get("/{archive_id}/attachments/{part}")
def get_archived_attachment(
    archive_id: int,
    part: str = Path(pattern=r"^\d{1,4}(\.\d{1,4}){0,10}$"),
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    chast = mail_archive.vlozhenie(db, user, archive_id, part)
    return mail_boxes._fayl(chast.data, chast.name, chast.content_type)


@router.delete("/{archive_id}")
def delete_archived(
    archive_id: int,
    user: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    """Удалить копию из CRM. На сервере письмо (если оно там ещё есть) остаётся."""
    mail_archive.udalit(db, user, archive_id)
    return {"ok": True}
