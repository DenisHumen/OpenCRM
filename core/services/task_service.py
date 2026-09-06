"""Напоминания: перезвонить, отправить счёт, забрать технику."""

from datetime import timedelta
from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import references
from core.security import tokens
from core.services import audit_service, client_service
from core.utils import now_utc, to_utc_naive
from database.models import Task, TaskFile, User
from database.models.task import VAZHNOSTI, VAZHNOST_PO_UMOLCHANIYU
from database.repositories import tasks as tasks_repo

MAX_TITLE = 300

#: Потолок подробностей. Не про размер колонки (там `MEDIUMTEXT`, 16 МБ), а
#: про смысл: подробности читают глазами, и разбор длиннее романа никто не
#: прочтёт. Лишнее не режется молча — отвергается: файл, в котором тихо
#: недостаёт половины, хуже отсутствующего файла.
MAX_NOTE = 20_000

#: Что кладут в карточку: снимок «что привезли» и видео «как гудит». Тот же
#: перечень, что у вложений возврата, — приёмка общая.
VLOZHENIYA = {"jpg", "jpeg", "png", "webp", "gif", "mp4", "webm", "mov"}

#: Потолок списка напоминаний. Счётчики в меню считают мимо него: их дело —
#: сказать, сколько есть всего, а не сколько поместилось на экран.
LIST_LIMIT = 200


def get_task(db: Session, task_id: int) -> Task:
    task = tasks_repo.get(db, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    return task


def create(db: Session, data: dict, author: User) -> Task:
    title = (data.get("title") or "").strip()
    if not title:
        raise errors.ValidationError("Title is required", code="title_required")

    task = Task(
        title=title[:MAX_TITLE],
        vazhnost=_vazhnost(data["vazhnost"]) if data.get("vazhnost") else VAZHNOST_PO_UMOLCHANIYU,
        note=_zametka(data.get("note")),
        due_at=to_utc_naive(data.get("due_at")),
        # Не указали исполнителя — задача на авторе. «Ничья» задача не делается
        # никем: каждый считает, что её возьмёт кто-то другой.
        assignee_id=(
            references.user(
                db, data["assignee_id"], code="assignee_not_found", message="Assignee not found"
            )
            if "assignee_id" in data
            else author.id
        ),
        client_id=references.client(db, data.get("client_id")),
        deal_id=references.deal(db, data.get("deal_id")),
        created_by=author.id,
    )
    db.add(task)
    db.flush()
    _skazat_ispolnitelyu(db, task, author)
    return task


def update(db: Session, task_id: int, data: dict, author: User | None = None) -> Task:
    task = get_task(db, task_id)
    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise errors.ValidationError("Title is required", code="title_required")
        task.title = title[:MAX_TITLE]
    if "vazhnost" in data and data["vazhnost"] is not None:
        task.vazhnost = _vazhnost(data["vazhnost"])
    if "note" in data and data["note"] is not None:
        task.note = _zametka(data["note"])
    if "due_at" in data:
        task.due_at = to_utc_naive(data["due_at"])
    if "assignee_id" in data:
        task.assignee_id = references.user(
            db, data["assignee_id"], code="assignee_not_found", message="Assignee not found"
        )
    if "client_id" in data:
        task.client_id = references.client(db, data["client_id"])
    if "deal_id" in data:
        task.deal_id = references.deal(db, data["deal_id"])
    if "is_done" in data and data["is_done"] is not None:
        # Дата закрытия отвечает сразу на два вопроса: сделано ли и когда.
        task.done_at = now_utc() if data["is_done"] else None
    db.flush()
    if "assignee_id" in data:
        _skazat_ispolnitelyu(db, task, author)
    return task


def _vazhnost(slovo) -> str:
    """Важность — одно из четырёх слов. Чужое отвергаем, а не подменяем тихо:
    молча съеденное «срочно» — это напоминание, которое не заметят. Пустая
    строка тоже чужая: «не указали» — это отсутствие поля, а не пустое поле."""
    if slovo not in VAZHNOSTI:
        raise errors.ValidationError("Unknown importance", code="vazhnost_unknown")
    return slovo


def _zametka(tekst) -> str:
    """Подробности с потолком. Отказ, а не обрезка: человек, вставивший разбор
    на тридцать тысяч знаков, обязан узнать, что половина не сохранилась."""
    tekst = tekst or ""
    if len(tekst) > MAX_NOTE:
        raise errors.ValidationError("Details are too long", code="note_too_long")
    return tekst


def _skazat_ispolnitelyu(db: Session, task: Task, author: User | None) -> None:
    """Напоминание повесили на другого — он узнаёт об этом сразу, а не в срок."""
    from core.services import notification_service
    from database.repositories import users as users_repo

    if not task.assignee_id or (author is not None and task.assignee_id == author.id):
        return
    komu = users_repo.get_by_id(db, task.assignee_id)
    if komu is not None:
        notification_service.notify(db, [komu], "task_assigned", {"title": task.title}, "/tasks")


def delete(db: Session, task_id: int, actor: User | None = None) -> None:
    # Замок, а не просто чтение: пока мы перечисляем вложения, соседний запрос
    # успевает залить ещё одно. Его строку унёс бы каскад, а файл остался бы на
    # диске навсегда — потому что в нашем списке его не было (§3 CLAUDE.md).
    task = tasks_repo.zapert(db, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    for file in tasks_repo.files_of(db, task.id):
        # Снимок исчезает вместе с напоминанием, и это тоже удаление файла:
        # без записи в журнале осталась бы дорожка «снести напоминание —
        # снести вложение молча».
        audit_service.record_deletion(
            db,
            actor=actor,
            entity_type=audit_service.ENTITY_FILE,
            entity_id=file.id,
            entity_label=file.original_name,
        )
        _snyat_s_diska_posle_fiksatsii(db, file_path_on_disk(file))
    db.delete(task)
    db.flush()


def search(
    db: Session,
    scope: str = "open",
    assignee_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    limit: int = LIST_LIMIT,
) -> list[Task]:
    """Списки, которыми пользуются каждый день.

    `scope`: open | overdue | today | week | done. Границы «сегодня» и «недели»
    считаются от текущего момента, а не от полуночи по UTC: для человека
    «на сегодня» — это «до конца рабочего дня», и сдвигать эту границу на
    произвольное число часов из-за зоны сервера нельзя.
    """
    now = now_utc()
    horizon = {"today": timedelta(days=1), "week": timedelta(days=7)}.get(scope)
    return tasks_repo.search(
        db,
        scope=scope,
        now=now,
        until=now + horizon if horizon else None,
        assignee_id=assignee_id,
        client_id=client_id,
        deal_id=deal_id,
        limit=limit,
    )


def summary(db: Session, user: User) -> dict:
    """Счётчики для навигации: без них в задачи заходят «на всякий случай»."""
    now = now_utc()
    return tasks_repo.counters(
        db, user_id=user.id, now=now, today_until=now + timedelta(days=1)
    )


# --- вложения -----------------------------------------------------------------


def files(db: Session, task_id: int) -> list[TaskFile]:
    return tasks_repo.files_of(db, task_id)


def files_counts(db: Session, task_ids) -> dict[int, int]:
    return tasks_repo.counts_of_files(db, task_ids)


def zametki_est(db: Session, task_ids) -> set[int]:
    return tasks_repo.nepustye_zametki(db, task_ids)


def _files_dir(task_id: int) -> Path:
    return get_settings().task_files_dir.joinpath(str(task_id))


def file_path_on_disk(file: TaskFile) -> Path:
    return _files_dir(file.task_id).joinpath(f"{file.file_uid}{Path(file.original_name).suffix}")


def add_file(db: Session, task_id: int, uploader: User, original_name: str, content: bytes) -> TaskFile:
    """Фото или видео к напоминанию. Приёмка общая с файлами клиента.

    Замок на напоминание — пара к такому же в `delete`: без него заливка и
    снос расходятся так, что файл остаётся на диске без строки в базе.
    """
    task = tasks_repo.zapert(db, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    ext, content = client_service.proverit_vlozhenie(original_name, content, VLOZHENIYA)
    file = tasks_repo.add_file(
        db,
        TaskFile(
            task_id=task.id,
            uploaded_by=uploader.id,
            file_uid=tokens.new_file_uid(),
            original_name=Path(original_name).name[:255],
            # Присланный `Content-Type` не сохраняем вовсе: его выбирает тот,
            # кто загружает, а уходит он в заголовок ответа сотруднику.
            mime=client_service.MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream"),
            size_bytes=len(content),
        ),
    )
    directory = _files_dir(task.id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path_on_disk(file).write_bytes(content)
    return file


def mime_dlya_otdachi(file: TaskFile) -> str:
    """Чем отдавать вложение. Считается из имени, а не берётся из записи — тот
    же довод, что у файлов клиента: заголовок ответа не должен зависеть от
    того, что когда-то положили в строку."""
    ext = Path(file.original_name).suffix.lstrip(".").lower()
    return client_service.MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream")


def get_file(db: Session, task_id: int, file_id: int) -> TaskFile:
    file = tasks_repo.get_file(db, task_id, file_id)
    if file is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    return file


def delete_file(db: Session, task_id: int, file_id: int, actor: User) -> None:
    file = get_file(db, task_id, file_id)
    _snyat_s_diska_posle_fiksatsii(db, file_path_on_disk(file))
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=file.id,
        entity_label=file.original_name,
    )
    tasks_repo.drop_file(db, file)


def _snyat_s_diska_posle_fiksatsii(db: Session, path: Path) -> None:
    # После коммита, а не сразу: откат вернул бы строку, а файла уже нет.
    @sa_event.listens_for(db, "after_commit", once=True)
    def _ubrat(_session) -> None:
        path.unlink(missing_ok=True)
        # И пустой каталог следом. У возвратов так не делали, но их заводят
        # руками и их десятки, а напоминания заводятся сами — от пропущенного
        # звонка и заявки с сайта: за год это десятки тысяч пустых каталогов,
        # по которым потом ходит подсчёт занятого места.
        try:
            path.parent.rmdir()
        except OSError:
            # Не пуст или уже снят — обе беды не беды.
            pass
