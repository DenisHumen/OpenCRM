"""Доступ к ящикам: кто какой ящик видит и от чьего имени может писать.

Матрица прав пускает в раздел (`mail.view`, `mail.create`, `mail.manage`), а в
конкретный ящик пускает список доступа (`MailBoxAccess`) — как у ключей. Общий
ящик (`is_shared`) виден всем с `mail.view`: так было до списков, и старые ящики
такими и остались. Токен может сузить набор ещё (`user_token_mail_boxes`).
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import permissions_service
from database.models import MailAccount, User
from database.repositories import mail as mail_repo
from database.repositories import mail_boxes as boxes_repo

#: Ключ `db.info`: ящики, которыми ограничен токен запроса. Нет ключа или None — без ограничения.
TOKEN_SCOPE_KEY = "opencrm_mail_token_scope"


def upravlyaet(db: Session, user: User) -> bool:
    """Управляющий почтой видит все ящики: он их заводит и раздаёт."""
    return permissions_service.has(db, user, "mail", "manage")


def oblast_tokena(db: Session) -> set[int] | None:
    return db.info.get(TOKEN_SCOPE_KEY)


def v_oblasti(db: Session, account_id: int) -> MailAccount:
    """Ящик для управляющего: любой, но в пределах токена. Вне их — тот же 404, что у чужого."""
    box = mail_repo.get_account(db, account_id)
    oblast = oblast_tokena(db)
    if box is None or (oblast is not None and account_id not in oblast):
        raise errors.NotFoundError("Mailbox not found", code="mail_account_not_found")
    return box


def bez_ogranicheniya(db: Session) -> None:
    """Заводить ящики может только токен на все ящики: узкий токен — про свои."""
    if oblast_tokena(db) is not None:
        raise errors.ForbiddenError(
            "This token is limited to selected mailboxes", code="mail_box_forbidden"
        )


def vidimye_yashchiki(db: Session, user: User, *, s_neaktivnymi: bool = False) -> list[MailAccount]:
    return boxes_repo.vidimye(
        db,
        user.id,
        vse=upravlyaet(db, user),
        tolko=oblast_tokena(db),
        s_neaktivnymi=s_neaktivnymi,
    )


def vidimye_ids(db: Session, user: User) -> set[int]:
    return {box.id for box in vidimye_yashchiki(db, user)}


def proverit(db: Session, user: User, account_id: int, *, otpravka: bool = False) -> MailAccount:
    """Ящик, если сотрудник его видит (и может писать от его имени), иначе отказ.

    Невидимый ящик отвечает тем же 404, что и несуществующий: «такой ящик есть,
    но вам нельзя» — уже ответ на вопрос, который задавать не просили.
    """
    box = mail_repo.get_account(db, account_id)
    if box is None or account_id not in vidimye_ids(db, user):
        raise errors.NotFoundError("Mailbox not found", code="mail_account_not_found")
    if otpravka:
        if not permissions_service.has(db, user, "mail", "create"):
            raise errors.ForbiddenError("Sending is not allowed", code="mail_box_forbidden")
        if not box.is_shared and not upravlyaet(db, user):
            stroka = boxes_repo.dostup(db, account_id, user.id)
            if stroka is None or not stroka.can_send:
                raise errors.ForbiddenError(
                    "Sending from this mailbox is not allowed", code="mail_box_forbidden"
                )
    return box


def mozhet_pisat(db: Session, user: User, box: MailAccount) -> bool:
    """То же, что `proverit(otpravka=True)`, но ответом для списка ящиков."""
    if not permissions_service.has(db, user, "mail", "create"):
        return False
    if box.is_shared or upravlyaet(db, user):
        return True
    stroka = boxes_repo.dostup(db, box.id, user.id)
    return stroka is not None and stroka.can_send
