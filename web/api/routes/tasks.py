"""Напоминания: списки, календарь, карточка, раз, люди, шаги, ссылки, звонки, вложения.

Устройство — docs/bloki/29-napominaniya.md; таблица ручек — docs/osnovy/04-api.md.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import modules_service, permissions_service, task_service
from core.utils import to_utc_naive
from database.models import Task, User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo
from database.repositories import tasks as tasks_repo
from database.repositories import users as users_repo
from database.repositories import warehouse as warehouse_repo
from web.api import schemas
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_module("tasks"))]
)


class SsylkaIn(BaseModel):
    url: str
    title: str | None = None


class TaskIn(BaseModel):
    title: str
    vazhnost: str | None = None
    note: str | None = None
    due_at: datetime | None = None
    ves_den: bool | None = None
    poyas: str | None = None
    povtor: str | None = None
    povtor_posle: bool | None = None
    opovesheniya: str | list[int] | None = None
    nastoychivo: int | None = None
    obshchee: bool | None = None
    poluchateli: list[int] | None = None
    nablyudateli: list[int] | None = None
    #: Прежнее поле, одним числом: его шлют соседние блоки и старые экраны.
    assignee_id: int | None = None
    #: «Каждому своё»: по напоминанию на получателя.
    kazhdomu: bool | None = None
    client_id: int | None = None
    deal_id: int | None = None
    document_id: int | None = None
    product_id: int | None = None
    board_id: int | None = None
    shagi: list[str] | None = None
    ssylki: list[SsylkaIn] | None = None


class TaskPatchIn(BaseModel):
    title: str | None = None
    vazhnost: str | None = None
    note: str | None = None
    due_at: datetime | None = None
    ves_den: bool | None = None
    poyas: str | None = None
    povtor: str | None = None
    povtor_posle: bool | None = None
    opovesheniya: str | list[int] | None = None
    nastoychivo: int | None = None
    obshchee: bool | None = None
    poluchateli: list[int] | None = None
    nablyudateli: list[int] | None = None
    assignee_id: int | None = None
    client_id: int | None = None
    deal_id: int | None = None
    document_id: int | None = None
    product_id: int | None = None
    board_id: int | None = None
    is_done: bool | None = None


class OtlozhitIn(BaseModel):
    do: datetime


class ShagIn(BaseModel):
    text: str


class ShagPatchIn(BaseModel):
    text: str | None = None
    sdelan: bool | None = None


class ZvonkiIn(BaseModel):
    ids: list[int]


def _out(db: Session, tasks: list[Task], user: User) -> list[dict]:
    """Строки списка с тем, что собирается пачкой: люди, имена привязок, счёт вложений.

    Заголовок заявки — в ОБЛАСТИ смотрящего: иначе перебором `deal_id` вычитывался
    бы список чужих заявок мимо `deals.view_others`. Номер остаётся — он ничего не
    рассказывает. Имена выключенных блоков не отдаются вовсе (docs/bloki/29 §7).
    """
    nomera = [t.id for t in tasks]
    lyudi = tasks_repo.lyudi(db, nomera)
    imena = {u.id: u.name for u in users_repo.get_many(db, {c.user_id for v in lyudi.values() for c in v})}
    vkl = {blok: modules_service.is_enabled(db, blok) for blok in ("documents", "warehouse", "boards")}
    klienty = clients_repo.names_by_ids(db, [t.client_id for t in tasks if t.client_id])
    zayavki = {
        d.id: d.title
        for d in deals_repo.by_ids(
            db, {t.deal_id for t in tasks if t.deal_id}, permissions_service.deals_scope(db, user)
        )
    }
    bumagi = documents_repo.podpisi_po_nomeram(db, [t.document_id for t in tasks]) if vkl["documents"] else {}
    tovary = (
        {p.id: p.name for p in warehouse_repo.products_by_ids(db, {t.product_id for t in tasks if t.product_id})}
        if vkl["warehouse"]
        else {}
    )
    doski = boards_repo.nazvaniya_po_nomeram(db, [t.board_id for t in tasks]) if vkl["boards"] else {}
    vlozheniya = task_service.files_counts(db, nomera)
    zametki = task_service.zametki_est(db, nomera)
    shagi = tasks_repo.shagi_schyot(db, nomera)
    itog = []
    for t in tasks:
        svoi = lyudi.get(t.id, [])
        itog.append(schemas.task_out(t, {
            "lyudi": [
                {
                    "user_id": c.user_id, "name": imena.get(c.user_id, ""), "vladelets": c.vladelets,
                    "poluchaet": c.poluchaet, "otlozheno_do": schemas._iso(c.otlozheno_do),
                }
                for c in svoi
            ],
            "moyo": any(c.user_id == user.id and c.poluchaet for c in svoi),
            "client_name": klienty.get(t.client_id),
            "deal_title": zayavki.get(t.deal_id),
            "document_title": (bumagi.get(t.document_id) or (None, None))[0],
            "document_kind": (bumagi.get(t.document_id) or (None, None))[1],
            "product_name": tovary.get(t.product_id),
            "board_title": doski.get(t.board_id),
            "files_count": vlozheniya.get(t.id, 0),
            "note_est": t.id in zametki,
            "shagi": list(shagi.get(t.id, (0, 0))),
        }))
    return itog


def _pravit(db: Session, user: User, task_id: int) -> Task:
    return task_service.dostupnoe(db, user, task_id)


def _lyudi_menyat(db: Session, user: User, task: Task) -> None:
    """Состав людей меняет владелец или тот, кто видит все: получатель чужого
    напоминания не вправе переписать, кому оно звонит."""
    if task_service.vidit_vse(db, user) or task.obshchee:
        return
    chelovek = tasks_repo.chlen(db, task.id, user.id)
    if chelovek is None or not chelovek.vladelets:
        raise errors.ForbiddenError("Only the owner changes people", code="task_ne_vladelets")


@router.get("")
def list_tasks(
    scope: str = Query(default="open"),
    kto: str = Query(default="vse"),
    client_id: int | None = None,
    deal_id: int | None = None,
    document_id: int | None = None,
    product_id: int | None = None,
    board_id: int | None = None,
    user: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    privyazki = {
        k: v for k, v in {
            "client_id": client_id, "deal_id": deal_id, "document_id": document_id,
            "product_id": product_id, "board_id": board_id,
        }.items() if v
    }
    tasks = task_service.search(db, user, scope=scope, kto=kto, privyazki=privyazki)
    return {"items": _out(db, tasks, user)}


@router.get("/summary")
def summary(user: User = Depends(require_perm("tasks", "view")), db: Session = Depends(get_db)):
    """Счётчики для навигации. Отдельной точкой, потому что их спрашивают
    часто и без списка: полоса с числом просроченных висит на каждом экране."""
    return task_service.summary(db, user)


@router.get("/calendar")
def calendar(
    s: datetime,
    po: datetime,
    user: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    """Разы в окне — по одному на каждый раз повторяющегося. Окно до двух месяцев:
    месяц с хвостами соседних недель."""
    razy = task_service.kalendar(db, user, to_utc_naive(s), to_utc_naive(po))
    stroki = {row["id"]: row for row in _out(db, list({r["task"].id: r["task"] for r in razy}.values()), user)}
    return {
        "items": [
            {
                "srok": schemas._iso(r["srok"]),
                "sdelan": r["sdelan"],
                "budushchiy": r.get("budushchiy", False),
                "task": stroki[r["task"].id],
            }
            for r in razy
        ]
    }


@router.get("/signals")
def my_signals(user: User = Depends(require_perm("tasks", "view")), db: Session = Depends(get_db)):
    """Звонки, которых человек ещё не видел, — их показывает вкладка со звуком."""
    zvonki = task_service.zvonki_moi(db, user)
    zadachi = tasks_repo.po_nomeram(db, [z.task_id for z in zvonki])
    return {
        "items": [
            {
                "id": z.id,
                "task_id": z.task_id,
                "title": zadachi[z.task_id].title,
                "vazhnost": zadachi[z.task_id].vazhnost,
                "vid": z.vid,
                "srok": schemas._iso(z.srok),
                "moment": schemas._iso(z.moment),
            }
            for z in zvonki
            if z.task_id in zadachi
        ]
    }


@router.post("/signals/ack")
def ack_signals(
    payload: ZvonkiIn,
    user: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    return {"acknowledged": task_service.zvonki_prinyat(db, user, payload.ids)}


@router.get("/{task_id}")
def get_task(
    task_id: int,
    user: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    """Карточка: то же, что в списке, плюс подробности, вложения, шаги, ссылки и история."""
    task = task_service.dostupnoe(db, user, task_id)
    vlozheniya = task_service.files(db, task.id)
    istoriya = tasks_repo.istoriya(db, task.id)
    imena = {u.id: u.name for u in users_repo.get_many(db, {e.user_id for e in istoriya if e.user_id})}
    data = _out(db, [task], user)[0]
    data["note"] = task.note
    data["files"] = [schemas.task_file_out(f) for f in vlozheniya]
    data["files_count"] = len(vlozheniya)
    data["shagi_spisok"] = [
        {"id": s.id, "text": s.text, "sdelan": s.sdelan_at is not None} for s in tasks_repo.shagi(db, task.id)
    ]
    data["ssylki"] = [{"id": s.id, "url": s.url, "title": s.title} for s in tasks_repo.ssylki(db, task.id)]
    data["istoriya"] = [
        {
            "vid": e.vid, "kto": imena.get(e.user_id), "srok": schemas._iso(e.srok),
            "created_at": schemas._iso(e.created_at),
        }
        for e in istoriya
    ]
    return data


@router.post("", status_code=201)
def create_task(
    payload: TaskIn,
    user: User = Depends(require_perm("tasks", "create")),
    db: Session = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    if data.pop("kazhdomu", False):
        return {"items": _out(db, task_service.create_kazhdomu(db, data, user), user)}
    task = task_service.create(db, data, user)
    return _out(db, [task], user)[0]


@router.patch("/{task_id}")
def update_task(
    task_id: int,
    payload: TaskPatchIn,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    task = _pravit(db, user, task_id)
    if {"poluchateli", "nablyudateli", "assignee_id"} & data.keys():
        _lyudi_menyat(db, user, task)
    task = task_service.update(db, task.id, data, user)
    return _out(db, [task], user)[0]


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    user: User = Depends(require_perm("tasks", "delete")),
    db: Session = Depends(get_db),
):
    task = _pravit(db, user, task_id)
    _lyudi_menyat(db, user, task)
    task_service.delete(db, task.id, user)
    return {"message": "Task deleted"}


@router.post("/{task_id}/done")
def done(task_id: int, user: User = Depends(require_perm("tasks", "edit")), db: Session = Depends(get_db)):
    task = _pravit(db, user, task_id)
    if task.done_at is not None:
        raise errors.ConflictError("Already done", code="task_uzhe_sdelano")
    task_service.zakryt(db, task, user)
    return _out(db, [task], user)[0]


@router.post("/{task_id}/reopen")
def reopen(task_id: int, user: User = Depends(require_perm("tasks", "edit")), db: Session = Depends(get_db)):
    task = task_service.otkryt(db, _pravit(db, user, task_id), user)
    return _out(db, [task], user)[0]


@router.post("/{task_id}/skip")
def skip(task_id: int, user: User = Depends(require_perm("tasks", "edit")), db: Session = Depends(get_db)):
    task = task_service.propustit(db, _pravit(db, user, task_id), user)
    return _out(db, [task], user)[0]


@router.post("/{task_id}/snooze")
def snooze(
    task_id: int,
    payload: OtlozhitIn,
    user: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    """Отложить — своё: право на правку не нужно, звонок-то звонит мне."""
    task = task_service.otlozhit(db, _pravit(db, user, task_id), user, payload.do)
    return _out(db, [task], user)[0]


@router.post("/{task_id}/take")
def take(task_id: int, user: User = Depends(require_perm("tasks", "edit")), db: Session = Depends(get_db)):
    """Взять с общей полки."""
    task = _pravit(db, user, task_id)
    task_service.vzyat_s_polki(db, task, user)
    return _out(db, [task], user)[0]


@router.post("/{task_id}/steps", status_code=201)
def add_step(
    task_id: int,
    payload: ShagIn,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    shag = task_service.shag_dobavit(db, _pravit(db, user, task_id), user, payload.text)
    return {"id": shag.id, "text": shag.text, "sdelan": False}


@router.patch("/{task_id}/steps/{step_id}")
def edit_step(
    task_id: int,
    step_id: int,
    payload: ShagPatchIn,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    shag = task_service.shag_pravit(
        db, _pravit(db, user, task_id), step_id, payload.model_dump(exclude_unset=True)
    )
    return {"id": shag.id, "text": shag.text, "sdelan": shag.sdelan_at is not None}


@router.delete("/{task_id}/steps/{step_id}")
def delete_step(
    task_id: int,
    step_id: int,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    task_service.shag_ubrat(db, _pravit(db, user, task_id), step_id)
    return {"message": "Step deleted"}


@router.post("/{task_id}/links", status_code=201)
def add_link(
    task_id: int,
    payload: SsylkaIn,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    ssylka = task_service.ssylka_dobavit(db, _pravit(db, user, task_id), user, payload.url, payload.title)
    return {"id": ssylka.id, "url": ssylka.url, "title": ssylka.title}


@router.delete("/{task_id}/links/{url_id}")
def delete_link(
    task_id: int,
    url_id: int,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    task_service.ssylka_ubrat(db, _pravit(db, user, task_id), url_id)
    return {"message": "Link deleted"}


@router.post("/{task_id}/files", status_code=201)
async def upload_file(
    task_id: int,
    file: UploadFile,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    _pravit(db, user, task_id)
    content = await file.read()
    record = task_service.add_file(db, task_id, user, file.filename or "file", content)
    return schemas.task_file_out(record)


@router.get("/{task_id}/files/{file_id}/download")
def download_file(
    task_id: int,
    file_id: int,
    user: User = Depends(require_perm("tasks", "view")),
    db: Session = Depends(get_db),
):
    _pravit(db, user, task_id)
    record = task_service.get_file(db, task_id, file_id)
    path = task_service.file_path_on_disk(record)
    if not path.exists():
        raise errors.NotFoundError("File is missing on disk", code="file_missing")
    # Картинкой, а не вложением: её смотрят прямо в карточке. Тип — из
    # расширения, уже сверенного с содержимым при приёме.
    return FileResponse(
        path,
        media_type=task_service.mime_dlya_otdachi(record),
        filename=record.original_name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{task_id}/files/{file_id}")
def delete_file(
    task_id: int,
    file_id: int,
    user: User = Depends(require_perm("tasks", "edit")),
    db: Session = Depends(get_db),
):
    _pravit(db, user, task_id)
    task_service.delete_file(db, task_id, file_id, user)
    return {"message": "File deleted"}
