"""Почта: ящики фирмы, письма и отправка.

Ящик заводит и правит управляющий почтой (`mail.manage`, как `/mail/admin`), в
пределах своего токена. Читать письма даёт `mail.view` (проверяется на уровне
роутера), отправлять — `mail.create`. Весь роутер закрыт
`require_module("mail")`: выключенный блок отвечает 403 `module_disabled`
раньше, чем зайдёт речь о правах.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import deal_service, mail_access, mail_service
from database.models import MailMessage, User
from database.models.mail import MAIL_DIRECTIONS
from database.repositories import mail as mail_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm

# Блок — первым: выключённая почта отвечает «блок выключен», а не «нет права».
# `require_perm` проверяет то же самое и сам, но порядок в списке — это то, что
# читает следующий человек.
router = APIRouter(
    prefix="/mail",
    tags=["mail"],
    dependencies=[Depends(require_module("mail")), Depends(require_perm("mail", "view"))],
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
    #: На какое письмо это ответ. Отсюда берутся заголовки цепочки, а клиент и
    #: заявка наследуются от исходного письма, если своих не назвали.
    reply_to_id: int | None = None


class MailReadIn(BaseModel):
    is_read: bool = True


# --- ящики (управляющий почтой) ---
# Право то же, что у `/mail/admin`: два права на одни ящики расходились, и тот, кто
# правил настройки, менял адрес ящика своего сервера мимо него (разбор 10.10.2026).

@router.get("/accounts")
def list_accounts(
    user: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    """Все ящики, включая выключенные; токен с `mail_boxes` видит только свои."""
    yashchiki = mail_access.vidimye_yashchiki(db, user, s_neaktivnymi=True)
    return {"items": [schemas.mail_account_out(a) for a in sorted(yashchiki, key=lambda a: a.id)]}


@router.post("/accounts", status_code=201)
def create_account(
    payload: MailAccountIn,
    _: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    mail_access.bez_ogranicheniya(db)
    account = mail_service.create_account(db, payload.model_dump())
    return schemas.mail_account_out(account)


@router.patch("/accounts/{account_id}")
def update_account(
    account_id: int,
    payload: MailAccountPatchIn,
    _: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    mail_access.v_oblasti(db, account_id)
    account = mail_service.update_account(db, account_id, payload.model_dump(exclude_unset=True))
    return schemas.mail_account_out(account)


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    actor: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    mail_access.v_oblasti(db, account_id)
    mail_service.delete_account(db, account_id, actor)
    return {"message": "Mail account deleted"}


@router.post("/accounts/{account_id}/check")
def check_account(
    account_id: int,
    _: User = Depends(require_perm("mail", "manage")),
    db: Session = Depends(get_db),
):
    """Проверка настроек: вход по IMAP и по SMTP. Ошибка возвращается текстом."""
    mail_access.v_oblasti(db, account_id)
    return mail_service.check_account(db, account_id)


@router.post("/accounts/{account_id}/sync")
def sync_account(
    account_id: int,
    user: User = Depends(require_perm("mail", "view")),
    db: Session = Depends(get_db),
):
    """Ручная синхронизация. Доступна любому сотруднику: это чтение почты фирмы,
    а не правка настроек — ждать root'а ради свежих писем незачем. Но только
    своего ящика: чужой личный ящик отвечает тем же 404, что и несуществующий."""
    mail_access.proverit(db, user, account_id)
    return mail_service.sync_account(db, account_id)


# --- письма ---

@router.get("/senders")
def list_senders(
    user: User = Depends(require_perm("mail", "create")),
    db: Session = Depends(get_db),
):
    """Ящики, из которых можно писать. Только имя и адрес.

    **Отдельная ручка, а не общий список ящиков.** Список ящиков закрыт
    `mail.manage` и правильно закрыт: там хосты, порты и логины — настройка
    уровня фирмы. Но чтобы ОТПРАВИТЬ письмо, всего этого знать не надо, нужен
    выбор из двух-трёх адресов.

    Пока ручки не было, экран почты спрашивал общий список — и менеджеру с
    правом писать он отвечал отказом. Ящиков ноль, выбирать не из чего, кнопка
    ответа спрятана: писать письма мог только владелец системы. Найдено
    разбором правки, а типами и тестами не ловилось никак.

    Выключённые ящики не отдаём: письмо из них всё равно не уйдёт, а выбор,
    который отвечает отказом, хуже отсутствующего. Ящики — только те, от чьего
    имени сотруднику можно писать (`mail_access`).
    """
    return {
        "items": [
            {"id": a.id, "title": a.title, "address": a.address}
            for a in mail_access.vidimye_yashchiki(db, user)
            if mail_access.mozhet_pisat(db, user, a)
        ]
    }


@router.get("/unread")
def unread_count(
    user: User = Depends(require_perm("mail", "view")),
    db: Session = Depends(get_db),
):
    """Сколько входящих не прочитано — число на пункт меню (план К-02).

    Только видимые ящики. У ящика своего сервера число — с сервера (`unseen_count`
    наблюдателя): прочитанное в веб-почте меняет флаг на сервере, а не в зеркале.
    """
    yashchiki = mail_access.vidimye_yashchiki(db, user)
    vneshnie = {b.id for b in yashchiki if b.server_id is None}
    po_yashchikam = {b.id: b.unseen_count for b in yashchiki if b.server_id is not None}
    schyot = mail_repo.count_unread_by_account(db, vneshnie)
    po_yashchikam.update({b_id: schyot.get(b_id, 0) for b_id in vneshnie})
    return {
        "count": sum(po_yashchikam.values()),
        "by_box": [{"id": b_id, "unseen": n} for b_id, n in sorted(po_yashchikam.items())],
    }


@router.get("/messages")
def list_messages(
    account_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    direction: str | None = Query(default=None, pattern="^(in|out)$"),
    unread: bool | None = None,
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("mail", "view")),
    db: Session = Depends(get_db),
):
    items, total = mail_repo.search_messages(
        db,
        account_ids=mail_access.vidimye_ids(db, user),
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


def _vidimoe_pismo(db: Session, user: User, message_id: int) -> MailMessage:
    """Письмо из ящика, который сотрудник видит; чужое — тем же 404, что и несуществующее."""
    message = mail_repo.get_message(db, message_id)
    if message is None or message.account_id not in mail_access.vidimye_ids(db, user):
        raise errors.NotFoundError("Message not found", code="mail_message_not_found")
    return message


@router.get("/messages/{message_id}")
def get_message(
    message_id: int,
    user: User = Depends(require_perm("mail", "view")),
    db: Session = Depends(get_db),
):
    return schemas.mail_message_out(_vidimoe_pismo(db, user, message_id), with_body=True)


@router.post("/messages/{message_id}/read")
def set_read(
    message_id: int,
    payload: MailReadIn,
    user: User = Depends(require_perm("mail", "view")),
    db: Session = Depends(get_db),
):
    _vidimoe_pismo(db, user, message_id)
    return schemas.mail_message_out(mail_service.mark_read(db, message_id, payload.is_read))


@router.post("/send", status_code=201)
def send(
    payload: MailSendIn,
    user: User = Depends(require_perm("mail", "create")),
    db: Session = Depends(get_db),
):
    deal_service.proverit_vidimost(db, user, payload.deal_id)
    # Ящик выбираем здесь, а не в сервисе: «первый включённый» мог оказаться
    # чужим личным ящиком, от которого этому сотруднику писать нельзя.
    account_id = payload.account_id
    if payload.reply_to_id:
        # Видимость исходного — всегда, а не только без ящика: иначе ответ из своего
        # ящика наследовал клиента и заявку письма из чужого (разбор 10.10.2026).
        iskhodnoe = _vidimoe_pismo(db, user, payload.reply_to_id)
        if account_id is None:
            account_id = iskhodnoe.account_id
        if payload.deal_id is None:
            deal_service.proverit_vidimost(db, user, iskhodnoe.deal_id)
    if account_id is None:
        svoi = [b for b in mail_access.vidimye_yashchiki(db, user) if mail_access.mozhet_pisat(db, user, b)]
        if not svoi:
            raise errors.ValidationError("No mailbox is configured", code="no_mail_account")
        account_id = svoi[0].id
    mail_access.proverit(db, user, account_id, otpravka=True)
    message = mail_service.send_message(
        db,
        user,
        to_addrs=payload.to,
        subject=payload.subject,
        body_text=payload.body,
        account_id=account_id,
        client_id=payload.client_id,
        deal_id=payload.deal_id,
        reply_to_id=payload.reply_to_id,
    )
    return schemas.mail_message_out(message, with_body=True)
