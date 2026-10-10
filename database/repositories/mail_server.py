"""Свой почтовый сервер: серверы, домены, ящики на них, псевдонимы (docs/bloki/32-pochtovyy-server.md).

Общий файл двух частей блока: установку и домены ведёт `mail_install`, ящики и
псевдонимы — `mail_server_service`, счётчики новой почты — `mail_watcher`.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from database.models import (
    MailAccount,
    MailAlias,
    MailBoxAccess,
    MailDomain,
    MailServer,
    User,
    UserTokenMailBox,
)
from database.models.mail_server import STATUS_READY


def dobavit(db: Session, obekt):
    db.add(obekt)
    db.flush()
    return obekt


def udalit(db: Session, obekt) -> None:
    db.delete(obekt)
    db.flush()


# --- серверы и домены ---


def get_server(db: Session, server_id: int) -> MailServer | None:
    return db.get(MailServer, server_id)


def servery(db: Session) -> list[MailServer]:
    return list(db.scalars(select(MailServer).order_by(MailServer.id)))


def gotovye_servery(db: Session) -> list[MailServer]:
    return list(
        db.scalars(
            select(MailServer).where(MailServer.status == STATUS_READY).order_by(MailServer.id)
        )
    )


def domeny_servera(db: Session, server_id: int) -> list[MailDomain]:
    return list(
        db.scalars(
            select(MailDomain)
            .where(MailDomain.server_id == server_id)
            .order_by(MailDomain.domain, MailDomain.id)
        )
    )


def domen_po_imeni(db: Session, domain: str) -> MailDomain | None:
    return db.scalar(select(MailDomain).where(MailDomain.domain == domain))


def vse_domeny(db: Session) -> list[MailDomain]:
    return list(db.scalars(select(MailDomain).order_by(MailDomain.domain, MailDomain.id)))


# --- ящики ---


def yashchik_po_adresu(db: Session, address: str) -> MailAccount | None:
    """Ящик с таким адресом, свой или внешний. Сравнение — как у базы (без регистра)."""
    return db.scalar(
        select(MailAccount).where(MailAccount.address == address).order_by(MailAccount.id).limit(1)
    )


def zanyatye_adresa(db: Session, adresa: list[str]) -> set[str]:
    """Какие из адресов уже заняты ящиком — одним запросом на всю пачку подсказок."""
    if not adresa:
        return set()
    return {
        a.lower()
        for a in db.scalars(select(MailAccount.address).where(MailAccount.address.in_(adresa)))
    }


def yashchiki_servera(db: Session, server_id: int) -> list[MailAccount]:
    return list(
        db.scalars(
            select(MailAccount)
            .where(MailAccount.server_id == server_id)
            .order_by(MailAccount.address, MailAccount.id)
        )
    )


def vse_yashchiki(db: Session, tolko: set[int] | None = None) -> list[MailAccount]:
    """Все ящики, включённые и выключенные: экран управляющего. `tolko` — область токена."""
    zapros = select(MailAccount)
    if tolko is not None:
        if not tolko:
            return []
        zapros = zapros.where(MailAccount.id.in_(sorted(tolko)))
    return list(db.scalars(zapros.order_by(MailAccount.address, MailAccount.id)))


def otmetit_proverku(db: Session, ids: list[int], kogda: datetime) -> None:
    """Отметка опроса мимо ORM: грязный объект дал бы живой намёк всем каждые 15 секунд."""
    if ids:
        db.execute(
            update(MailAccount)
            .where(MailAccount.id.in_(sorted(ids)))
            .values(status_checked_at=kogda)
            .execution_options(synchronize_session=False)
        )


def dostupy_yashchikov(db: Session, ids: list[int]) -> dict[int, list[tuple[MailBoxAccess, User]]]:
    """Списки доступа пачкой: экран ящиков не делает запрос на строку."""
    itog: dict[int, list[tuple[MailBoxAccess, User]]] = {i: [] for i in ids}
    if not ids:
        return itog
    zapros = (
        select(MailBoxAccess, User)
        .join(User, User.id == MailBoxAccess.user_id)
        .where(MailBoxAccess.account_id.in_(sorted(ids)))
        .order_by(User.name, User.id)
    )
    for d, u in db.execute(zapros):
        itog.setdefault(d.account_id, []).append((d, u))
    return itog


def yashchiki_tokenov(db: Session, token_ids: list[int]) -> dict[int, list[int]]:
    """Ящики токенов пачкой — для списка токенов на экране API."""
    itog: dict[int, list[int]] = {}
    if not token_ids:
        return itog
    zapros = (
        select(UserTokenMailBox)
        .where(UserTokenMailBox.token_id.in_(sorted(token_ids)))
        .order_by(UserTokenMailBox.token_id, UserTokenMailBox.account_id, UserTokenMailBox.id)
    )
    for stroka in db.scalars(zapros):
        itog.setdefault(stroka.token_id, []).append(stroka.account_id)
    return itog


# --- псевдонимы ---


def get_alias(db: Session, alias_id: int) -> MailAlias | None:
    return db.get(MailAlias, alias_id)


def psevdonimy(db: Session, server_id: int | None = None) -> list[MailAlias]:
    zapros = select(MailAlias)
    if server_id is not None:
        zapros = zapros.where(MailAlias.server_id == server_id)
    return list(db.scalars(zapros.order_by(MailAlias.source, MailAlias.destination, MailAlias.id)))


def psevdonim(db: Session, server_id: int, source: str, destination: str) -> MailAlias | None:
    return db.scalar(
        select(MailAlias).where(
            MailAlias.server_id == server_id,
            MailAlias.source == source,
            MailAlias.destination == destination,
        )
    )


def psevdonimy_na(db: Session, server_id: int, destination: str) -> list[MailAlias]:
    """Псевдонимы, ведущие в этот адрес: при переезде и удалении ящика их ведут следом."""
    return list(
        db.scalars(
            select(MailAlias)
            .where(MailAlias.server_id == server_id, MailAlias.destination == destination)
            .order_by(MailAlias.source, MailAlias.id)
        )
    )


def psevdonim_s_adresa(db: Session, server_id: int, source: str) -> MailAlias | None:
    return db.scalar(
        select(MailAlias)
        .where(MailAlias.server_id == server_id, MailAlias.source == source)
        .order_by(MailAlias.id)
        .limit(1)
    )
