from sqlalchemy import literal, or_, select
from sqlalchemy.orm import Session

from database.models import Client, ClientFile, ClientNote
from database.query import contains, page_of


def get_many(db: Session, ids) -> list[Client]:
    """Клиенты пачкой — одним запросом на выдачу, а не по одному на строку.

    Мягко удалённых не отсеиваем: у старой сделки клиент мог быть удалён, но имя
    в карточке всё равно надо показать, иначе строка станет безымянной.
    """
    ids = [i for i in set(ids) if i]
    if not ids:
        return []
    return list(db.scalars(select(Client).where(Client.id.in_(ids))))


def names_by_ids(db: Session, client_ids: list[int]) -> dict[int, str]:
    """Имена клиентов разом — для списков, где клиент нужен одной подписью.

    Удалённые тоже возвращаются: доска, сделанная для клиента, которого потом
    убрали, остаётся его доской, и подпись «—» вместо имени сказала бы неправду.
    """
    if not client_ids:
        return {}
    rows = db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids))).all()
    return {client_id: name for client_id, name in rows}


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
        needle = q.strip()
        # Телефон ищем и как набрано, и в приведённом виде. Колонка `phone_norm`
        # заведена ровно ради этого («показываем то, что ввёл менеджер, а ищем —
        # по этому»), телефония ею пользуется, а поисковая строка — нет: две
        # карточки с одним номером находились по-разному в зависимости от того,
        # ставил ли менеджер пробелы. Искали «0671112233» — находили одну из
        # двух.
        digits = "".join(ch for ch in needle if ch.isdigit())
        conditions = [
            contains(Client.name, needle),
            contains(Client.company, needle),
            contains(Client.phone, needle),
            contains(Client.email, needle),
            contains(Client.tags, needle),
        ]
        if digits:
            conditions.append(contains(Client.phone_norm, digits))
        stmt = stmt.where(or_(*conditions))
    if tag and tag.strip():
        # Метки лежат одной строкой через запятую, и подстрочный поиск по ней
        # находит чужие: фильтр `ip` возвращал всех, у кого стоит `vip`, `zip`
        # или `equipment`. Обрамляем и строку, и искомое запятыми — тогда
        # совпасть может только метка целиком.
        #
        # Сложение строк, а не `func.concat`: SQLAlchemy сама соберёт `||` для
        # SQLite и `concat()` для MySQL. Экранирование `%` и `_` остаётся за
        # `contains`.
        v_ramke = literal(",") + Client.tags + literal(",")
        stmt = stmt.where(contains(v_ramke, f",{tag.strip()},"))
    if manager_id:
        stmt = stmt.where(Client.manager_id == manager_id)
    stmt = stmt.order_by(Client.updated_at.desc())
    return page_of(db, stmt, page=page, per_page=per_page)


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
    stmt = base.order_by(ClientNote.happened_at.desc(), ClientNote.id.desc())
    return page_of(db, stmt, page=page, per_page=per_page)


def get_note_by_id(db: Session, note_id: int) -> ClientNote | None:
    """Запись ленты без оглядки на клиента.

    Отдельно от `get_note`: там клиент известен из адреса и сверяется, здесь
    к записи идут от звонка, который её и породил, — своего клиента он помнит
    сам, и спрашивать его заново значило бы сверять запись саму с собой.
    """
    return db.get(ClientNote, note_id)


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


def deleted(db: Session) -> list[Client]:
    """Клиенты в корзине — для отчёта о том, сколько места можно освободить."""
    return list(db.scalars(select(Client).where(Client.deleted_at.is_not(None))))


def files_of(db: Session, client_ids) -> list[ClientFile]:
    """Файлы сразу нескольких клиентов — одним запросом на весь список.

    Отдельно от `list_files`: там речь про одну карточку, здесь про обход всей
    корзины, и запрос на каждого клиента превратил бы отчёт о месте в сотни
    обращений к базе ровно на той системе, где мусора накопилось больше всего.
    """
    client_ids = [i for i in set(client_ids) if i]
    if not client_ids:
        return []
    return list(db.scalars(select(ClientFile).where(ClientFile.client_id.in_(client_ids))))


def get_file(db: Session, client_id: int, file_id: int) -> ClientFile | None:
    f = db.get(ClientFile, file_id)
    if f is None or f.client_id != client_id:
        return None
    return f
