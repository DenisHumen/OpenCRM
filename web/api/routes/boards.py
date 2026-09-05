from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import board_service, media_service, share_service
from database.models import User
from database.repositories import clients as clients_repo
from database.repositories import boards as boards_repo
from database.repositories import shares as shares_repo
from database.repositories import users as users_repo
from web.api import cards, schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm
from web.public import layout

router = APIRouter(
    prefix="/boards", tags=["boards"], dependencies=[Depends(require_module("boards"))]
)


def _avtor(db: Session, board) -> str | None:
    """Имя того, кто завёл доску; у досок до колонки `created_by` — пусто."""
    if not board.created_by:
        return None
    avtor = users_repo.get_by_id(db, board.created_by)
    return avtor.name if avtor else None


def _imya(db: Session, board) -> str | None:
    """Имя клиента доски. Одно и то же в ответе на чтение и на правку.

    Прежде редактор брал его из справочника клиентов, загруженного в поле
    выбора первыми двумя сотнями: у клиента за этим пределом подпись доски
    показывала многоточие вместо имени.
    """
    if not board.client_id:
        return None
    klient = clients_repo.get(db, board.client_id)
    return klient.name if klient else None


@router.get("")
def list_boards(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    client_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_perm("boards", "view")),
    db: Session = Depends(get_db),
):
    boards, total = boards_repo.search(db, q=search, client_id=client_id, page=page, per_page=per_page)
    items = cards.board_cards(db, boards, with_client=True)
    return schemas.paginated(items, total, page, per_page)


@router.post("", status_code=201)
def create_board(
    payload: schemas.BoardIn,
    user: User = Depends(require_perm("boards", "create")),
    db: Session = Depends(get_db),
):
    board = board_service.create_board(
        db, user, payload.title, payload.description or "", payload.client_id, payload.deal_id
    )
    return schemas.board_out(board, works_count=0, created_by_name=user.name)


@router.get("/{board_id}")
def get_board(
    board_id: int,
    _: User = Depends(require_perm("boards", "view")),
    db: Session = Depends(get_db),
):
    board = board_service.get_board(db, board_id)
    works = boards_repo.list_works(db, board_id)
    links = shares_repo.list_for_board(db, board_id)
    data = schemas.board_out(
        board, works_count=len(works), client_name=_imya(db, board), created_by_name=_avtor(db, board)
    )
    data["works"] = [schemas.work_out(w) for w in works]
    # форма места каждой работы на витрине — редактору обрезки, чтобы рамка
    # фрагмента совпадала с тем, что увидит клиент. Композицию собирают только
    # готовые работы, поэтому необработанные места пока не занимают.
    #
    # Здесь же — ответ на вопрос «обрезана ли работа». Правило зависит от
    # композиции, а её знает только сервер: редактор не повторяет условие, а
    # читает готовый ответ (`layout.is_cropped`). Тем же правилом рисуется
    # витрина, поэтому карточка в CRM и плитка у клиента не разойдутся.
    ready = [w for w in data["works"] if w["status"] == "ready"]
    for work, ratio in zip(ready, layout.place_ratios(len(ready))):
        work["place_ratio"] = ratio
        work["is_cropped"] = layout.is_cropped(
            work["kind"], work["width"], work["height"], ratio
        )
    data["shares"] = [
        schemas.share_out(link, share_service.share_stats(db, link)) for link in links
    ]
    return data


@router.patch("/{board_id}")
def update_board(
    board_id: int,
    payload: schemas.BoardPatchIn,
    _: User = Depends(require_perm("boards", "edit")),
    db: Session = Depends(get_db),
):
    board = board_service.update_board(db, board_id, payload.model_dump(exclude_unset=True))
    return schemas.board_out(
        board,
        works_count=boards_repo.count_works(db, board.id),
        client_name=_imya(db, board),
        created_by_name=_avtor(db, board),
    )


@router.delete("/{board_id}")
def delete_board(
    board_id: int,
    actor: User = Depends(require_perm("boards", "delete")),
    db: Session = Depends(get_db),
):
    board_service.delete_board(db, board_id, actor)
    return {"message": "Board deleted"}


# --- работы ---

@router.post("/{board_id}/works", status_code=202)
async def upload_work(
    board_id: int,
    file: UploadFile,
    background: BackgroundTasks,
    _: User = Depends(require_perm("boards", "create")),
    db: Session = Depends(get_db),
):
    content = await file.read()
    work = board_service.upload_work(db, board_id, file.filename or "file", content)
    # коммитим до планирования фоновой задачи: она работает в своей сессии
    db.commit()
    background.add_task(board_service.process_work, work.id)
    return schemas.work_out(work)


@router.get("/{board_id}/works/{work_id}")
def get_work(
    board_id: int,
    work_id: int,
    _: User = Depends(require_perm("boards", "view")),
    db: Session = Depends(get_db),
):
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    return schemas.work_out(work)


@router.get("/{board_id}/works/{work_id}/download")
def download_work(
    board_id: int,
    work_id: int,
    _: User = Depends(require_perm("boards", "view")),
    db: Session = Depends(get_db),
):
    """Отдать сотруднику ИСХОДНИК работы — тот файл, который загрузили.

    Правом на просмотр, и это решение, а не недосмотр: разбор — в шапке раздела
    «выгрузка исходников» в `core/services/board_service.py`. Коротко: тот, кому
    доска открыта, уже видит работу в полном размере, и «скачать» здесь означает
    «взять то же самое файлом», а не новое полномочие.

    Клиенту витрины эта ручка недоступна: у него нет сессии CRM, а публичная
    часть исходников не отдаёт вовсе (`media_service.PUBLIC_FILENAMES`).
    """
    put, work = board_service.fayl_raboty(db, board_id, work_id)
    return FileResponse(
        put,
        filename=board_service.imya_dlya_sohraneniya(work, put),
        media_type=work.mime or "application/octet-stream",
        # `attachment` у всего без исключений — в отличие от вложений переписки,
        # где фотографию показывают по этой же ссылке. Здесь показывать нечем и
        # незачем: картинку рисует витрина, а сюда приходят именно за файлом. И
        # это разом снимает вопрос с SVG: скрипт внутри него (санитайзинг —
        # второй рубеж, а не первый) не выполнится, потому что документ не
        # откроется вовсе.
        content_disposition_type="attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            # Не кэшировать: право могут отобрать, доску — удалить, а исходник
            # из кэша браузера пережил бы и то, и другое.
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{board_id}/download")
def download_board(
    board_id: int,
    _: User = Depends(require_perm("boards", "view")),
    db: Session = Depends(get_db),
):
    """Все исходники доски одним архивом.

    **Список файлов собирается ЗДЕСЬ, а отдача идёт потоком уже без базы.**
    Иначе никак: сессия закрывается до того, как уйдёт тело ответа
    (`web/api/deps.py`, `scope="function"`), и генератор, полезший бы в неё за
    следующей работой, обратился бы к закрытой сессии. Заодно это ставит все
    отказы — нет доски, нет файлов — на своё место: до первого байта, пока их
    ещё можно превратить в честный ответ, а не в оборванную загрузку.
    """
    imya, fayly = board_service.fayly_doski(db, board_id)
    return StreamingResponse(
        media_service.potok_zip(fayly),
        media_type="application/zip",
        headers={
            # Имя собираем руками: у `StreamingResponse`, в отличие от
            # `FileResponse`, поля `filename` нет. Две записи в заголовке — не
            # избыточность: `filename*` несёт настоящее имя (название доски
            # бывает и русским), а простой `filename` остаётся тому, кто
            # RFC 5987 не понимает, и он обязан быть из одних ASCII-знаков.
            "Content-Disposition": (
                f'attachment; filename="board-{board_id}.zip"; '
                f"filename*=utf-8''{quote(imya)}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.patch("/{board_id}/works/{work_id}")
def update_work(
    board_id: int,
    work_id: int,
    payload: schemas.WorkPatchIn,
    _: User = Depends(require_perm("boards", "edit")),
    db: Session = Depends(get_db),
):
    work = board_service.update_work(db, board_id, work_id, payload.model_dump(exclude_unset=True))
    return schemas.work_out(work)


@router.put("/{board_id}/works/order")
def reorder_works(
    board_id: int,
    payload: schemas.WorkOrderIn,
    _: User = Depends(require_perm("boards", "edit")),
    db: Session = Depends(get_db),
):
    works = board_service.reorder_works(db, board_id, payload.work_ids)
    return {"items": [schemas.work_out(w) for w in works]}


@router.delete("/{board_id}/works/{work_id}")
def delete_work(
    board_id: int,
    work_id: int,
    _: User = Depends(require_perm("boards", "delete")),
    db: Session = Depends(get_db),
):
    board_service.delete_work(db, board_id, work_id)
    return {"message": "Work deleted"}


# --- публичные ссылки доски ---

@router.get("/{board_id}/shares")
def list_shares(
    board_id: int,
    _: User = Depends(require_perm("boards", "view")),
    db: Session = Depends(get_db),
):
    board_service.get_board(db, board_id)
    links = shares_repo.list_for_board(db, board_id)
    return {
        "items": [schemas.share_out(l, share_service.share_stats(db, l)) for l in links]
    }


@router.post("/{board_id}/shares", status_code=201)
def create_share(
    board_id: int,
    payload: schemas.ShareIn,
    user: User = Depends(require_perm("boards", "edit")),
    db: Session = Depends(get_db),
):
    board = board_service.get_board(db, board_id)
    link = share_service.create_share(
        db, board, user, expires_at=payload.expires_at, pin=payload.pin
    )
    return schemas.share_out(link, {"views_count": 0, "unique_views_count": 0, "last_viewed_at": None})
