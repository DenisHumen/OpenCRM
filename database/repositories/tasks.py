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

from sqlalchemy import Select, and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import Task, TaskEvent, TaskFile, TaskMember, TaskSignal, TaskStep, TaskUrl
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


def po_nomeram(db: Session, task_ids) -> dict[int, Task]:
    """{номер: напоминание} пачкой.

    Список, у которого напоминание висит на каждой строке (ключи двухфакторки),
    иначе добирал бы их по одному: двенадцать карточек — двенадцать запросов.
    """
    nomera = [int(x) for x in task_ids if x]
    if not nomera:
        return {}
    return {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(nomera)))}


def zapert(db: Session, task_id: int) -> Task | None:
    """Напоминание под замком до конца транзакции.

    Нужно там, где между «оно ещё есть» и записью рядом с ним успевает пройти
    чужое удаление: вложение доехало бы до диска, а строку унёс бы каскад —
    и файл остался бы на диске навсегда (§3 CLAUDE.md).
    """
    return db.scalar(select(Task).where(Task.id == task_id).with_for_update())


def _open_only(query: Select) -> Select:
    return query.where(Task.done_at.is_(None))


def _vidimye(query: Select, user_id: int, vse: bool) -> Select:
    """Своё, общая полка и то, где человек при напоминании. `vse` — право видеть все."""
    if vse:
        return query
    svoi = select(TaskMember.task_id).where(TaskMember.user_id == user_id)
    return query.where(or_(Task.obshchee.is_(True), Task.id.in_(svoi)))


#: Чьи напоминания показать: все видимые, звонящие мне, поставленные мной другим, общая полка.
KTO = ("vse", "moi", "postavil", "polka")


def _kto(query: Select, user_id: int, kto: str) -> Select:
    if kto == "moi":
        mne = select(TaskMember.task_id).where(
            TaskMember.user_id == user_id, TaskMember.poluchaet.is_(True)
        )
        return query.where(Task.id.in_(mne))
    if kto == "postavil":
        drugim = select(TaskMember.task_id).where(
            TaskMember.user_id != user_id, TaskMember.poluchaet.is_(True)
        )
        return query.where(Task.created_by == user_id, Task.id.in_(drugim))
    if kto == "polka":
        return query.where(Task.obshchee.is_(True))
    return query


def search(
    db: Session,
    *,
    user_id: int,
    vse: bool,
    scope: str = SCOPE_OPEN,
    kto: str = "vse",
    now,
    until=None,
    privyazki: dict | None = None,
    limit: int,
) -> list[Task]:
    """Список напоминаний, видимых человеку. `until` — граница срока для «сегодня» и «недели»."""
    query = _kto(_vidimye(select(Task), user_id, vse), user_id, kto)

    if scope == SCOPE_DONE:
        query = query.where(Task.done_at.is_not(None)).order_by(Task.done_at.desc(), Task.id.desc())
    else:
        query = _open_only(query)
        if scope == SCOPE_OVERDUE:
            query = query.where(Task.due_at.is_not(None), Task.due_at < now)
        elif until is not None:
            query = query.where(Task.due_at.is_not(None), Task.due_at < until)
        prosrocheno = case((and_(Task.due_at.is_not(None), Task.due_at < now), 0), else_=1)
        query = query.order_by(
            prosrocheno, _PO_VAZHNOSTI, Task.due_at.is_(None), Task.due_at.asc(), Task.id.desc()
        )

    for kolonka, nomer in (privyazki or {}).items():
        query = query.where(getattr(Task, kolonka) == nomer)

    return list(db.scalars(query.limit(limit)))


def v_okne(db: Session, *, user_id: int, vse: bool, s, po, limit: int) -> list[Task]:
    """Для календаря: открытые со сроком до конца окна и закрытые внутри окна.

    Открытые — все до `po`, а не с `s`: просроченное и повторяющееся тоже нужно
    календарю. Разы повторяющихся по дням раскладывает сервис.
    """
    query = _vidimye(select(Task), user_id, vse).where(
        or_(
            and_(Task.done_at.is_(None), Task.due_at.is_not(None), Task.due_at < po),
            and_(Task.done_at.is_not(None), Task.done_at >= s, Task.done_at < po),
        )
    )
    return list(db.scalars(query.order_by(Task.due_at.asc(), Task.id.asc()).limit(limit)))


def counters(db: Session, *, user_id: int, vse: bool, now, today_until) -> dict[str, int]:
    """Счётчики для навигации: без них в напоминания заходят «на всякий случай».

    Считаются мимо потолка списка нарочно: дело счётчика — сказать, сколько
    есть всего, а не сколько поместилось на экран.
    """

    def count(*conditions) -> int:
        query = _vidimye(_open_only(select(func.count(Task.id))), user_id, vse)
        for condition in conditions:
            query = query.where(condition)
        return db.scalar(query) or 0

    mne = select(TaskMember.task_id).where(
        TaskMember.user_id == user_id, TaskMember.poluchaet.is_(True)
    )
    return {
        "overdue": count(Task.due_at.is_not(None), Task.due_at < now),
        "today": count(Task.due_at.is_not(None), Task.due_at < today_until),
        "mine": count(Task.id.in_(mne)),
        "open": count(),
    }


# --- люди ---------------------------------------------------------------------


def lyudi(db: Session, task_ids) -> dict[int, list[TaskMember]]:
    """{напоминание: люди при нём} одним запросом на список."""
    nomera = {int(x) for x in task_ids if x}
    if not nomera:
        return {}
    itog: dict[int, list[TaskMember]] = {n: [] for n in nomera}
    for chelovek in db.scalars(
        select(TaskMember).where(TaskMember.task_id.in_(nomera)).order_by(TaskMember.id)
    ):
        itog[chelovek.task_id].append(chelovek)
    return itog


def chlen(db: Session, task_id: int, user_id: int) -> TaskMember | None:
    return db.scalar(
        select(TaskMember).where(TaskMember.task_id == task_id, TaskMember.user_id == user_id)
    )


def dobavit(db: Session, obj) -> None:
    db.add(obj)
    db.flush()


def ubrat(db: Session, obj) -> None:
    db.delete(obj)
    db.flush()


# --- шаги, ссылки, история ----------------------------------------------------


def shagi(db: Session, task_id: int) -> list[TaskStep]:
    return list(db.scalars(
        select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.poryadok, TaskStep.id)
    ))


def shag(db: Session, task_id: int, step_id: int) -> TaskStep | None:
    return db.scalar(select(TaskStep).where(TaskStep.id == step_id, TaskStep.task_id == task_id))


def shagi_schyot(db: Session, task_ids) -> dict[int, tuple[int, int]]:
    """{напоминание: (сделано шагов, всего)} — для строки списка."""
    nomera = {int(x) for x in task_ids if x}
    if not nomera:
        return {}
    rows = db.execute(
        select(TaskStep.task_id, func.count(TaskStep.sdelan_at), func.count(TaskStep.id))
        .where(TaskStep.task_id.in_(nomera))
        .group_by(TaskStep.task_id)
    ).all()
    return {task_id: (sdelano, vsego) for task_id, sdelano, vsego in rows}


def ssylki(db: Session, task_id: int) -> list[TaskUrl]:
    return list(db.scalars(
        select(TaskUrl).where(TaskUrl.task_id == task_id).order_by(TaskUrl.poryadok, TaskUrl.id)
    ))


def ssylka(db: Session, task_id: int, url_id: int) -> TaskUrl | None:
    return db.scalar(select(TaskUrl).where(TaskUrl.id == url_id, TaskUrl.task_id == task_id))


def istoriya(db: Session, task_id: int, limit: int = 100) -> list[TaskEvent]:
    return list(db.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
        .limit(limit)
    ))


def sdelannye_v_okne(db: Session, task_ids, s, po) -> list[TaskEvent]:
    """Закрытые разы повторяющихся внутри окна — календарь показывает и прошлое."""
    nomera = {int(x) for x in task_ids if x}
    if not nomera:
        return []
    return list(db.scalars(
        select(TaskEvent).where(
            TaskEvent.task_id.in_(nomera),
            TaskEvent.vid == "done",
            TaskEvent.srok >= s,
            TaskEvent.srok < po,
        )
    ))


# --- звонки -------------------------------------------------------------------


def kandidaty_zvonka(db: Session, s, po) -> list[Task]:
    """Открытые напоминания, у которых в окне [s, po] может найтись минута звонка."""
    return list(db.scalars(
        select(Task).where(
            Task.done_at.is_(None), Task.due_at.is_not(None), Task.due_at >= s, Task.due_at <= po
        )
    ))


def otlozhennye_v_okne(db: Session, s, po) -> list[TaskMember]:
    """Кого пора разбудить после «отложить»."""
    return list(db.scalars(
        select(TaskMember).where(TaskMember.otlozheno_do >= s, TaskMember.otlozheno_do <= po)
    ))


def zvonok_zapisat(db: Session, signal: TaskSignal) -> bool:
    """Записать звонок. Ключ (напоминание, человек, минута) уже занят — позвонил другой
    процесс, и второго звонка не будет: False."""
    try:
        with db.begin_nested():
            db.add(signal)
            db.flush()
    except IntegrityError:
        return False
    return True


def zvonki_nerazobrannye(db: Session, user_id: int, s) -> list[TaskSignal]:
    """Звонки человеку, которых он ещё не видел; старее `s` не показываем."""
    return list(db.scalars(
        select(TaskSignal)
        .where(TaskSignal.user_id == user_id, TaskSignal.prinyato.is_(None), TaskSignal.created_at >= s)
        .order_by(TaskSignal.moment.desc(), TaskSignal.id.desc())
        .limit(50)
    ))


def zvonki_prinyat(db: Session, user_id: int, now, *, signal_ids=None, task_id=None) -> int:
    """Отметить звонки увиденными: названные, все по напоминанию или все свои."""
    query = update(TaskSignal).where(TaskSignal.user_id == user_id, TaskSignal.prinyato.is_(None))
    if signal_ids is not None:
        query = query.where(TaskSignal.id.in_([int(x) for x in signal_ids]))
    if task_id is not None:
        query = query.where(TaskSignal.task_id == task_id)
    return db.execute(query.values(prinyato=now)).rowcount or 0


def zvonki_prinyat_vsem(db: Session, task_id: int, now) -> None:
    """Раз закрыт — звонки по нему смолкают у всех, а не только у закрывшего."""
    db.execute(
        update(TaskSignal)
        .where(TaskSignal.task_id == task_id, TaskSignal.prinyato.is_(None))
        .values(prinyato=now)
    )


def prinyal_li(db: Session, task_id: int, user_id: int, srok) -> bool:
    """Отозвался ли человек хоть на один звонок этого раза — тогда настойчивость смолкает."""
    return (
        db.scalar(
            select(func.count(TaskSignal.id)).where(
                TaskSignal.task_id == task_id,
                TaskSignal.user_id == user_id,
                TaskSignal.srok == srok,
                TaskSignal.prinyato.is_not(None),
            )
        )
        or 0
    ) > 0


def zvonki_ubrat_starye(db: Session, do) -> int:
    return db.execute(delete(TaskSignal).where(TaskSignal.created_at < do)).rowcount or 0


def sleduyushchiy_poryadok(db: Session, task_id: int) -> int:
    """Следующий номер по порядку для шага или ссылки — в конец."""
    shagov = db.scalar(select(func.max(TaskStep.poryadok)).where(TaskStep.task_id == task_id)) or 0
    ssylok = db.scalar(select(func.max(TaskUrl.poryadok)).where(TaskUrl.task_id == task_id)) or 0
    return max(shagov, ssylok) + 1


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
