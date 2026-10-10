"""Кто какие ящики видит (docs/bloki/32-pochtovyy-server.md, «Доступ к ящикам»).

Видимость — условием запроса, как у ключей (`database/repositories/klyuchi.py`):
отбор после выборки однажды посчитает непрочитанные по всем ящикам и покажет
число тому, кому виден один.
"""

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from database.models import MailAccount, MailBoxAccess, User, UserTokenMailBox


def _vidno(user_id: int):
    """Общий ящик или выданный поимённо."""
    poimenno = exists().where(
        MailBoxAccess.account_id == MailAccount.id,
        MailBoxAccess.user_id == user_id,
    )
    return or_(MailAccount.is_shared.is_(True), poimenno)


def vidimye(
    db: Session,
    user_id: int,
    *,
    vse: bool,
    tolko: set[int] | None = None,
    s_neaktivnymi: bool = False,
) -> list[MailAccount]:
    """Ящики сотрудника: `vse` — управляющий почтой; `tolko` — ограничение токена."""
    zapros = select(MailAccount)
    if not s_neaktivnymi:
        zapros = zapros.where(MailAccount.is_active.is_(True))
    if not vse:
        zapros = zapros.where(_vidno(user_id))
    if tolko is not None:
        if not tolko:
            return []
        zapros = zapros.where(MailAccount.id.in_(sorted(tolko)))
    zapros = zapros.order_by(MailAccount.address, MailAccount.id)
    return list(db.scalars(zapros))


def dostup(db: Session, account_id: int, user_id: int) -> MailBoxAccess | None:
    return db.scalar(
        select(MailBoxAccess).where(
            MailBoxAccess.account_id == account_id, MailBoxAccess.user_id == user_id
        )
    )


def dostupy(db: Session, account_id: int) -> list[tuple[MailBoxAccess, User]]:
    zapros = (
        select(MailBoxAccess, User)
        .join(User, User.id == MailBoxAccess.user_id)
        .where(MailBoxAccess.account_id == account_id)
        .order_by(User.name, User.id)
    )
    return [(d, u) for d, u in db.execute(zapros)]


def zamenit_dostupy(
    db: Session, account_id: int, stroki: list[tuple[int, bool]], granted_by: int | None
) -> None:
    """Список доступа целиком: кого нет в `stroki` — убрать, кто есть — выставить отправку."""
    nuzhno = {user_id: can_send for user_id, can_send in stroki}
    for d in db.scalars(select(MailBoxAccess).where(MailBoxAccess.account_id == account_id)):
        if d.user_id not in nuzhno:
            db.delete(d)
        else:
            d.can_send = nuzhno.pop(d.user_id)
    for user_id, can_send in nuzhno.items():
        db.add(
            MailBoxAccess(
                account_id=account_id, user_id=user_id, can_send=can_send, granted_by=granted_by
            )
        )
    db.flush()


def yashchiki_tokena(db: Session, token_id: int) -> set[int]:
    return set(
        db.scalars(select(UserTokenMailBox.account_id).where(UserTokenMailBox.token_id == token_id))
    )


def zadat_yashchiki_tokena(db: Session, token_id: int, ids: set[int]) -> None:
    for stroka in db.scalars(select(UserTokenMailBox).where(UserTokenMailBox.token_id == token_id)):
        if stroka.account_id in ids:
            ids.discard(stroka.account_id)
        else:
            db.delete(stroka)
    for account_id in sorted(ids):
        db.add(UserTokenMailBox(token_id=token_id, account_id=account_id))
    db.flush()
