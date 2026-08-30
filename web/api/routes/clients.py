from datetime import date

from fastapi import APIRouter, Depends, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import client_service, permissions_service, settings_service
from database.models import User
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_perm


def _note_authors(db, notes) -> dict[int, str]:
    """Имена авторов записей ленты — одним запросом на список.

    Пачкой, а не по строке: лента открывается на каждой карточке, и запрос на
    запись превратил бы её в самое дорогое место экрана.
    """
    people = users_repo.get_many(db, [n.author_id for n in notes])
    return {person.id: person.name for person in people}


router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("")
def list_clients(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    tag: str | None = None,
    manager_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_perm("clients", "view")),
    db: Session = Depends(get_db),
):
    items, total = clients_repo.search(
        db, q=search, tag=tag, manager_id=manager_id, page=page, per_page=per_page
    )
    return schemas.paginated([schemas.client_out(c) for c in items], total, page, per_page)


@router.get("/export.csv")
def export_clients(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    tag: str | None = None,
    manager_id: int | None = None,
    _: User = Depends(require_perm("clients", "view")),
    db: Session = Depends(get_db),
):
    """Список клиентов файлом. Отбор — тот же, что у списка на экране.

    Право то же, что на просмотр, и это не послабление: выгрузка отдаёт ровно
    те карточки, которые человек и так видит. Заведи ей своё право — и
    получилось бы, что видеть можно, а сохранить нельзя; обойти это удалось бы
    выделением мышью.

    Отдельным адресом с расширением, а не флагом `?format=csv` у списка: у
    списка ответ страничный и в JSON, здесь — файл целиком. Один маршрут на
    два разных ответа заставил бы обе половины объяснять, какие поля она
    сегодня читает, а браузер — гадать, скачивать или показывать.
    """
    content = client_service.vygruzka_csv(
        db, q=search, tag=tag, manager_id=manager_id
    )
    # Имя с датой: в папке «Загрузки» через месяц лежит пять выгрузок, и
    # «clients.csv (3)» не отвечает, какая из них свежая.
    imya = f"clients-{date.today().isoformat()}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{imya}"',
            # Список клиентов меняется каждый день; кэш браузера отдал бы
            # вчерашний файл по той же ссылке.
            "Cache-Control": "no-store",
        },
    )


@router.post("", status_code=201)
def create_client(
    payload: schemas.ClientIn,
    user: User = Depends(require_perm("clients", "create")),
    db: Session = Depends(get_db),
):
    client = client_service.create_client(db, payload.model_dump(), user)
    return schemas.client_out(client)


@router.get("/{client_id}")
def get_client(
    client_id: int,
    user: User = Depends(require_perm("clients", "view")),
    db: Session = Depends(get_db),
):
    client = client_service.get_client(db, client_id)
    notes, _total = clients_repo.list_notes(db, client_id, page=1, per_page=10)
    files = clients_repo.list_files(db, client_id)
    data = schemas.client_out(client)
    note_authors = _note_authors(db, notes)
    data["recent_notes"] = [
        schemas.note_out(n, note_authors.get(n.author_id)) for n in notes
    ]
    data["files"] = [schemas.file_out(f) for f in files]
    # Заявки клиента прямо в карточке: без них «что мы для него делали» —
    # вопрос к памяти, а не к системе. Врезка подчиняется тем же правилам, что
    # и раздел заявок: чужие сюда не попадают, суммы прячутся вместе с ними.
    # Иначе ограничение обходилось бы через карточку клиента.
    amounts = permissions_service.sees_amounts(db, user)
    data["deals"] = [
        schemas.deal_out(d, amounts=amounts)
        for d in deals_repo.for_client(
            db, client_id, only_manager_id=permissions_service.deals_scope(db, user)
        )
    ]
    data["currency"] = settings_service.get_all(db).get("currency", "USD")
    return data


@router.patch("/{client_id}")
def update_client(
    client_id: int,
    payload: schemas.ClientPatchIn,
    _: User = Depends(require_perm("clients", "edit")),
    db: Session = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    return schemas.client_out(client_service.update_client(db, client_id, data))


@router.delete("/{client_id}")
def delete_client(
    client_id: int,
    user: User = Depends(require_perm("clients", "delete")),
    db: Session = Depends(get_db),
):
    client_service.delete_client(db, client_id, user)
    return {"message": "Client deleted"}


@router.post("/{client_id}/restore")
def restore_client(
    client_id: int,
    actor: User = Depends(require_perm("clients", "restore")),
    db: Session = Depends(get_db),
):
    return schemas.client_out(client_service.restore_client(db, client_id, actor))


# --- заметки ---

@router.get("/{client_id}/notes")
def list_notes(
    client_id: int,
    kind: str | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_perm("clients", "view")),
    db: Session = Depends(get_db),
):
    client_service.get_client(db, client_id)
    notes, total = clients_repo.list_notes(
        db, client_id, page=page, per_page=per_page, kind=kind, deal_id=deal_id
    )
    authors = _note_authors(db, notes)
    return schemas.paginated(
        [schemas.note_out(n, authors.get(n.author_id)) for n in notes], total, page, per_page
    )


@router.post("/{client_id}/notes", status_code=201)
def add_note(
    client_id: int,
    payload: schemas.NoteIn,
    user: User = Depends(require_perm("clients", "edit")),
    db: Session = Depends(get_db),
):
    note = client_service.add_note(
        db,
        client_id,
        user,
        payload.kind,
        payload.body,
        payload.happened_at,
        direction=payload.direction,
        deal_id=payload.deal_id,
    )
    return schemas.note_out(note, note.author_id and _note_authors(db, [note]).get(note.author_id))


@router.delete("/{client_id}/notes/{note_id}")
def delete_note(
    client_id: int,
    note_id: int,
    user: User = Depends(require_perm("clients", "edit")),
    db: Session = Depends(get_db),
):
    client_service.delete_note(db, client_id, note_id, user)
    return {"message": "Note deleted"}


# --- файлы ---

@router.get("/{client_id}/files")
def list_files(
    client_id: int,
    _: User = Depends(require_perm("clients", "view")),
    db: Session = Depends(get_db),
):
    client_service.get_client(db, client_id)
    return {"items": [schemas.file_out(f) for f in clients_repo.list_files(db, client_id)]}


@router.post("/{client_id}/files", status_code=201)
async def upload_file(
    client_id: int,
    file: UploadFile,
    user: User = Depends(require_perm("clients", "edit")),
    db: Session = Depends(get_db),
):
    content = await file.read()
    record = client_service.add_file(
        db, client_id, user, file.filename or "file", content, file.content_type or ""
    )
    return schemas.file_out(record)


@router.get("/{client_id}/files/{file_id}/download")
def download_file(
    client_id: int,
    file_id: int,
    _: User = Depends(require_perm("clients", "view")),
    db: Session = Depends(get_db),
):
    record = client_service.get_file(db, client_id, file_id)
    path = client_service.file_path_on_disk(record)
    if not path.exists():
        raise errors.NotFoundError("File is missing on disk", code="file_missing")
    return FileResponse(
        path,
        # Тип считает служба из имени, а не берёт из записи: у файлов,
        # залитых до этой правки, там лежит присланный при загрузке заголовок.
        media_type=client_service.mime_dlya_otdachi(record),
        filename=record.original_name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{client_id}/files/{file_id}")
def delete_file(
    client_id: int,
    file_id: int,
    user: User = Depends(require_perm("clients", "edit")),
    db: Session = Depends(get_db),
):
    client_service.delete_file(db, client_id, file_id, user)
    return {"message": "File deleted"}
