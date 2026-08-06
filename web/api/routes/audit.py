"""Журнал действий: только чтение.

Ни POST, ни PATCH, ни DELETE здесь нет и не появится. Дело не в лени: журнал
дописывается сервисами в той же транзакции, что и само изменение, — ручка
«добавить запись» позволила бы дописать в него что угодно и когда угодно, а
ручка «удалить» отменила бы весь смысл затеи. Запрет на правку продублирован на
уровне ORM (`database/models/audit.py`), чтобы он не держался на отсутствии
маршрута.

Видит журнал пока только root. Ролей в системе ещё нет; когда появятся, право
«видеть журнал» станет отдельным — здесь поменяется одна зависимость.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database.models import User
from database.repositories import audit as audit_repo
from web.api import schemas
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
def list_events(
    actor_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    source: str | None = None,
    action: str | None = None,
    search: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(default=1, ge=1),
    # Потолок ниже обычного: страница журнала — это таблица со «стало» и «было»
    # в каждой строке, и двести таких строк никто глазами не читает.
    per_page: int = Query(default=50, ge=1, le=100),
    _: User = Depends(require_perm("audit", "view")),
    db: Session = Depends(get_db),
):
    items, total = audit_repo.search(
        db,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
        action=action,
        q=search,
        since=since,
        until=until,
        page=page,
        per_page=per_page,
    )
    return schemas.paginated(
        [schemas.audit_event_out(entry) for entry in items], total, page, per_page
    )
