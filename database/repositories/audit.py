"""Чтение журнала.

Записи сюда не попадают: писать умеет только `core/services/audit_service.py`,
а править и удалять — никто (запрет стоит на мэппере, см. модель).

Порядок всегда «свежее сверху и по убыванию id при равном времени». Одно
действие человека порождает несколько записей в одну и ту же секунду, а `id`
задаёт среди них тот же порядок, в котором они писались, — без него цепочка
«списали, сменили этап, записали в ленту» показывалась бы вперемешку и читалась
бы как разные случаи.
"""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from database.models import AuditEvent
from database.query import contains, page_of


def search(
    db: Session,
    *,
    actor_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    source: str | None = None,
    action: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[AuditEvent], int]:
    stmt = select(AuditEvent)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if source:
        stmt = stmt.where(AuditEvent.source == source)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if q:
        # По названию объекта и по имени исполнителя: в журнал приходят со
        # словами «что было с матрицей», а не с идентификатором строки.
        needle = q.strip()
        stmt = stmt.where(
            or_(contains(AuditEvent.entity_label, needle), contains(AuditEvent.actor_name, needle))
        )
    if since is not None:
        stmt = stmt.where(AuditEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditEvent.created_at <= until)

    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    return page_of(db, stmt, page=page, per_page=per_page)
