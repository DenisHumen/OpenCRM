"""Работы установки почтового сервера и то, что им нужно от базы (docs/bloki/32-pochtovyy-server.md).

Серверы, домены и ящики — в `mail_server.py`; здесь — журнал работ, замок
сервера и счёты, которые спрашивает только установка.
"""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database.models import MailAccount, MailJob, MailServer
from database.models.mail_server import JOB_RUNNING


def zapert_server(db: Session, server_id: int) -> MailServer | None:
    """Строка сервера под замком: две работы разом на один сервер не начинаются."""
    return db.scalar(
        select(MailServer).where(MailServer.id == server_id).with_for_update().execution_options(populate_existing=True)
    )


def get_job(db: Session, job_id: int) -> MailJob | None:
    return db.get(MailJob, job_id)


def raboty_servera(db: Session, server_id: int, limit: int = 20) -> list[MailJob]:
    return list(
        db.scalars(
            select(MailJob)
            .where(MailJob.server_id == server_id)
            .order_by(MailJob.id.desc())
            .limit(limit)
        )
    )


def idushchie(db: Session, server_id: int | None = None) -> list[MailJob]:
    zapros = select(MailJob).where(MailJob.status == JOB_RUNNING)
    if server_id is not None:
        zapros = zapros.where(MailJob.server_id == server_id)
    return list(db.scalars(zapros.order_by(MailJob.id)))


def dopisat_zhurnal(db: Session, job_id: int, kusok: str, *, steps: str, kogda: datetime) -> None:
    """Дописать журнал на стороне базы: весь журнал на каждый кусок в процесс не тянем."""
    db.execute(
        update(MailJob)
        .where(MailJob.id == job_id)
        .values(log=func.concat(func.coalesce(MailJob.log, ""), kusok), steps=steps, heartbeat_at=kogda)
        .execution_options(synchronize_session=False)
    )


def zhurnal(db: Session, job_id: int, s_bayta: int, dlina: int) -> tuple[str, int]:
    """Кусок журнала с байта `s_bayta` (UTF-8) и смещение, с которого читать дальше.

    Смещение считается по отданному куску, а не отдельным запросом длины: журнал
    дописывается на ходу. Недописанный хвостом символ не отдаётся — его байты
    придут целиком в следующий раз.
    """
    syroe = (db.scalar(select(MailJob.log).where(MailJob.id == job_id)) or "").encode("utf-8")
    nachalo = max(0, min(s_bayta, len(syroe)))
    while nachalo < len(syroe) and 0x80 <= syroe[nachalo] < 0xC0:
        nachalo += 1  # чужое смещение посреди символа — с начала следующего
    kusok = syroe[nachalo : nachalo + dlina]
    i = len(kusok) - 1
    while i >= 0 and 0x80 <= kusok[i] < 0xC0:
        i -= 1
    if i >= 0 and kusok[i] >= 0xC0:
        nado = 2 if kusok[i] < 0xE0 else (3 if kusok[i] < 0xF0 else 4)
        if len(kusok) - i < nado:
            kusok = kusok[:i]
    return kusok.decode("utf-8", "replace"), nachalo + len(kusok)


def chislo_yashchikov(db: Session, server_id: int) -> int:
    return int(
        db.scalar(select(func.count()).select_from(MailAccount).where(MailAccount.server_id == server_id)) or 0
    )


def chislo_yashchikov_po_serveram(db: Session) -> dict[int, int]:
    zapros = (
        select(MailAccount.server_id, func.count())
        .where(MailAccount.server_id.is_not(None))
        .group_by(MailAccount.server_id)
    )
    return {server_id: int(n) for server_id, n in db.execute(zapros)}


def yashchiki_domena(db: Session, server_id: int, domain: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(MailAccount)
            .where(MailAccount.server_id == server_id, MailAccount.address.endswith("@" + domain, autoescape=True))
        )
        or 0
    )


def otklyuchit_yashchiki(db: Session, server_id: int) -> int:
    """Сервер снят — его ящики гаснут, но не стираются: переписка клиентов остаётся."""
    itog = db.execute(
        update(MailAccount)
        .where(MailAccount.server_id == server_id, MailAccount.is_active.is_(True))
        .values(is_active=False)
        .execution_options(synchronize_session="fetch")
    )
    return int(itog.rowcount or 0)
