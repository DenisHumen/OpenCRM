from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.security import tokens
from core.services import audit_service, media_service, settings_service, storage_service
from core.utils import normalize_phone, now_utc
from database.models import Client, ClientFile, ClientNote, User
from database.models.audit import SOURCE_MANUAL
from database.models.client import MAX_SOURCE, NOTE_KINDS, SYSTEM_NOTE_KINDS
from database.models.user import ROLE_ROOT
from database.repositories import clients as clients_repo

# Внутренние документы клиентов: расширения, которые принимаем.
ALLOWED_CLIENT_FILE_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "csv",
    "zip", "rar", "7z", "jpg", "jpeg", "png", "webp", "gif", "svg", "psd",
    "ai", "fig", "sketch", "mp4", "webm", "mov",
}


def get_client(db: Session, client_id: int, include_deleted: bool = False) -> Client:
    client = clients_repo.get(db, client_id, include_deleted=include_deleted)
    if client is None:
        raise errors.NotFoundError("Client not found", code="client_not_found")
    return client


def create_client(db: Session, data: dict, author: User) -> Client:
    if not (data.get("name") or "").strip():
        raise errors.ValidationError("Name is required", code="name_required")
    phone = (data.get("phone") or "").strip()
    client = Client(
        name=data["name"].strip(),
        company=(data.get("company") or "").strip(),
        phone=phone,
        phone_norm=_normalize_phone_for(db, phone),
        email=(data.get("email") or "").strip(),
        messenger=(data.get("messenger") or "").strip(),
        tags=_normalize_tags(data.get("tags")),
        source=_normalize_source(data.get("source")),
        manager_id=data.get("manager_id") or author.id,
    )
    db.add(client)
    db.flush()
    return client


def update_client(db: Session, client_id: int, data: dict) -> Client:
    client = get_client(db, client_id)
    for field in ("name", "company", "phone", "email", "messenger"):
        if field in data and data[field] is not None:
            value = data[field].strip()
            if field == "name" and not value:
                raise errors.ValidationError("Name is required", code="name_required")
            setattr(client, field, value)
            if field == "phone":
                # нормализованный вид пересчитывается вместе с исходным, иначе
                # после правки телефона звонки уходили бы мимо карточки
                client.phone_norm = _normalize_phone_for(db, value)
    if "tags" in data and data["tags"] is not None:
        client.tags = _normalize_tags(data["tags"])
    # Источник снимается присланным null или пустой строкой — «выяснили, что
    # спрашивать было не у кого» бывает не реже, чем «выяснили откуда».
    if "source" in data:
        client.source = _normalize_source(data["source"])
    if "manager_id" in data:
        client.manager_id = data["manager_id"]
    client.updated_at = now_utc()
    return client


def delete_client(
    db: Session,
    client_id: int,
    actor: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> None:
    client = get_client(db, client_id)
    client.deleted_at = now_utc()
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_CLIENT,
        entity_id=client.id,
        entity_label=client.name,
    )


def restore_client(
    db: Session,
    client_id: int,
    actor: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> Client:
    """Вернуть клиента из корзины.

    Возврат попадает в журнал наравне с удалением. Иначе журнал врёт умолчанием:
    «удалил клиента» в нём есть, «вернул через час» — нет, и читающий через
    месяц уверен, что карточки не стало.
    """
    client = get_client(db, client_id, include_deleted=True)
    if client.deleted_at is None:
        # Возвращать нечего: запись и так на месте. Молча отвечать «готово»
        # нельзя — в журнале появилась бы отметка о возврате, которого не было.
        return client
    client.deleted_at = None
    db.flush()
    audit_service.record_restore(
        db,
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_CLIENT,
        entity_id=client.id,
        entity_label=client.name,
    )
    return client


def _normalize_phone_for(db: Session, phone: str) -> str:
    code = settings_service.get_all(db).get("default_country_code", "")
    return normalize_phone(phone, code)[:32]


def _normalize_tags(tags) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        parts = tags.split(",")
    else:
        parts = list(tags)
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return ",".join(dict.fromkeys(cleaned))  # без дубликатов, порядок сохранён


def _normalize_source(value) -> str | None:
    """Источник клиента в стабильный ключ или None.

    Пустая строка превращается в None намеренно: форма присылает "" при выборе
    «не указан», а хранить пустую строку рядом с NULL значило бы завести два
    разных «не знаю», по которым отчёт дал бы две строки вместо одной.

    Своё значение не приводим к латинице, в отличие от ключей этапов: оно
    никуда не уходит из базы, кроме отчёта и выгрузки, — а вот пропущенное
    через транслитерацию «Радио» превратилось бы в нечитаемое. Плата за это
    честная и указана в модели: пока справочника нет, ключ своего источника и
    есть его название.
    """
    if value is None:
        return None
    return (str(value).strip()[:MAX_SOURCE]) or None


# --- заметки / история взаимодействий ---

def add_note(
    db: Session,
    client_id: int,
    author: User,
    kind: str,
    body: str,
    happened_at=None,
    direction: str = "",
    deal_id: int | None = None,
) -> ClientNote:
    get_client(db, client_id)
    if kind not in NOTE_KINDS:
        raise errors.ValidationError(f"kind must be one of {NOTE_KINDS}", code="bad_note_kind")
    if not body.strip():
        raise errors.ValidationError("Note body is required", code="body_required")
    # Направление есть у звонка и письма; у заметки и встречи его нет, и
    # выдумывать его нельзя: «нет направления» — не то же, что «входящее».
    if direction not in ("", "in", "out"):
        raise errors.ValidationError("direction must be in or out", code="bad_direction")
    note = ClientNote(
        client_id=client_id,
        author_id=author.id,
        kind=kind,
        direction=direction,
        deal_id=deal_id,
        body=body.strip(),
        happened_at=happened_at or now_utc(),
    )
    db.add(note)
    db.flush()
    return note


def add_system_note(
    db: Session,
    client_id: int,
    actor: User | None,
    kind: str,
    body: str,
    deal_id: int | None = None,
    source: str = SOURCE_MANUAL,
) -> ClientNote:
    """Запись в ленте, порождённая действием, а не набранная в форме.

    Отдельный вход, а не флаг у `add_note`: у рукописной записи проверяется
    вид, потому что он приходит из запроса, а здесь вид задаёт код — и
    пропускать его через тот же список значило бы разрешить менеджеру прислать
    «смену этапа», которой не было.

    Исполнитель — тот же живой человек, чьё действие всё это запустило, а не
    «система»: лента отвечает на вопрос «кто», и ничьих строк в ней быть не
    должно. Пусто допускается ровно там же, где и в журнале, — у вебхука АТС и
    синхронизации почты, — и проверяет это `assert_actor`, а не внимательность
    того, кто добавит следующего подписчика.
    """
    if kind not in SYSTEM_NOTE_KINDS:
        raise errors.ValidationError(
            f"kind must be one of {SYSTEM_NOTE_KINDS}", code="bad_note_kind"
        )
    audit_service.assert_actor(actor, source, f"Feed entry {kind!r}")
    note = ClientNote(
        client_id=client_id,
        author_id=actor.id if actor else None,
        kind=kind,
        deal_id=deal_id,
        body=body.strip(),
        happened_at=now_utc(),
    )
    db.add(note)
    db.flush()
    return note


def delete_note(db: Session, client_id: int, note_id: int, actor: User) -> None:
    note = clients_repo.get_note(db, client_id, note_id)
    if note is None:
        raise errors.NotFoundError("Note not found", code="note_not_found")
    # Запись о событии не удаляется никем, включая root и самого автора.
    #
    # Автором системной записи стоит тот, кто двигал заявку, — и правило
    # «автор может удалить своё» отдало бы ему право стереть след
    # собственного действия. Лента при этом перестаёт отвечать на вопрос
    # «что происходило», ради которого она и заведена: рукописную заметку
    # убирают, потому что ошиблись при вводе, а смена этапа либо была, либо
    # нет, и передумать тут нечего.
    if note.kind in SYSTEM_NOTE_KINDS:
        raise errors.ForbiddenError(
            "System entries cannot be deleted", code="system_note_immutable"
        )
    if actor.role != ROLE_ROOT and note.author_id != actor.id:
        raise errors.ForbiddenError("Only the author or root can delete a note", code="not_note_author")
    # Снимок тела берём до удаления: после него от записи не останется ничего,
    # а «удалил заметку №17» не отвечает на вопрос, какую именно.
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_NOTE,
        entity_id=note.id,
        entity_label=note.body,
    )
    db.delete(note)


# --- файлы клиента (внутренние) ---

def _client_files_dir(client_id: int) -> Path:
    return get_settings().client_files_dir / str(client_id)


def file_path_on_disk(file: ClientFile) -> Path:
    ext = Path(file.original_name).suffix
    return _client_files_dir(file.client_id) / f"{file.file_uid}{ext}"


def add_file(db: Session, client_id: int, uploader: User, original_name: str, content: bytes, mime: str) -> ClientFile:
    get_client(db, client_id)
    ext = Path(original_name).suffix.lstrip(".").lower()
    if ext not in ALLOWED_CLIENT_FILE_EXTS:
        raise errors.ValidationError(f"File type .{ext} is not allowed", code="file_type_not_allowed")
    if len(content) > get_settings().max_upload_bytes:
        raise errors.ValidationError("File is too large", code="file_too_large")
    if not content:
        raise errors.ValidationError("File is empty", code="file_empty")
    if not storage_service.has_room_for(len(content)):
        raise errors.ValidationError(
            "Not enough free disk space on the server", code="disk_full"
        )

    # SVG чистится, как везде. Здесь этого не делалось, и файл клиента был
    # единственным местом, куда `<script>` доезжал до диска нетронутым.
    #
    # Сегодня цена невелика: файл виден только вошедшему сотруднику и
    # отдаётся ВЛОЖЕНИЕМ с `nosniff` — браузер его не рисует. Но безопасность
    # держится тогда на одном заголовке в одном маршруте: появится
    # предпросмотр в списке файлов (а он напрашивается) — и скрипт сработает
    # на странице сотрудника, в его сессии. Чистый файл на диске переживает
    # любую такую правку, грязный — нет.
    #
    # Размер считается ПОСЛЕ очистки: в базу должно попасть то, что вправду
    # лежит на диске, иначе список файлов покажет одно, а скачается другое.
    if ext == "svg":
        content = media_service.sanitize_svg(content)
        if not content.strip():
            raise errors.ValidationError(
                "The SVG contained nothing but scripts", code="file_empty"
            )

    file = ClientFile(
        client_id=client_id,
        uploaded_by=uploader.id,
        file_uid=tokens.new_file_uid(),
        original_name=Path(original_name).name[:255],
        mime=mime or "application/octet-stream",
        size_bytes=len(content),
    )
    db.add(file)
    db.flush()
    directory = _client_files_dir(client_id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path_on_disk(file).write_bytes(content)
    return file


def get_file(db: Session, client_id: int, file_id: int) -> ClientFile:
    file = clients_repo.get_file(db, client_id, file_id)
    if file is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    return file


def delete_file(db: Session, client_id: int, file_id: int, actor: User) -> None:
    file = get_file(db, client_id, file_id)
    path = file_path_on_disk(file)
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=file.id,
        entity_label=file.original_name,
    )
    db.delete(file)
    db.flush()
    # Файл снимаем с диска ПОСЛЕ коммита, а не сразу. Откат транзакции вернул бы
    # строку в базу, а файла уже нет: карточка клиента обещает договор, которого
    # физически не существует, и скачивание падает. Сирота на диске (обратный
    # случай) стоит места, а не правды.
    @sa_event.listens_for(db, "after_commit", once=True)
    def _remove_from_disk(_session) -> None:
        path.unlink(missing_ok=True)
