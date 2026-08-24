import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import references
from core.services import audit_service, media_service, storage_service
from core.utils import normalize_external_url, now_utc
from database.models import Board, User, Work
from database.models.board import WORK_FAILED, WORK_PROCESSING, WORK_READY
from database.repositories import boards as boards_repo
from database.session import get_session


def get_board(db: Session, board_id: int) -> Board:
    board = boards_repo.get(db, board_id)
    if board is None:
        raise errors.NotFoundError("Board not found", code="board_not_found")
    return board


def create_board(
    db: Session,
    author: User,
    title: str,
    description: str = "",
    client_id: int | None = None,
    deal_id: int | None = None,
) -> Board:
    if not title.strip():
        raise errors.ValidationError("Title is required", code="title_required")
    board = Board(
        title=title.strip(),
        description=(description or "").strip(),
        client_id=references.client(db, client_id),
        deal_id=references.deal(db, deal_id),
        created_by=author.id,
        # **Новая доска сразу опубликована, и переключатель это показывает.**
        #
        # Доску заводят, чтобы её показать: другого повода нет. Прежний порядок
        # («создали выключенной, потом включите») давал лишний шаг ровно там,
        # где он никому не нужен, и молча приводил к тому, что ссылку отправляли
        # с непубликованной доски — посетитель видел отказ, а автор был уверен,
        # что всё отдал.
        #
        # Обратное действие при этом не потеряно: переключатель на месте, и
        # нажатие снимает публикацию. Спрятать доску — осознанное решение, и
        # именно оно должно требовать движения, а не показ.
        is_published=True,
    )
    db.add(board)
    db.flush()
    return board


def update_board(db: Session, board_id: int, data: dict) -> Board:
    board = get_board(db, board_id)
    if "title" in data and data["title"] is not None:
        if not data["title"].strip():
            raise errors.ValidationError("Title is required", code="title_required")
        board.title = data["title"].strip()
    if "description" in data and data["description"] is not None:
        board.description = data["description"].strip()
    if "client_id" in data:
        board.client_id = data["client_id"]
    # Привязку к заявке можно и снять: доска переехала или создавалась не под неё.
    if "deal_id" in data:
        board.deal_id = data["deal_id"]
    if "is_published" in data and data["is_published"] is not None:
        board.is_published = bool(data["is_published"])
    if "cover_work_id" in data:
        cover_id = data["cover_work_id"]
        if cover_id is not None:
            work = boards_repo.get_work(db, board.id, cover_id)
            if work is None:
                raise errors.ValidationError("Cover work must belong to the board", code="bad_cover")
        board.cover_work_id = cover_id
    board.updated_at = now_utc()
    return board


def delete_board(db: Session, board_id: int, actor: User) -> None:
    """Удаляет доску вместе с файлами работ.

    Раньше доска только помечалась удалённой (deleted_at), а медиа висело на
    диске до чистки корзины. Восстановления досок в UI нет, поэтому мягкое
    удаление лишь занимало место — теперь удаляем сразу и запись, и файлы.
    Записи works / share_links / share_views уходят каскадом (ondelete=CASCADE).
    """
    board = get_board(db, board_id)
    work_uids = [w.work_uid for w in boards_repo.list_works(db, board_id)]
    # Снимок названия до удаления: спросить его потом будет не у кого, а
    # «доска 17 удалена» не отвечает на вопрос «какая».
    label, board_id_before = board.title, board.id
    db.delete(board)
    db.flush()
    # В журнал: удаление доски снимает у клиента и саму витрину, и файлы с
    # диска — навсегда. Это единственное здесь удаление без корзины, и вопрос
    # «куда делись работы по тому заказу» задают именно про него.
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_BOARD,
        entity_id=board_id_before,
        entity_label=label,
    )
    for uid in work_uids:
        media_service.delete_work_files(uid)
    storage_service.invalidate_size_cache()


def cover_work(db: Session, board: Board) -> Work | None:
    """Обложка доски: назначенная, иначе первая готовая работа."""
    if board.cover_work_id:
        work = boards_repo.get_work(db, board.id, board.cover_work_id)
        if work is not None and work.status == WORK_READY:
            return work
    ready = boards_repo.list_works(db, board.id, only_ready=True)
    return ready[0] if ready else None


# --- работы ---

def title_from_filename(original_name: str) -> str:
    """Название работы из имени файла: без расширения и без служебных знаков.

    Раньше название оставалось пустым, а на витрине подпись рисуется только при
    заполненном `title` — то есть свежезагруженная работа была безымянной, пока
    менеджер не переименует её руками. Имя файла почти всегда осмысленно, так
    что берём его: расширение убираем (`.jpg` в подписи под работой не нужен),
    подчёркивания и точки-разделители заменяем пробелами.
    """
    stem = Path(original_name).stem.strip()
    cleaned = re.sub(r"[_.]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200]


def upload_work(db: Session, board_id: int, original_name: str, content: bytes) -> Work:
    board = get_board(db, board_id)
    if not content:
        raise errors.ValidationError("File is empty", code="file_empty")
    if len(content) > get_settings().max_upload_bytes:
        raise errors.ValidationError("File is too large", code="file_too_large")
    # превью занимают ещё примерно столько же, поэтому запрашиваем двойной объём
    if not storage_service.has_room_for(len(content) * 2):
        raise errors.ValidationError(
            "Not enough free disk space on the server", code="disk_full"
        )
    detected_kind, ext, mime = media_service.detect_media(content[:512], original_name)
    kind = "image" if detected_kind == "svg" else detected_kind
    if detected_kind == "image":
        # Отказ ЗДЕСЬ, в запросе, а не в фоновой обработке. Превью строятся
        # фоновой задачей, и отказ там пометил бы работу `failed` без единого
        # слова о причине — поля для причины у модели нет, и человек повторял бы
        # тот же файл. Проверка при этом ничего не стоит: размеры читаются из
        # заголовка, разжатия не происходит.
        media_service.assert_decodable(content)

    work = Work(
        board_id=board.id,
        work_uid=uuid.uuid4().hex,
        kind=kind,
        title=title_from_filename(original_name),
        status=WORK_PROCESSING,
        original_name=original_name[:255],
        mime=mime,
        size_bytes=len(content),
        sort_order=boards_repo.next_sort_order(db, board.id),
    )
    db.add(work)
    db.flush()
    media_service.save_original(work.work_uid, detected_kind, ext, content)
    board.updated_at = now_utc()
    return work


def process_work(work_id: int) -> None:
    """Фоновая задача: генерирует превью. Открывает собственную сессию БД."""
    db = get_session()
    try:
        work = boards_repo.get_work_by_id(db, work_id)
        if work is None:
            return
        try:
            # Имя исходника собирает `media_service`, а не этот код: класть файл
            # и искать его — одно правило, и жить оно обязано в одном месте.
            original = media_service.original_path(work.work_uid, work.kind, work.mime)
            if work.mime == "image/svg+xml":
                meta = {}  # SVG отдаётся как есть, превью не нужны
            elif work.kind == "image":
                meta = media_service.process_image(work.work_uid, original)
            else:
                meta = media_service.process_video(work.work_uid, original)
            for field in ("width", "height", "duration_sec", "blurhash"):
                if meta.get(field) is not None:
                    setattr(work, field, meta[field])
            work.status = WORK_READY
        except Exception:
            work.status = WORK_FAILED
            raise
        finally:
            db.commit()
    finally:
        db.close()


def update_work(db: Session, board_id: int, work_id: int, data: dict) -> Work:
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    if "title" in data and data["title"] is not None:
        work.title = data["title"].strip()[:200]
    if "description" in data and data["description"] is not None:
        work.description = data["description"].strip()
    if "project_url" in data and data["project_url"] is not None:
        try:
            work.project_url = normalize_external_url(data["project_url"])
        except ValueError as exc:
            raise errors.ValidationError(str(exc), code="bad_project_url") from exc
    _apply_preview_crop(work, data)
    return work


def _apply_preview_crop(work: Work, data: dict) -> None:
    """Выбранный менеджером фрагмент работы: 0 — верх картинки, 1 — низ.

    `null` возвращает работу к показу от верха.

    Обрезана работа или нет — решает её место в композиции витрины
    (`web/public/layout.py`, `is_cropped`), и раньше здесь стоял порог
    вытянутости: короткая картинка «помещается в место целиком». Это неправда —
    мест в композиции семь, и формы у них разные, — и именно на этом отказе
    ломалась жалоба «работа обрезана, а поправить нечем».

    По месту служба всё же не судит, и намеренно. Место работы зависит от
    соседей: добавили работу, переставили, удалили — и та же работа переехала в
    место другой формы. Судить по нему значило бы, что один и тот же PATCH то
    проходит, то нет, а сохранённое вчера значение назавтра оказывается
    «недопустимым». Служба хранит выбор, композиция решает, применять ли его.

    Отказ остаётся там, где фрагмент не из чего выбирать в принципе: у видео в
    плитке показан постер, а без сторон работы не построить ни окна обрезки, ни
    самого суждения об обрезке.
    """
    if "preview_focus" not in data:
        return
    if work.kind != "image" or not work.width or not work.height:
        raise errors.ValidationError(
            "Preview crop applies to images with known dimensions",
            code="not_a_croppable_work",
        )
    focus = data["preview_focus"]
    work.preview_focus = None if focus is None else round(max(0.0, min(1.0, float(focus))), 4)


def delete_work(db: Session, board_id: int, work_id: int) -> None:
    board = get_board(db, board_id)
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    if board.cover_work_id == work.id:
        board.cover_work_id = None
    uid = work.work_uid
    db.delete(work)
    db.flush()
    media_service.delete_work_files(uid)


# --- выгрузка исходников -----------------------------------------------------
#
# Зачем это вообще нужно. Наружу уходят только производные: WebP по длинной
# стороне, постер видео, размытый след. Исходник закрыт списком разрешённых имён
# (`media_service.PUBLIC_FILENAMES`) и не отдаётся НИКОМУ — ни клиенту по
# ссылке, ни сотруднику. То есть файл, который менеджер сам же сюда и загрузил,
# забрать обратно было неоткуда: за ним шли на сервер по ssh или искали письмо
# полугодовой давности.
#
# Клиенту витрины этого не дают и не дадут: там доска — витрина, а не выдача
# файлов, и «посмотреть» с «забрать себе исходник в печатном качестве» —
# разные вещи. Поэтому ручки живут в API CRM, за сессией и правом, а публичная
# часть (`web/public/routes.py`) не меняется вовсе.
#
# **Скачать — то же действие, что посмотреть, поэтому обе ручки закрыты
# `boards.view`.** Отдельного права заводить не за что: тот, кому доска открыта,
# уже видит работу в полном размере (`large`, 1920px) и сохраняет её из браузера
# в два щелчка. Право, которое ничего не запрещает, хуже его отсутствия — тот же
# довод, по которому у наклеек печать не отделена от просмотра
# (`core/permissions.py`).

#: Знаки, которым в имени файла делать нечего: разделители пути, служебные для
#: Windows и управляющие. Имя приходит от постороннего (его выбрал тот, кто
#: прислал файл), едет в заголовок ответа и оттуда — прямо в чужую файловую
#: систему.
_PLOHIE_V_IMENI = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def _chistoe_imya(tekst: str) -> str:
    """Строка, годная в имя файла: без разделителей, без хвостовых точек."""
    return re.sub(r"\s+", " ", _PLOHIE_V_IMENI.sub(" ", tekst or "")).strip(" .")


def imya_dlya_sohraneniya(work: Work, put: Path) -> str:
    """Имя, под которым работа ляжет в «Загрузки».

    Берём то, под которым файл загрузили: менеджер узнаёт свою работу именно по
    нему, а на диске она лежит под опознавателем каталога и словом `original` —
    такое имя в «Загрузках» не говорит ни о чём.

    **Расширение — от настоящего файла, а не из прежнего имени.** Вид мы
    определяем по сигнатуре и расширению не верим: присланный «logo.png», внутри
    которого JPEG, лежит у нас как `original.jpg`. Вернуть его под прежним именем
    значило бы отдать человеку файл, который не откроется двойным щелчком.
    """
    osnova = _chistoe_imya(Path(work.original_name or "").stem) or _chistoe_imya(work.title)
    return (osnova[:120] or f"work-{work.id}") + put.suffix


def _put_originala(work: Work) -> Path | None:
    """Проверенный путь к исходнику работы — или `None`, если файла нет.

    Путь сверяется с каталогом медиа целиком, как в `media_service.public_file`.
    Опознаватель каталога сочиняем мы сами (`uuid4().hex`), увести он никуда не
    может — но проверка стоит там, где файл отдают наружу, а не там, где путь
    выглядит подозрительным. Ровно этой проверки не хватало по всей отрасли в
    целом классе таких мест.
    """
    koren = get_settings().media_dir.resolve()
    put = media_service.original_path(work.work_uid, work.kind, work.mime).resolve()
    if not put.is_relative_to(koren) or not put.is_file():
        return None
    return put


def fayl_raboty(db: Session, board_id: int, work_id: int) -> tuple[Path, Work]:
    """Исходник одной работы: путь на диске и сама работа.

    **Работа обязана принадлежать НАЗВАННОЙ доске.** Проверяет это
    `boards_repo.get_work`, и без неё подставленный в адрес чужой номер отдавал
    бы файл соседней доски — то есть чужого клиента. Доска при этом спрашивается
    отдельно (`get_board`): у доски в корзине работы на диске ещё лежат, а
    открывать её больше нельзя.

    Нет файла — «нет такого». Не «есть, но не отдадим»: разные ответы на эти два
    случая рассказали бы постороннему, какие номера работ существуют.
    """
    get_board(db, board_id)
    work = boards_repo.get_work(db, board_id, work_id)
    if work is None:
        raise errors.NotFoundError("Work not found", code="work_not_found")
    put = _put_originala(work)
    if put is None:
        raise errors.NotFoundError("File is missing on disk", code="work_file_missing")
    return put, work


def fayly_doski(db: Session, board_id: int) -> tuple[str, list[tuple[Path, str]]]:
    """Все исходники доски: имя архива и пары «файл на диске — имя внутри».

    Берутся ВСЕ работы, а не только готовые. Исходник ложится на диск при
    загрузке, а `ready` означает лишь, что построились превью; работа, у которой
    обработка отвалилась, — как раз та единственная, которую иначе не забрать
    вовсе (на витрину она не попадает, картинки в карточке у неё нет).

    Пропавший файл пропускаем молча: доска из тридцати работ не должна
    переставать выгружаться из-за одной, потерянной когда-то на диске.
    """
    board = get_board(db, board_id)
    raboty = boards_repo.list_works(db, board_id)
    # Ширина номера — по числу работ: при `02d` и сотне работ распаковщик
    # разложил бы «100» перед «11», то есть номера перестали бы значить порядок.
    shirina = max(2, len(str(len(raboty))))
    fayly: list[tuple[Path, str]] = []
    for nomer, work in enumerate(raboty, start=1):
        put = _put_originala(work)
        if put is None:
            continue
        # Номер впереди делает два дела разом. Держит ПОРЯДОК доски — тот, в
        # котором собрана композиция витрины (распаковщик раскладывает файлы по
        # алфавиту, а он у имён свой). И разводит совпадающие имена: две работы,
        # загруженные как `logo.png`, иначе столкнулись бы в одном архиве.
        fayly.append((put, f"{nomer:0{shirina}d} {imya_dlya_sohraneniya(work, put)}"))
    if not fayly:
        raise errors.ValidationError(
            "This board has no files to download", code="board_has_no_files"
        )
    imya = _chistoe_imya(board.title)[:120] or f"board-{board.id}"
    return f"{imya}.zip", fayly


def reorder_works(db: Session, board_id: int, work_ids: list[int]) -> list[Work]:
    board = get_board(db, board_id)
    works = boards_repo.list_works(db, board_id)
    existing_ids = {w.id for w in works}
    if set(work_ids) != existing_ids or len(work_ids) != len(existing_ids):
        raise errors.ValidationError(
            "work_ids must contain every work of the board exactly once", code="bad_order"
        )
    position = {work_id: (index + 1) * 10 for index, work_id in enumerate(work_ids)}
    for work in works:
        work.sort_order = position[work.id]
    board.updated_at = now_utc()
    return boards_repo.list_works(db, board_id)
