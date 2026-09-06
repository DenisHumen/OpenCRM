"""Запросы по напоминаниям.

Границы «сегодня» и «недели» приходят сюда готовым моментом времени, а не
считаются здесь. Так и должно быть: для человека «на сегодня» — это «до конца
рабочего дня», и решать, от чего отсчитывать, — дело сервиса, знающего про
часовые пояса. Репозиторий только спрашивает базу.

Счётчики считаются в базе, а не в Python. Прежний `len(list(...))` поднимал в
память каждую незакрытую задачу и делал это четыре раза за один заход на любую
страницу: на 13 тысячах задач счётчики в меню стоили 447 мс, тем же условием
через `count(*)` — 16 мс.
"""

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from database.models import Task, TaskFile
from database.models.task import VAZHNOSTI

#: Виды списков, которыми пользуются каждый день.
SCOPE_OPEN = "open"
SCOPE_OVERDUE = "overdue"
SCOPE_TODAY = "today"
SCOPE_WEEK = "week"
SCOPE_DONE = "done"

#: Важность в число — для сортировки. Словами она читается, числом сортируется.
_PO_VAZHNOSTI = case(
    {slovo: nomer for nomer, slovo in enumerate(VAZHNOSTI)},
    value=Task.vazhnost,
    else_=len(VAZHNOSTI),
)


def get(db: Session, task_id: int) -> Task | None:
    return db.get(Task, task_id)


def zapert(db: Session, task_id: int) -> Task | None:
    """Напоминание под замком до конца транзакции.

    Нужно там, где между «оно ещё есть» и записью рядом с ним успевает пройти
    чужое удаление: вложение доехало бы до диска, а строку унёс бы каскад —
    и файл остался бы на диске навсегда (§3 CLAUDE.md).
    """
    return db.scalar(select(Task).where(Task.id == task_id).with_for_update())


def _open_only(query: Select) -> Select:
    return query.where(Task.done_at.is_(None))


def search(
    db: Session,
    *,
    scope: str = SCOPE_OPEN,
    now,
    until=None,
    assignee_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    limit: int,
) -> list[Task]:
    """Список напоминаний. `until` — граница срока для «сегодня» и «недели»."""
    query = select(Task)

    if scope == SCOPE_DONE:
        query = query.where(Task.done_at.is_not(None)).order_by(Task.done_at.desc(), Task.id.desc())
    else:
        query = _open_only(query)
        if scope == SCOPE_OVERDUE:
            query = query.where(Task.due_at.is_not(None), Task.due_at < now)
        elif until is not None:
            query = query.where(Task.due_at.is_not(None), Task.due_at < until)
        # Порядок: просроченное, важность, срок. Важность выше срока — «срочно»
        # человек ставит руками именно затем, чтобы это увидели раньше прочего.
        # Но просроченное всё равно первое, и это не про красоту: у списка есть
        # потолок в двести строк, и две сотни бессрочных «срочно» вытеснили бы
        # из выдачи ВСЁ просроченное — то есть обещания, которые уже нарушены.
        prosrocheno = case((and_(Task.due_at.is_not(None), Task.due_at < now), 0), else_=1)
        query = query.order_by(
            prosrocheno, _PO_VAZHNOSTI, Task.due_at.is_(None), Task.due_at.asc(), Task.id.desc()
        )

    if assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)
    if client_id is not None:
        query = query.where(Task.client_id == client_id)
    if deal_id is not None:
        query = query.where(Task.deal_id == deal_id)

    return list(db.scalars(query.limit(limit)))


def counters(db: Session, *, user_id: int, now, today_until) -> dict[str, int]:
    """Счётчики для навигации: без них в задачи заходят «на всякий случай».

    Считаются мимо потолка списка нарочно: дело счётчика — сказать, сколько
    есть всего, а не сколько поместилось на экран.
    """

    def count(*conditions) -> int:
        query = _open_only(select(func.count(Task.id)))
        for condition in conditions:
            query = query.where(condition)
        return db.scalar(query) or 0

    # Ничей — тоже мой: задача без исполнителя лежит на том, кто её увидит, и
    # спрятать её из счётчика значило бы дать ей потеряться совсем.
    mine = or_(Task.assignee_id == user_id, Task.assignee_id.is_(None))
    return {
        "overdue": count(Task.due_at.is_not(None), Task.due_at < now),
        "today": count(Task.due_at.is_not(None), Task.due_at < today_until),
        "mine": count(mine),
        "open": count(),
    }


# --- вложения -----------------------------------------------------------------


def files_of(db: Session, task_id: int) -> list[TaskFile]:
    return list(
        db.scalars(
            select(TaskFile)
            .where(TaskFile.task_id == task_id)
            .order_by(TaskFile.created_at.desc(), TaskFile.id.desc())
        ).all()
    )


def counts_of_files(db: Session, task_ids) -> dict[int, int]:
    """Сколько вложений у каждого напоминания — одним запросом на список."""
    task_ids = {int(i) for i in task_ids if i}
    if not task_ids:
        return {}
    rows = db.execute(
        select(TaskFile.task_id, func.count(TaskFile.id))
        .where(TaskFile.task_id.in_(task_ids))
        .group_by(TaskFile.task_id)
    ).all()
    return {task_id: count for task_id, count in rows}


def nepustye_zametki(db: Session, task_ids) -> set[int]:
    """У кого из напоминаний подробности не пусты — одним запросом на список.

    Отдельным запросом, потому что сами подробности `deferred`: в списке от них
    спрашивают только «есть ли», а весят они до потолка каждая.
    """
    task_ids = {int(i) for i in task_ids if i}
    if not task_ids:
        return set()
    return set(
        db.scalars(
            select(Task.id).where(Task.id.in_(task_ids), func.char_length(Task.note) > 0)
        ).all()
    )


def get_file(db: Session, task_id: int, file_id: int) -> TaskFile | None:
    return db.scalar(
        select(TaskFile).where(TaskFile.id == file_id, TaskFile.task_id == task_id)
    )


def add_file(db: Session, file: TaskFile) -> TaskFile:
    db.add(file)
    db.flush()
    return file


def drop_file(db: Session, file: TaskFile) -> None:
    db.delete(file)
    db.flush()
