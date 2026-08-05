"""Почта: ящики фирмы, письма и отправка.

Ящик — настройка уровня фирмы, поэтому его CRUD и проверку соединения закрывает
`require_root`; читать и отправлять письма может любой сотрудник. Весь роутер
закрыт `require_module("mail")`: выключенный блок отвечает 403 `module_disabled`.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import mail_service
from database.models import User
from database.models.mail import MAIL_DIRECTIONS
from database.repositories import mail as mail_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_root, require_staff

router = APIRouter(
    prefix="/mail",
    tags=["mail"],
    dependencies=[Depends(require_staff), Depends(require_module("mail"))],
)


class MailAccountIn(BaseModel):
    title: str | None = None
    address: str
    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True
    login: str | None = None
    # Пароль приходит только внутрь и никогда не возвращается наружу.
    password: str | None = None
    is_active: bool = True


class MailAccountPatchIn(BaseModel):
    title: str | None = None
    address: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_ssl: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_ssl: bool | None = None
    login: str | None = None
    password: str | None = None
    is_active: bool | None = None


class MailSendIn(BaseModel):
    to: list[str] = Field(min_length=1)
    subject: str = ""
    body: str
    account_id: int | None = None
    client_id: int | None = None
    deal_id: int | None = None


class MailReadIn(BaseModel):
    is_read: bool = True


# --- ящики (root) ---

@router.get("/accounts")
def list_accounts(
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    return {"items": [schemas.mail_account_out(a) for a in mail_repo.list_accounts(db)]}


@router.post("/accounts", status_code=201)
def create_account(
    payload: MailAccountIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    account = mail_service.create_account(db, payload.model_dump())
    return schemas.mail_account_out(account)


@router.patch("/accounts/{account_id}")
def update_account(
    account_id: int,
    payload: MailAccountPatchIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    account = mail_service.update_account(db, account_id, payload.model_dump(exclude_unset=True))
    return schemas.mail_account_out(account)


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    mail_service.delete_account(db, account_id)
    return {"message": "Mail account deleted"}


@router.post("/accounts/{account_id}/check")
def check_account(
    account_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """Проверка настроек: вход по IMAP и по SMTP. Ошибка возвращается текстом."""
    return mail_service.check_account(db, account_id)


@router.post("/accounts/{account_id}/sync")
def sync_account(
    account_id: int,
    db: Session = Depends(get_db),
):
    """Ручная синхронизация. Доступна любому сотруднику: это чтение почты фирмы,
    а не правка настроек — ждать root'а ради свежих писем незачем."""
    return mail_service.sync_account(db, account_id)


# --- письма ---

@router.get("/messages")
def list_messages(
    account_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    direction: str | None = Query(default=None, pattern="^(in|out)$"),
    unread: bool | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    items, total = mail_repo.search_messages(
        db,
        account_id=account_id,
        client_id=client_id,
        deal_id=deal_id,
        direction=direction if direction in MAIL_DIRECTIONS else None,
        unread=unread,
        q=search,
        page=page,
        per_page=per_page,
    )
    return schemas.paginated([schemas.mail_message_out(m) for m in items], total, page, per_page)


@router.get("/messages/{message_id}")
def get_message(message_id: int, db: Session = Depends(get_db)):
    message = mail_repo.get_message(db, message_id)
    if message is None:
        raise errors.NotFoundError("Message not found", code="mail_message_not_found")
    return schemas.mail_message_out(message, with_body=True)


@router.post("/messages/{message_id}/read")
def set_read(message_id: int, payload: MailReadIn, db: Session = Depends(get_db)):
    return schemas.mail_message_out(mail_service.mark_read(db, message_id, payload.is_read))


@router.post("/send", status_code=201)
def send(
    payload: MailSendIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    message = mail_service.send_message(
        db,
        user,
        to_addrs=payload.to,
        subject=payload.subject,
        body_text=payload.body,
        account_id=payload.account_id,
        client_id=payload.client_id,
        deal_id=payload.deal_id,
    )
    return schemas.mail_message_out(message, with_body=True)
