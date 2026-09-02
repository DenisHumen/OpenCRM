from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Board, Work
from database.query import contains, page_of, page_without_total


def get(db: Session, board_id: int, include_deleted: bool = False) -> Board | None:
    board = db.get(Board, board_id)
    if board is None:
        return None
    if board.deleted_at is not None and not include_deleted:
        return None
    return board


def _search_stmt(q: str | None = None, client_id: int | None = None):
    """Условия отбора досок — общие для списка раздела и для палитры.

    Склейки `search_text` у досок НЕТ намеренно, хотя у клиентов и заявок она
    появилась. Досок в системе единицы-сотни: полный проход по ним стоит
    миллисекунды, а колонка платится записью на каждом сохранении и местом на
    диске. Приём применяется там, где счёт замерен в секундах.
    """
    stmt = select(Board).where(Board.deleted_at.is_(None))
    if q:
        needle = q.strip()
        stmt = stmt.where(or_(contains(Board.title, needle), contains(Board.description, needle)))
    if client_id:
        stmt = stmt.where(Board.client_id == client_id)
    return stmt.order_by(Board.updated_at.desc())


def search(
    db: Session,
    q: str | None = None,
    client_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Board], int]:
    return page_of(db, _search_stmt(q, client_id), page=page, per_page=per_page)


def search_top(
    db: Session, q: str | None = None, *, page: int = 1, per_page: int = 6
) -> tuple[list[Board], bool]:
    """Страница досок для командной палитры — без счёта найденного.

    `page` появился по беде: палитра показывала шесть досок и на этом
    заканчивалась, а «показать ещё» ей взять было неоткуда — запрос жёстко
    просил первую страницу. Досок под «be» было больше шести, и седьмую нельзя
    было увидеть никак.
    """
    return page_without_total(db, _search_stmt(q), page=page, per_page=per_page)


def works_count_by_board(db: Session, board_ids: list[int]) -> dict[int, int]:
    """Сколько работ на каждой доске — одним запросом на весь список.

    Досок в списке до двухсот, и спрашивать про каждую отдельно значит двести
    запросов на одну страницу. Доски без работ в ответе нет: «ноль» подставляет
    тот, кто спрашивал, — ему виднее, отличать ли пустую доску от отсутствующей.
    """
    if not board_ids:
        return {}
    rows = db.execute(
        select(Work.board_id, func.count())
        .where(Work.board_id.in_(board_ids))
        .group_by(Work.board_id)
    ).all()
    return {board_id: count for board_id, count in rows}


def works_by_board(db: Session, board_ids: list[int], only_ready: bool = False) -> dict[int, list[Work]]:
    """Работы досок разом, в том же порядке, что и поштучно."""
    if not board_ids:
        return {}
    stmt = select(Work).where(Work.board_id.in_(board_ids))
    if only_ready:
        stmt = stmt.where(Work.status == "ready")
    grouped: dict[int, list[Work]] = {}
    for work in db.scalars(stmt.order_by(Work.sort_order.asc(), Work.id.asc())):
        grouped.setdefault(work.board_id, []).append(work)
    return grouped


def list_works(db: Session, board_id: int, only_ready: bool = False) -> list[Work]:
    stmt = select(Work).where(Work.board_id == board_id)
    if only_ready:
        stmt = stmt.where(Work.status == "ready")
    stmt = stmt.order_by(Work.sort_order.asc(), Work.id.asc())
    return list(db.scalars(stmt))


def get_work(db: Session, board_id: int, work_id: int) -> Work | None:
    work = db.get(Work, work_id)
    if work is None or work.board_id != board_id:
        return None
    return work


def next_sort_order(db: Session, board_id: int) -> int:
    current = db.scalar(
        select(func.max(Work.sort_order)).where(Work.board_id == board_id)
    )
    return (current or 0) + 10


def for_deal(db: Session, deal_id: int) -> list[Board]:
    """Доски, сделанные по этой заявке. Свежие выше."""
    return list(
        db.scalars(
            select(Board)
            .where(Board.deal_id == deal_id, Board.deleted_at.is_(None))
            .order_by(Board.created_at.desc(), Board.id.desc())
        )
    )


def count_works(db: Session, board_id: int) -> int:
    return db.scalar(select(func.count()).where(Work.board_id == board_id).select_from(Work)) or 0


def get_work_by_uid(db: Session, work_uid: str) -> Work | None:
    """Работа по имени её каталога на диске — так к ней приходят за файлом."""
    if not work_uid:
        return None
    return db.scalar(select(Work).where(Work.work_uid == work_uid))


def get_work_by_id(db: Session, work_id: int) -> Work | None:
    """Работа без оглядки на доску — для менеджера файлов, где доски ещё не знают."""
    return db.get(Work, work_id)


def works_with_board_title(db: Session) -> list[tuple[Work, str]]:
    """Все работы живых досок вместе с названием доски — одним запросом.

    Название приходит соединением, а не отдельным запросом на каждую работу:
    менеджер файлов открывают, когда на диске тесно, то есть работ там тысячи.
    """
    return list(
        db.execute(
            select(Work, Board.title)
            .join(Board, Board.id == Work.board_id)
            .where(Board.deleted_at.is_(None))
            .order_by(Work.created_at.desc(), Work.id.desc())
        ).all()
    )


def works_of_deleted_boards(db: Session) -> list[tuple[Board, Work]]:
    """Работы досок, лежащих в корзине, — чтобы посчитать занятое ими место."""
    return list(
        db.execute(
            select(Board, Work)
            .join(Work, Work.board_id == Board.id)
            .where(Board.deleted_at.is_not(None))
        ).all()
    )


def deleted_boards(db: Session) -> list[Board]:
    return list(db.scalars(select(Board).where(Board.deleted_at.is_not(None))))
