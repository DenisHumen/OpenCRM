from datetime import timedelta
from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import strany
from core.security import tokens
from core.services import (
    audit_service,
    media_service,
    report_service,
    settings_service,
    storage_service,
)
from core.utils import normalize_phone, now_utc
from database.models import Client, ClientFile, ClientNote, User
from database.models.audit import SOURCE_MANUAL
from database.models.client import MAX_SOURCE, NOTE_KINDS, SYSTEM_NOTE_KINDS
from database.models.user import ROLE_ROOT
from database.repositories import clients as clients_repo
from database.repositories import users as users_repo

# Внутренние документы клиентов: расширения, которые принимаем.
#: Подпись в первых байтах — по расширениям, у которых она однозначна.
#:
#: Расширение выбирает загружающий, содержимое — нет. Пока сходились они
#: только на слово, `otchet.pdf` мог оказаться чем угодно, а `logotip.png` —
#: страницей со скриптом. Сегодня файл отдаётся вложением с `nosniff`, и
#: браузер его не рисует; но это ровно тот довод, который уже подводил с SVG:
#: безопасность держалась на одном заголовке в одном маршруте, а появись
#: предпросмотр в списке файлов — и подделка сработала бы в сессии сотрудника.
#:
#: Проверяем только те, где подпись однозначна. Остальные (`txt`, `csv`, `rtf`,
#: `ai`, `fig`, `sketch`) подписи не имеют вовсе либо имеют её общей с чужими
#: форматами: выдумать её значило бы отказывать в настоящих файлах. Для них
#: проверка остаётся по расширению, как была.
PODPISI_PO_RASSHIRENIYU: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    # Современные офисные — это zip. Он же у самого zip.
    "docx": (bytes.fromhex("504b0304"),),
    "xlsx": (bytes.fromhex("504b0304"),),
    "pptx": (bytes.fromhex("504b0304"),),
    "zip": (
        bytes.fromhex("504b0304"),
        bytes.fromhex("504b0506"),   # пустой архив
        bytes.fromhex("504b0708"),   # разрезанный на тома
    ),
    # Старые офисные — контейнер OLE, один на doc/xls/ppt.
    "doc": (bytes.fromhex("d0cf11e0"),),
    "xls": (bytes.fromhex("d0cf11e0"),),
    "ppt": (bytes.fromhex("d0cf11e0"),),
    "rar": (bytes.fromhex("526172211a07"),),
    "7z": (bytes.fromhex("377abcaf271c"),),
    "psd": (b"8BPS",),
    "jpg": (bytes.fromhex("ffd8ff"),),
    "jpeg": (bytes.fromhex("ffd8ff"),),
    "png": (bytes.fromhex("89504e470d0a1a0a"),),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (),   # разбирается отдельно: подпись не в начале файла
    "webm": (bytes.fromhex("1a45dfa3"),),
    "mp4": (),    # контейнер ISO BMFF: подпись на четвёртом байте
    "mov": (),    # он же
}

#: Чем отдавать файл наружу. Присланный `Content-Type` сюда не попадает вовсе:
#: его выбирает тот, кто загружает, а уходит он в заголовок ответа — то есть
#: значение, выбранное посторонним, возвращалось бы браузеру сотрудника.
MIME_PO_RASSHIRENIYU: dict[str, str] = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "rtf": "application/rtf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "rar": "application/vnd.rar",
    "7z": "application/x-7z-compressed",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "psd": "image/vnd.adobe.photoshop",
    "ai": "application/postscript",
    "fig": "application/octet-stream",
    "sketch": "application/octet-stream",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
}

ALLOWED_CLIENT_FILE_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "csv",
    "zip", "rar", "7z", "jpg", "jpeg", "png", "webp", "gif", "svg", "psd",
    "ai", "fig", "sketch", "mp4", "webm", "mov",
}


#: Сколько строк отдаём одной выгрузкой.
#:
#: Не круглое число ради круглого: большой ответ занимает слот обработчика на
#: всё время отдачи — та самая форма, которой 24 августа положили сервер
#: (разбор — в `docs/ekspluatatsiya/08-razvyortyvanie.md`). Десять тысяч строк это около двух
#: мегабайт и доли секунды, то есть заведомо ниже порога, за которым выгрузка
#: начинает мешать работать остальным.
#:
#: Больше — не молчаливое обрезание, а честный отказ: файл, в котором тихо
#: недостаёт половины клиентов, хуже отсутствующего файла. Тем, кому нужно
#: больше, есть чем сузить отбор — он тот же, что на экране.
PREDEL_VYGRUZKI = 10_000


def vygruzka_csv(
    db: Session,
    q: str | None = None,
    tag: str | None = None,
    manager_id: int | None = None,
) -> bytes:
    """Список клиентов файлом — тем же отбором, что показан на экране.

    Ответственный подписывается ИМЕНЕМ, а не номером: `manager_id=7` в таблице
    Excel не отвечает ни на один вопрос, ради которого выгрузку и открывают.
    Имена берутся одной пачкой — запрос на строку превратил бы выгрузку в
    десять тысяч обращений к базе.
    """
    klienty, est_eshchyo = clients_repo.dlya_vygruzki(
        db, q=q, tag=tag, manager_id=manager_id, predel=PREDEL_VYGRUZKI
    )
    if est_eshchyo:
        raise errors.ValidationError(
            f"Too many clients to export at once (limit {PREDEL_VYGRUZKI}); "
            "narrow the filter",
            code="export_too_large",
        )

    imena = {
        person.id: person.name
        for person in users_repo.get_many(
            db, [c.manager_id for c in klienty if c.manager_id]
        )
    }
    stroki = [
        [
            c.name,
            c.company or "",
            c.phone or "",
            c.email or "",
            c.messenger or "",
            ", ".join(x for x in c.tags.split(",") if x),
            c.source or "",
            imena.get(c.manager_id, ""),
            c.created_at.strftime("%Y-%m-%d") if c.created_at else "",
        ]
        for c in klienty
    ]
    return report_service.to_csv(
        stroki,
        ["Имя", "Компания", "Телефон", "Почта", "Мессенджер", "Метки",
         "Источник", "Ответственный", "Заведён"],
    )


def get_client(db: Session, client_id: int, include_deleted: bool = False) -> Client:
    client = clients_repo.get(db, client_id, include_deleted=include_deleted)
    if client is None:
        raise errors.NotFoundError("Client not found", code="client_not_found")
    return client


#: Поля адреса: колонка, ширина, подпись в отказе, код отказа.
#:
#: Длина проверяется здесь, а не оставляется базе: строка длиннее колонки роняет
#: вставку отказом `Data too long for column`, и магазин, приславший длинный
#: адрес, получил бы пятисотку вместо внятного «слишком длинно». Та же беда уже
#: случалась с почтой заявки — см. `lead_service`.
#: Длина берётся У КОЛОНКИ, а не переписывается сюда числом: расширят колонку
#: миграцией — отказ остался бы на прежней ширине и разошёлся бы с базой молча.
POLYA_ADRESA = tuple(
    (imya, Client.__table__.c[imya].type.length, podpis, kod)
    for imya, podpis, kod in (
        ("city", "City", "city_too_long"),
        ("zip_code", "Postal code", "zip_too_long"),
        ("address", "Address", "address_too_long"),
    )
)


def _chistoe_pole(value, limit: int, *, label: str, code: str) -> str:
    tekst = (value or "").strip()
    if len(tekst) > limit:
        raise errors.ValidationError(
            f"{label} is too long (max {limit} characters)", code=code
        )
    return tekst


def _chistaya_strana(value) -> str:
    """Код страны ISO 3166-1 alpha-2 либо пусто.

    Двумя буквами, а не названием: название пишут по-разному («США», «USA»,
    «Соединённые Штаты»), и ни отбора, ни расчёта доставки по нему не собрать.
    Регистр приводим сами — пришедшее с сайта `ua` обязано лечь как `UA`, иначе
    один и тот же адрес даст две разные страны.
    """
    kod = (value or "").strip().upper()
    if not kod:
        return ""
    if len(kod) != 2 or not kod.isascii() or not kod.isalpha():
        raise errors.ValidationError(
            "Country must be a two-letter ISO code", code="country_invalid"
        )
    return kod


def _mezhdunarodnyy(db: Session, phone: str) -> bool:
    """Можно ли по этому номеру судить о стране.

    Местный номер без кода страны судить не даёт: московский `4951234567` при
    подборе по длине читается как `49` — Германия, и чужой флаг ляжет в карточку
    молча. Судим, только если человек написал номер международно (`+`, `00`)
    либо владелец назвал код своей страны в настройках — тогда `normalize_phone`
    его допишет, и это будет ЕГО страна, а не догадка.
    """
    if (phone or "").strip().startswith(("+", "00")):
        return True
    return bool(settings_service.get_all(db).get("default_country_code", ""))


def _adres_iz(data: dict, *, phone_norm: str = "") -> dict:
    adres = {
        imya: _chistoe_pole(data.get(imya), limit, label=label, code=code)
        for imya, limit, label, code in POLYA_ADRESA
    }
    # Страну подсказывает код номера, если её не назвали: она уже названа —
    # первыми цифрами телефона, — и спрашивать её второй раз незачем.
    adres["country"] = _chistaya_strana(data.get("country")) or strany.strana_po_nomeru(
        phone_norm
    )
    return adres


def create_client(db: Session, data: dict, author: User) -> Client:
    if not (data.get("name") or "").strip():
        raise errors.ValidationError("Name is required", code="name_required")
    phone = (data.get("phone") or "").strip()
    norm = _normalize_phone_for(db, phone)
    client = Client(
        name=data["name"].strip(),
        company=(data.get("company") or "").strip(),
        phone=phone,
        phone_norm=norm,
        email=(data.get("email") or "").strip(),
        messenger=(data.get("messenger") or "").strip(),
        tags=_normalize_tags(data.get("tags")),
        source=_normalize_source(data.get("source")),
        manager_id=data.get("manager_id") or author.id,
        **_adres_iz(
            data,
            phone_norm=norm if _mezhdunarodnyy(db, phone) else "",
        ),
    )
    db.add(client)
    db.flush()
    return client


def update_client(db: Session, client_id: int, data: dict) -> Client:
    client = get_client(db, client_id)
    bylo_norm = client.phone_norm
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
    # Адрес правится по частям: прислали один город — меняется только он.
    for imya, limit, label, code in POLYA_ADRESA:
        if imya in data:
            setattr(client, imya, _chistoe_pole(data[imya], limit, label=label, code=code))
    if client.phone_norm != bylo_norm and _mezhdunarodnyy(db, client.phone):
        # Страна едет за номером, но только если в карточке стоит ровно то, что
        # говорил ПРЕЖНИЙ номер: значит её никто не правил руками. Иначе правка
        # телефона молча затирала бы страну, названную человеком.
        if client.country == strany.strana_po_nomeru(bylo_norm):
            client.country = strany.strana_po_nomeru(client.phone_norm) or client.country
    # Названная явно страна сильнее подсказки по номеру — поэтому ниже, а не выше.
    if "country" in data:
        client.country = _chistaya_strana(data["country"])
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


# --- сводка карточки ---


KONTAKTY = ("note", "call", "meeting", "email")


def svodka(db: Session, client_id: int, user: User) -> dict:
    """Что справа от паспорта: заявки, деньги, последний контакт, бумаги, кто ведёт.

    Считается на каждое открытие карточки, а не хранится, и сужается теми же
    правами, что разделы: чужие заявки в счёт не идут, суммы пустеют без права
    на них, выключенный блок отсутствует ключом `None`, а не нулём.
    """
    # Ввозы внутри: права и блоки сами ввозят клиентов, и общий верх дал бы круг.
    from core.services import modules_service, permissions_service
    from database.repositories import deals as deals_repo
    from database.repositories import documents as documents_repo
    from database.repositories import finance as finance_repo
    from database.repositories import telephony as telephony_repo

    client = get_client(db, client_id)
    scope = permissions_service.deals_scope(db, user)
    amounts = permissions_service.sees_amounts(db, user)
    zayavki = deals_repo.svodka_klienta(db, client_id, only_manager_id=scope)

    poluchen = None
    if modules_service.is_enabled(db, "finance") and amounts and permissions_service.has(db, user, "finance", "view"):
        poluchen = finance_repo.received_of_client(db, client_id, since=now_utc() - timedelta(days=365))

    # Только живые записи: сводка называет это «последним контактом», а системная
    # строка про выданный бланк, будучи свежее звонка, занимала его место.
    notes, _vsego = clients_repo.list_notes(db, client_id, page=1, per_page=1, kinds=KONTAKTY)
    posledniy = None
    if notes:
        zapis = notes[0]
        posledniy = {
            "kind": zapis.kind,
            "at": zapis.happened_at.isoformat() if zapis.happened_at else None,
            "body": (zapis.body or "")[:120],
        }
    posledniy_zvonok = None
    if modules_service.is_enabled(db, "telephony") and permissions_service.has(db, user, "telephony", "view"):
        zvonki, _vsego = telephony_repo.list_calls(db, client_id=client_id, page=1, per_page=1)
        if zvonki:
            posledniy_zvonok = zvonki[0].started_at.isoformat() if zvonki[0].started_at else None

    bumagi = None
    if modules_service.is_enabled(db, "documents") and permissions_service.has(db, user, "documents", "view"):
        bumagi = documents_repo.schyot_po_vidam(db, client_id=client_id)

    vedyot = None
    if client.manager_id:
        lyudi = users_repo.get_many(db, {client.manager_id})
        vedyot = lyudi[0].name if lyudi else None

    return {
        "open_count": zayavki["open_count"],
        "open_amount": zayavki["open_amount"] if amounts else None,
        "won_count": zayavki["won_count"],
        "won_amount": zayavki["won_amount"] if amounts else None,
        "lost_count": zayavki["lost_count"],
        "received_12m": poluchen,
        "last_contact": posledniy,
        "last_call_at": posledniy_zvonok,
        "papers": bumagi,
        "papers_total": sum(bumagi.values()) if bumagi else 0,
        "manager_name": vedyot,
    }


# --- файлы клиента (внутренние) ---

def _client_files_dir(client_id: int) -> Path:
    return get_settings().client_files_dir / str(client_id)


def file_path_on_disk(file: ClientFile) -> Path:
    ext = Path(file.original_name).suffix
    return _client_files_dir(file.client_id) / f"{file.file_uid}{ext}"


def _soderzhimoe_sootvetstvuet(ext: str, content: bytes) -> bool:
    """Похоже ли содержимое на то, чем файл назвался.

    Расширений, у которых подписи нет, здесь нет вовсе — для них ответ «да»:
    выдуманная подпись отказывала бы в настоящих файлах, а это хуже, чем
    пропустить неизвестное.

    Контейнеры ISO BMFF (`mp4`, `mov`) и `webp` разбираются отдельно: подпись
    у них не в начале файла, и общее правило «первые байты» на них не ложится.
    """
    if ext in ("mp4", "mov"):
        return len(content) > 12 and content[4:8] == b"ftyp"
    if ext == "webp":
        return len(content) > 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    podpisi = PODPISI_PO_RASSHIRENIYU.get(ext)
    if not podpisi:
        return True
    return content.startswith(podpisi)


def mime_dlya_otdachi(file: ClientFile) -> str:
    """Чем отдавать файл. Считается из имени, а не берётся из записи.

    В записи у файлов, залитых до этой правки, лежит присланный при загрузке
    `Content-Type` — то есть значение, выбранное посторонним и уходившее в
    заголовок ответа. Считая из имени, мы закрываем и старые записи тоже:
    переписывать их миграцией нечем, потому что имя — единственное, что о них
    достоверно известно.
    """
    ext = Path(file.original_name).suffix.lstrip(".").lower()
    return MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream")


def proverit_vlozhenie(original_name: str, content: bytes, dopustimo: set[str]) -> tuple[str, bytes]:
    """Приёмка файла, одна на всех: расширение из перечня, содержимое похоже
    на него, не пуст, не велик, место на диске есть. Отвечает расширением и
    содержимым (SVG — уже очищенным). Файлы клиента и вложения бумаг
    принимаются здесь же — второй приёмке нечем было бы отличаться, кроме
    забытых проверок."""
    ext = Path(original_name).suffix.lstrip(".").lower()
    if ext not in dopustimo:
        raise errors.ValidationError(f"File type .{ext} is not allowed", code="file_type_not_allowed")
    if content and not _soderzhimoe_sootvetstvuet(ext, content):
        # Отказ по содержимому, а не по расширению: файл назвался одним, а
        # внутри другое. Пустой не проверяем — на него есть свой отказ ниже,
        # и он говорит понятнее.
        raise errors.ValidationError(
            f"The file does not look like a .{ext}", code="file_content_mismatch"
        )
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
    return ext, content


def add_file(
    db: Session,
    client_id: int,
    uploader: User,
    original_name: str,
    content: bytes,
    mime: str = "",
) -> ClientFile:
    """Принять файл клиента.

    `mime` принимается и НЕ используется. Довод не в вежливости к вызывающему:
    заголовок присылает тот, кто загружает, и уйти он должен ровно никуда.
    Оставлен в подписи, чтобы правка не выглядела как «поле потеряли» и чтобы
    следующий, кому он понадобится, прочитал здесь, почему его не берут.
    """
    get_client(db, client_id)
    ext, content = proverit_vlozhenie(original_name, content, ALLOWED_CLIENT_FILE_EXTS)

    file = ClientFile(
        client_id=client_id,
        uploaded_by=uploader.id,
        file_uid=tokens.new_file_uid(),
        original_name=Path(original_name).name[:255],
        # Присланный `Content-Type` не сохраняем вовсе: он выбирается тем, кто
        # загружает, а уходит в заголовок ответа сотруднику. Тип считаем сами
        # из расширения, которое к этому месту уже сверено с содержимым.
        mime=MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream"),
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
