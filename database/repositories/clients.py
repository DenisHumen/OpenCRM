from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session

from database.models import Client, ClientFile, ClientNote
from database.query import contains, contains_norm, page_of, page_without_total


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


def _search_stmt(
    q: str | None = None,
    tag: str | None = None,
    manager_id: int | None = None,
):
    """Условия отбора — отдельно от того, как выдачу нарезают на страницы.

    Двое зовут один и тот же отбор по-разному: экран раздела листает выдачу и
    просит точный `total` (`search`), командная палитра показывает шесть строк
    и точное число выбрасывает (`search_top`). Собранные порознь, эти условия
    однажды разъедутся — и поиск начнёт находить разное в двух местах, ничем
    этого не объявив.
    """
    stmt = select(Client).where(Client.deleted_at.is_(None))
    if q:
        needle = q.strip()
        # Пять условий `lower(колонка) LIKE …` заменены одним по склейке
        # (`Client.search_text`, см. `database/models/client.py`). Набор
        # найденного тот же: в склейку входят ровно те поля, что проверялись
        # раньше, и совпадение не может перескочить границу поля — они склеены
        # переводом строки, которого в строке поиска не бывает.
        #
        # Телефон по-прежнему ищется и как набрано, и в приведённом виде — но
        # цифры ищутся ТОЛЬКО в `phone_norm`, а не по всей склейке. Разница не
        # теоретическая: в склейку входят имя, фирма, почта и метки, и человек
        # с номером заказа в имени («Заказ 380671119999») стал бы находиться по
        # чужому телефону, хотя телефона у него нет вовсе. Поиск по номеру
        # обязан отвечать про номер.
        #
        # Колонка `phone_norm` при этом остаётся нетронутой и в другом смысле:
        # по ней телефония находит карточку РАВЕНСТВОМ, и это единственный
        # быстрый путь в проекте.
        digits = "".join(ch for ch in needle if ch.isdigit())
        conditions = [contains_norm(Client.search_text, needle)]
        if digits:
            conditions.append(contains(Client.phone_norm, digits))
        stmt = stmt.where(or_(*conditions))
    if tag and tag.strip():
        # Метки лежат одной строкой через запятую, и подстрочный поиск по ней
        # находит чужие: фильтр `ip` возвращал всех, у кого стоит `vip`, `zip`
        # или `equipment`. Обрамляем и строку, и искомое запятыми — тогда
        # совпасть может только метка целиком.
        #
        # Сложение строк, а не `func.concat`: SQLAlchemy сама соберёт из него
        # `concat()` под MySQL. Экранирование `%` и `_` остаётся за
        # `contains`.
        v_ramke = literal(",") + Client.tags + literal(",")
        stmt = stmt.where(contains(v_ramke, f",{tag.strip()},"))
    if manager_id:
        stmt = stmt.where(Client.manager_id == manager_id)
    return stmt.order_by(Client.updated_at.desc())


def search(
    db: Session,
    q: str | None = None,
    tag: str | None = None,
    manager_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Client], int]:
    """Страница списка клиентов и точное число найденного.

    Точный `total` здесь нужен: экран раздела листает выдачу, и из него
    считается номер последней страницы.
    """
    stmt = _search_stmt(q, tag, manager_id)
    return page_of(db, stmt, page=page, per_page=per_page)


def search_top(
    db: Session, q: str | None = None, *, page: int = 1, per_page: int = 6
) -> tuple[list[Client], bool]:
    """Страница строк для командной палитры — без счёта найденного.

    Второй проход по таблице ради числа, которого никто не показывает, стоит
    столько же, сколько сама выборка. Вместо него — «есть ли ещё», см.
    `database/query.page_without_total`.

    `page` — не украшение: без него палитра упиралась в первые шесть строк, и
    седьмого клиента нельзя было достать оттуда ничем. «Есть ещё» она при этом
    честно сообщала, а показать это «ещё» было нечем.
    """
    return page_without_total(db, _search_stmt(q), page=page, per_page=per_page)


def find_candidates(
    db: Session,
    *,
    email: str = "",
    phone_norm: str = "",
    name: str = "",
    limit: int = 6,
) -> list[Client]:
    """Кого система уже знает под этими приметами — СПИСКОМ, а не одного.

    Отличие от `leads_repo.find_client` принципиальное, и потому это отдельный
    вход, а не расширение того. Тот отвечает на вопрос «хоть кто-нибудь» и
    сознательно берёт первого попавшегося: для анонимной формы с сайта это
    терпимо. Здесь вопрос другой — «кто именно», — и его задают у стойки, где на
    карточку сейчас лягут деньги и товар. Молча взятый первый означает, что они
    уедут на чужую.

    Одна функция на все три приметы, а не три вызова подряд из сервиса: три
    вызова означали бы четвёртую копию правил поиска, которую докстрока
    `database/repositories/leads.py` прямо запрещает.

    Приёмы взяты у тех, кто их выстрадал: адрес сравнивается через `lower()`, а
    не `ilike` (в адресах законны `_` и `%`, а для LIKE это шаблоны); номер — по
    приведённой `phone_norm` равенством; имя — по склейке, той же, что у обычного
    поиска.

    **Порядок задан, и он не украшение.** Сначала совпавшие точной приметой
    (адрес, номер), потом совпавшие только именем. Выдача обрезана пределом, а
    имя ищется подстрокой: десяток однофамильцев, правленых сегодня, вытеснил бы
    из окна ту единственную карточку, у которой сошёлся номер, — и заказ завёл бы
    ей дубль вместо того, чтобы её найти.

    Сортировка выражением `CASE`, а не булевым столбцом: булев тип в MySQL — это
    TINYINT, и `ORDER BY` по нему зависит от того, как драйвер его отдал.
    """
    exact_conditions = []
    if email and email.strip():
        exact_conditions.append(func.lower(Client.email) == email.strip().lower())
    if phone_norm:
        exact_conditions.append(Client.phone_norm == phone_norm)
    conditions = list(exact_conditions)
    if name and name.strip():
        conditions.append(contains_norm(Client.search_text, name.strip()))
    if not conditions:
        return []
    exact_first = (
        case((or_(*exact_conditions), 0), else_=1) if exact_conditions else literal(0)
    )
    stmt = (
        select(Client)
        .where(Client.deleted_at.is_(None), or_(*conditions))
        # Внутри своей половины — свежая карточка первой: тот же порядок, что у
        # поиска по номеру в телефонии. Но выбирать из списка всё равно будет
        # человек.
        .order_by(exact_first.asc(), Client.updated_at.desc(), Client.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


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
