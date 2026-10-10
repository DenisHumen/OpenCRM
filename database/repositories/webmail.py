"""Запросы веб-почты (docs/bloki/32-pochtovyy-server.md, «Веб-почта»).

Письма живут на почтовом сервере, а не в базе: отсюда берётся только то, чем
к нему войти, — сервер ящика.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import MailServer


def server(db: Session, server_id: int) -> MailServer | None:
    return db.scalar(select(MailServer).where(MailServer.id == server_id))
