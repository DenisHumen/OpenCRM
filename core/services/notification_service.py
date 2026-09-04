"""Уведомления сотрудникам: кому, о чём, куда вести (docs/21 §4).

Устройство в трёх правилах:

- **автор действия не получает уведомления о нём** — он и так видит ответ
  экрана; уведомление — для тех, кто смотрел в другую сторону;
- **адресат — тот, кто вправе видеть**: право `view` соответствующей области,
  а у заявок ещё и область ответственного (`deals_scope`): менеджеру, который
  видит только свои заявки, не рассказывают о чужих;
- **текста нет**: сервер пишет вид и подстановки, подпись собирает экран на
  языке читателя. Так системные данные остаются по-английски без единой
  английской фразы в базе.

Пишутся наблюдателями событий: не записалось — операция всё равно состоялась,
подсказка не стоит отгрузки.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.orm import Session

from core.services import permissions_service
from core.utils import now_utc
from database.models import User
from database.models.notification import Notification
from database.repositories import notifications as repo

#: Сколько уведомлений держим: старше — убирает уборка, подсказки не учёт.
HRANIT_DNEY = 60
NA_STRANITSE = 50


def adresaty(db: Session, area: str, *, manager_id: int | None = None, krome: User | None = None) -> list[User]:
    """Кто вправе видеть событие области: право `view`, у заявок — свои или все."""
    itog = []
    for user in repo.active_users(db):
        if krome is not None and user.id == krome.id:
            continue
        if not permissions_service.has(db, user, area, "view"):
            continue
        if area == "deals" and manager_id is not None:
            svoi = permissions_service.deals_scope(db, user)
            if svoi is not None and svoi != manager_id:
                continue
        itog.append(user)
    return itog


def notify(
    db: Session,
    users: list[User],
    kind: str,
    params: dict | None = None,
    link: str = "",
) -> int:
    """Записать уведомление каждому из списка. Пустой список — ничего."""
    if not users:
        return 0
    telo = json.dumps(params or {}, ensure_ascii=False)
    repo.add_many(
        db,
        [Notification(user_id=u.id, kind=kind, params=telo, link=link[:200]) for u in users],
    )
    return len(users)


def spisok(db: Session, user: User, page: int = 1) -> tuple[list[dict], int]:
    rows, total = repo.list_for_user(db, user.id, page, NA_STRANITSE)
    return [out(row) for row in rows], total


def neprochitano(db: Session, user: User) -> int:
    return repo.unread_count(db, user.id)


def prochitat(db: Session, user: User, ids: list[int] | None = None) -> int:
    return repo.mark_read(db, user.id, ids, now_utc().replace(tzinfo=None))


def ubrat_starye(db: Session) -> int:
    return repo.purge_older_than(db, (now_utc() - timedelta(days=HRANIT_DNEY)).replace(tzinfo=None))


def out(row: Notification) -> dict:
    try:
        params = json.loads(row.params or "{}")
    except ValueError:
        params = {}
    return {
        "id": row.id,
        "kind": row.kind,
        "params": params,
        "link": row.link,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "read": row.read_at is not None,
    }
