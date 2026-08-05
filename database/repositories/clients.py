from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Client, ClientFile, ClientNote


def get_many(db: Session, ids) -> list[Client]:
    """Клиенты пачкой — одним запросом на выдачу, а не по одному на строку.

    Мягко удалённых не отсеиваем: у старой сделки клиент мог быть удалён, но имя
    в карточке всё равно надо показать, иначе строка станет безымянной.
    """
    ids = [i for i in set(ids) if i]
    if not ids:
        return []
    return list(db.scalars(select(Client).where(Client.id.in_(ids))))


def get(db: Session, client_id: int, include_deleted: bool = False) -> Client | None:
    client = db.get(Client, client_id)
    if client is None:
        return None
    if client.deleted_at is not None and not include_deleted:
        return None
    return client


def search(
    db: Session,
    q: str | None = None,
    tag: str | None = None,
    manager_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Client], int]:
    stmt = select(Client).where(Client.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Client.name.ilike(like),
                Client.company.ilike(like),
                Client.phone.ilike(like),
                Client.email.ilike(like),
                Client.tags.ilike(like),
            )
        )
    if tag:
        stmt = stmt.where(Client.tags.ilike(f"%{tag.strip()}%"))
    if manager_id:
        stmt = stmt.where(Client.manager_id == manager_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(Client.updated_at.desc()).offset((page - 1) * per_page).limit(per_page)
    return list(db.scalars(stmt)), total


def list_notes(
    db: Session,
    client_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
    kind: str | None = None,
    deal_id: int | None = None,
) -> tuple[list[ClientNote], int]:
    """Лента: заметки, звонки, встречи и письма одним потоком.

    Порядок — по времени события, а не по времени записи: звонок вчерашний, а
    занесли его сегодня, и в ленте он обязан стоять вчерашним числом.
    """
    base = select(ClientNote)
    if client_id is not None:
        base = base.where(ClientNote.client_id == client_id)
    if kind:
        base = base.where(ClientNote.kind == kind)
    if deal_id is not None:
        base = base.where(ClientNote.deal_id == deal_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = (
        base.order_by(ClientNote.happened_at.desc(), ClientNote.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return list(db.scalars(stmt)), total


def get_note(db: Session, client_id: int, note_id: int) -> ClientNote | None:
    note = db.get(ClientNote, note_id)
    if note is None or note.client_id != client_id:
        return None
    return note


def list_files(db: Session, client_id: int) -> list[ClientFile]:
    return list(
        db.scalars(
            select(ClientFile)
            .where(ClientFile.client_id == client_id)
            .order_by(ClientFile.created_at.desc())
        )
    )


def get_file(db: Session, client_id: int, file_id: int) -> ClientFile | None:
    f = db.get(ClientFile, file_id)
    if f is None or f.client_id != client_id:
        return None
    return f
