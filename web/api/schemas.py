"""Pydantic-схемы запросов и сериализация моделей в JSON-ответы."""

from datetime import datetime

from pydantic import BaseModel, Field

from core.services import deal_service, media_service, warehouse_service
from core.utils import is_online, now_utc
from database.models import (
    AuditEvent,
    Board,
    Client,
    ClientFile,
    ClientNote,
    Company,
    Deal,
    DealStageChange,
    Document,
    MailAccount,
    MailMessage,
    PhoneCall,
    PipelineStage,
    Product,
    ShareLink,
    ShareView,
    StockMove,
    Task,
    User,
    Work,
)


# --- запросы ---

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class ProfileIn(BaseModel):
    name: str | None = None
    locale: str | None = None


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class ClientIn(BaseModel):
    name: str
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    messenger: str | None = None
    tags: str | list[str] | None = None
    # Адрес для отправки. `country` — код ISO 3166-1 alpha-2 (`UA`, `PL`),
    # регистр приводится сервисом. Разбор — docs/19-sborka-zakaza.md §Р7.
    country: str | None = None
    city: str | None = None
    zip_code: str | None = None
    address: str | None = None
    # Откуда пришёл: ключ из справочника или своё слово. Пусто и null означают
    # одно — «не спросили»; это НЕ то же самое, что источник «другое».
    source: str | None = None
    manager_id: int | None = None


class DealIn(BaseModel):
    title: str
    client_id: int
    manager_id: int | None = None
    # От чьего имени ведём. Не прислали — работаем от фирмы по умолчанию.
    company_id: int | None = None
    stage: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    # Деньги — целыми в минимальных единицах (копейки, центы). int, а не float:
    # на дробных округление вылезает всегда, и сумма колонки канбана расходится
    # с суммой карточек.
    amount: int | None = None
    prepaid: int | None = None


class DealPatchIn(BaseModel):
    title: str | None = None
    client_id: int | None = None
    manager_id: int | None = None
    company_id: int | None = None
    stage: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    lost_reason: str | None = None
    amount: int | None = None
    prepaid: int | None = None


class DealMoveIn(BaseModel):
    stage: str
    # Позиция внутри колонки. Не задана — карточка встаёт в конец.
    sort_order: int | None = None
    lost_reason: str | None = None


class CompanyIn(BaseModel):
    """Своё юрлицо. Обязательно только краткое название — с одним полем фирму
    заводят за секунду, а реквизиты дописывают, когда дойдут руки до первого
    счёта. Требовать всё сразу значит не завести фирму вовсе."""

    name: str
    legal_name: str | None = None
    is_default: bool | None = None
    tax_number: str | None = None
    reg_number: str | None = None
    vat_number: str | None = None
    legal_address: str | None = None
    actual_address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    bank_code: str | None = None
    signatory_name: str | None = None
    signatory_basis: str | None = None
    signature_path: str | None = None
    stamp_path: str | None = None
    note: str | None = None


class CompanyPatchIn(CompanyIn):
    name: str | None = None


class DealLineIn(BaseModel):
    """Строка заявки. Товар — по номеру записи или по артикулу; ни того, ни
    другого — своя трата, и тогда обязательно `name`."""

    product_id: int | None = None
    sku: str | None = None
    #: Отсканированный штрихкод. Ищется первым: коробка уже в руках.
    code: str | None = None
    #: С какого склада берём. Пусто — решится при списании (§Р4).
    warehouse_id: int | None = None
    name: str | None = None
    # Количество разбирает сервер: у него три знака после запятой, и
    # `Math.round(0.3335 * 1000)` в браузере даст 334 — см. `parse_quantity`.
    quantity: str | int | float | None = None
    price: int | None = None


class DealLinePatchIn(BaseModel):
    quantity: str | int | float | None = None
    price: int | None = None
    warehouse_id: int | None = None
    name: str | None = None


class ClientPatchIn(BaseModel):
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    messenger: str | None = None
    tags: str | list[str] | None = None
    # Адрес для отправки. `country` — код ISO 3166-1 alpha-2 (`UA`, `PL`),
    # регистр приводится сервисом. Разбор — docs/19-sborka-zakaza.md §Р7.
    country: str | None = None
    city: str | None = None
    zip_code: str | None = None
    address: str | None = None
    source: str | None = None
    manager_id: int | None = None


class NoteIn(BaseModel):
    kind: str = "note"
    body: str
    happened_at: datetime | None = None
    # Входящее или исходящее — у звонка и письма. У заметки пусто.
    direction: str = ""
    deal_id: int | None = None


class BoardIn(BaseModel):
    title: str
    description: str | None = None
    client_id: int | None = None
    # Заявка, ради которой доска сделана. Необязательная: доски существовали
    # до заявок и обязаны работать без них.
    deal_id: int | None = None


class BoardPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None
    client_id: int | None = None
    deal_id: int | None = None
    cover_work_id: int | None = None
    is_published: bool | None = None


class WorkPatchIn(BaseModel):
    title: str | None = None
    description: str | None = None
    project_url: str | None = None
    # какой фрагмент длинной работы видно на витрине; null — от верха
    preview_focus: float | None = None


class WorkOrderIn(BaseModel):
    work_ids: list[int]


class ShareIn(BaseModel):
    expires_at: datetime | None = None
    pin: str | None = None


class SharePatchIn(BaseModel):
    is_active: bool | None = None
    expires_at: datetime | None = None
    pin: str | None = None


class SettingsPatchIn(BaseModel):
    values: dict[str, str]


class RoleUpdateIn(BaseModel):
    role: str


#: Количество принимаем и строкой, и числом — в отличие от денег, которые
#: приходят уже целыми в минимальных единицах. Разбирает его сервер через
#: Decimal (`warehouse_service.parse_quantity`): у количества три знака после
#: запятой, и `Math.round(0.3335 * 1000)` в браузере дал бы 334 — то самое
#: молчаливое округление, из-за которого одно и то же списывают дважды.
Quantity = str | int | float | None


class ProductIn(BaseModel):
    name: str
    sku: str | None = None
    unit: str = "pcs"
    # Деньги — целыми в минимальных единицах, как у сделок. None и 0 разные:
    # «цену не назвали» и «отдаём бесплатно».
    price: int | None = None
    cost: int | None = None
    is_service: bool = False
    min_stock: Quantity = None
    note: str | None = None
    # Описание для покупателя на сайте — не `note`: та для кладовщика.
    site_description: str | None = None


class ProductPatchIn(BaseModel):
    name: str | None = None
    sku: str | None = None
    unit: str | None = None
    price: int | None = None
    cost: int | None = None
    is_service: bool | None = None
    min_stock: Quantity = None
    note: str | None = None
    site_description: str | None = None


class StockMoveIn(BaseModel):
    product_id: int
    quantity: Quantity = None
    kind: str
    deal_id: int | None = None
    # Где это произошло. Не прислали — основной склад: пока склад один, форма о
    # нём вообще не спрашивает (см. `warehouse_service.warehouse_count`).
    warehouse_id: int | None = None
    cost: int | None = None
    comment: str | None = None
    happened_at: datetime | None = None


class WarehouseIn(BaseModel):
    name: str
    code: str | None = None
    address: str | None = None
    note: str | None = None
    is_default: bool = False
    # Тип места (`WAREHOUSE_KINDS`); `shop` — то, что видит сайт магазина.
    kind: str | None = None


class WarehousePatchIn(BaseModel):
    name: str | None = None
    code: str | None = None
    address: str | None = None
    note: str | None = None
    is_default: bool = False
    kind: str | None = None


class TransferIn(BaseModel):
    product_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    quantity: Quantity = None
    comment: str | None = None
    happened_at: datetime | None = None
    # Явное согласие увезти больше, чем лежит. По умолчанию такой переезд
    # останавливается: с пустого склада увезти нечего физически, и молчаливый
    # минус здесь означал бы, что коробку «перевезли» из воздуха.
    confirm_negative: bool = False


# --- сериализация ---

def user_out(user: User, role=None, permissions: list[str] | None = None) -> dict:
    """Сотрудник для ответа API.

    `permissions` кладём только там, где они нужны самому владельцу сессии
    (`/auth/me`): интерфейс по ним решает, что показывать. В списке сотрудников
    их нет намеренно — чужой набор прав это чужое дело, а на экране он всё равно
    показывается названием должности.
    """
    data = {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "role_id": user.role_id,
        "role_name": role.name if role is not None else None,
        "status": user.status,
        "locale": user.locale,
        "must_change_password": user.must_change_password,
        "avatar_url": user.avatar_path or None,
        "last_seen_at": _iso(user.last_seen_at),
        "is_online": is_online(user.last_seen_at),
        "created_at": _iso(user.created_at),
        "approved_at": _iso(user.approved_at),
    }
    if permissions is not None:
        data["permissions"] = permissions
    return data


def role_out(role, codes: list[str] | None = None, users_count: int | None = None) -> dict:
    data = {
        "id": role.id,
        "name": role.name,
        "preset": role.preset or "",
        "is_default": role.is_default,
        "created_at": _iso(role.created_at),
        "updated_at": _iso(role.updated_at),
    }
    if codes is not None:
        data["permissions"] = codes
    if users_count is not None:
        data["users_count"] = users_count
    return data


def client_out(client: Client) -> dict:
    return {
        "id": client.id,
        "name": client.name,
        "company": client.company,
        "phone": client.phone,
        "email": client.email,
        "messenger": client.messenger,
        "country": client.country,
        "city": client.city,
        "zip_code": client.zip_code,
        "address": client.address,
        "tags": [t for t in client.tags.split(",") if t],
        # null, а не пустая строка: интерфейс обязан отличать «не спросили» от
        # выбранного источника, а пустая строка выглядит как выбор.
        "source": client.source,
        "manager_id": client.manager_id,
        "created_at": _iso(client.created_at),
        "updated_at": _iso(client.updated_at),
        "deleted_at": _iso(client.deleted_at),
    }


def company_out(company: Company) -> dict:
    return {
        "id": company.id,
        "name": company.name,
        "legal_name": company.legal_name,
        "is_default": company.is_default,
        "tax_number": company.tax_number,
        "reg_number": company.reg_number,
        "vat_number": company.vat_number,
        "legal_address": company.legal_address,
        "actual_address": company.actual_address,
        "phone": company.phone,
        "email": company.email,
        "website": company.website,
        "bank_name": company.bank_name,
        "bank_account": company.bank_account,
        "bank_code": company.bank_code,
        "signatory_name": company.signatory_name,
        "signatory_basis": company.signatory_basis,
        "signature_path": company.signature_path,
        "stamp_path": company.stamp_path,
        "note": company.note,
        "created_at": _iso(company.created_at),
        "updated_at": _iso(company.updated_at),
    }


def _payload_of(document: Document) -> dict:
    """Снимок для печати из хранимого JSON. Битый — показываем что есть: он не
    должен ронять ни карточку, ни список целиком."""
    import json as _json

    try:
        return _json.loads(document.payload or "{}")
    except ValueError:
        return {}


def document_out(document: Document) -> dict:
    payload = _payload_of(document)
    return {
        "id": document.id,
        "number": document.number,
        "kind": document.kind,
        "locale": document.locale,
        "status": document.status,
        "client_id": document.client_id,
        "deal_id": document.deal_id,
        "payload": payload,
        "created_at": _iso(document.created_at),
        "updated_at": _iso(document.updated_at),
    }


def task_out(
    task: Task,
    assignee_name: str | None = None,
    client_name: str | None = None,
    deal_title: str | None = None,
) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        # Время уходит в ISO с явным Z: без него браузер разберёт его как
        # местное, и срок уедет на величину смещения.
        "due_at": _iso(task.due_at),
        "assignee_id": task.assignee_id,
        "assignee_name": assignee_name,
        "client_id": task.client_id,
        "client_name": client_name,
        "deal_id": task.deal_id,
        "deal_title": deal_title,
        "is_done": task.done_at is not None,
        "done_at": _iso(task.done_at),
        "created_at": _iso(task.created_at),
    }


def stage_out(stage: PipelineStage) -> dict:
    return {
        "key": stage.key,
        "name": stage.name,
        "kind": stage.kind,
        "sort_order": stage.sort_order,
        "color": stage.color,
        "is_archived": stage.is_archived,
    }


#: Ключи ответа, в которых лежат деньги заявки. Списком, а не перебором по
#: имени: `money_of` считает ещё и `is_paid`, а «оплачено ли» — это тот же ответ
#: про деньги, просто в булевом виде. Скрывать сумму, оставив признак оплаты,
#: значит скрыть её наполовину.
DEAL_MONEY_KEYS = ("amount", "prepaid", "remainder", "is_paid")


def deal_out(
    deal: Deal,
    client_name: str | None = None,
    manager: User | None = None,
    amounts: bool = True,
) -> dict:
    """`amounts=False` — у смотрящего нет права `deals.view_amounts`.

    Ключи остаются на месте и приходят пустыми, а не исчезают: форма ответа не
    должна зависеть от того, кто спрашивает, — иначе клиенту API придётся
    угадывать, какие поля сегодня бывают. Ровно то же правило, что у выключенных
    блоков в сводке.
    """
    data = _deal_fields(deal, client_name, manager)
    if not amounts:
        for key in DEAL_MONEY_KEYS:
            data[key] = None
    return data


def _deal_fields(deal: Deal, client_name: str | None, manager: User | None) -> dict:
    return {
        "id": deal.id,
        "title": deal.title,
        "client_id": deal.client_id,
        # Имя клиента и ответственного кладём в ответ, а не заставляем фронт
        # добирать их отдельным запросом на каждую карточку канбана. Без имени
        # ответственного доска не отвечает на первый же вопрос: кто это ведёт.
        "client_name": client_name,
        "manager_id": deal.manager_id,
        "manager_name": manager.name if manager else None,
        "manager_avatar": (manager.avatar_path or None) if manager else None,
        # Название фирмы не кладём сюда: в списке и на канбане оно не нужно
        # (у большинства фирма одна), а карточка добирает его сама.
        "company_id": deal.company_id,
        "stage": deal.stage,
        "sort_order": deal.sort_order,
        "description": deal.description,
        # Остаток и признак оплаты считаются, а не хранятся: лишнее поле в базе
        # разошлось бы с суммой и предоплатой при первой же правке.
        **deal_service.money_of(deal),
        "due_at": _iso(deal.due_at),
        "lost_reason": deal.lost_reason,
        "closed_at": _iso(deal.closed_at),
        "created_at": _iso(deal.created_at),
        "updated_at": _iso(deal.updated_at),
    }


def stage_change_out(change: DealStageChange, author_name: str | None = None) -> dict:
    return {
        "id": change.id,
        "from_stage": change.from_stage,
        "to_stage": change.to_stage,
        "author_name": author_name,
        "changed_at": _iso(change.changed_at),
    }


def note_out(note: ClientNote, author_name: str | None = None) -> dict:
    """Запись ленты наружу.

    Имя автора приходит отдельным аргументом, а не берётся из `note.author`:
    лента отдаётся списками, и обращение к связи на каждой строке — это запрос
    на запись. Имена вызывающий берёт пачкой (`users_repo.get_many`), как это
    уже сделано в истории этапов.

    Без имени лента не отвечает на вопрос «кто», ради которого она наполовину и
    заведена: у записи есть `author_id`, но число на экране никому ни о чём не
    говорит. Пусто — законное состояние: у письма и звонка, пришедших извне,
    живого автора нет.
    """
    return {
        "id": note.id,
        "client_id": note.client_id,
        "author_id": note.author_id,
        "author_name": author_name,
        "kind": note.kind,
        "direction": note.direction or None,
        "deal_id": note.deal_id,
        "body": note.body,
        "happened_at": _iso(note.happened_at),
        "created_at": _iso(note.created_at),
    }


def audit_event_out(entry: AuditEvent) -> dict:
    """Запись журнала наружу.

    Имя исполнителя отдаём снимком из самой записи, а не подставляем текущее из
    `users`: сотрудник переименовался или уволился — журнал обязан читаться так
    же, как читался в день записи. `actor_id` рядом нужен только чтобы нажать на
    имя и посмотреть, что ещё делал этот человек.
    """
    return {
        "id": entry.id,
        "action": entry.action,
        "actor_id": entry.actor_id,
        "actor_name": entry.actor_name,
        "source": entry.source,
        "source_ref": entry.source_ref,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "entity_label": entry.entity_label,
        # Было и стало отдаём порознь и как есть: склеенная на сервере строка
        # «5000 → 500» не переводится и не выравнивается в колонку.
        "value_before": entry.value_before,
        "value_after": entry.value_after,
        "created_at": _iso(entry.created_at),
    }


def call_out(call: PhoneCall) -> dict:
    return {
        "id": call.id,
        "external_id": call.external_id,
        "direction": call.direction,
        "from_number": call.from_number,
        "to_number": call.to_number,
        # С кем говорили: во входящем это звонивший, в исходящем — вызываемый.
        # Считается на сервере, чтобы каждый список не повторял эту логику.
        "counterparty": call.from_number if call.direction == "in" else call.to_number,
        "started_at": _iso(call.started_at),
        # null — длительность неизвестна, это не то же самое, что 0 секунд
        "duration_sec": call.duration_sec,
        # null — звонок ещё идёт
        "outcome": call.outcome,
        "has_recording": bool(call.recording_path),
        "client_id": call.client_id,
        "deal_id": call.deal_id,
        "user_id": call.user_id,
        "note_id": call.note_id,
        "created_at": _iso(call.created_at),
    }


def file_out(file: ClientFile) -> dict:
    return {
        "id": file.id,
        "client_id": file.client_id,
        "uploaded_by": file.uploaded_by,
        "original_name": file.original_name,
        "mime": file.mime,
        "size_bytes": file.size_bytes,
        "created_at": _iso(file.created_at),
        "download_url": f"/api/v1/clients/{file.client_id}/files/{file.id}/download",
    }


def board_out(
    board: Board, works_count: int | None = None, client_name: str | None = None
) -> dict:
    """`client_name` — подписью «доска для такого-то».

    Ключ есть всегда, даже когда имени нет: форма ответа не должна зависеть от
    того, кто и как спрашивает. Разная форма у GET и PATCH здесь уже ломала
    экраны — редактор клал ответ PATCH в состояние, и подпись пропадала до
    перезагрузки страницы.
    """
    data = {
        "id": board.id,
        "title": board.title,
        "description": board.description,
        "client_id": board.client_id,
        "client_name": client_name,
        "deal_id": board.deal_id,
        "cover_work_id": board.cover_work_id,
        "created_by": board.created_by,
        "is_published": board.is_published,
        "created_at": _iso(board.created_at),
        "updated_at": _iso(board.updated_at),
    }
    if works_count is not None:
        data["works_count"] = works_count
    return data


def work_out(work: Work, public: bool = False) -> dict:
    """Работа для ответа. `public=True` — то, что уходит клиенту студии.

    Клиенту не отдаётся то, что нужно только редактору: исходное имя файла, его
    размер, порядок в списке и состояние обработки. Имя файла на диске студии —
    это обычно фамилия клиента, сумма или внутренняя пометка
    («Иванов_Смета_180000», «правки_после_скандала_v3»); менеджер переименовывает
    видимую подпись и не подозревает, что рядом уезжает исходное имя. Так и было:
    витрина встраивала эти поля в HTML страницы целиком.
    """
    visible = {
        "id": work.id,
        "board_id": work.board_id,
        "kind": work.kind,
        "title": work.title,
        "description": work.description,
        "project_url": work.project_url or "",
        "sort_order": work.sort_order,
        "status": work.status,
        "original_name": work.original_name,
        "mime": work.mime,
        "size_bytes": work.size_bytes,
        "width": work.width,
        "height": work.height,
        "duration_sec": work.duration_sec,
        "blurhash": work.blurhash,
        # видимый фрагмент длинной работы: null — от верха
        "preview_focus": work.preview_focus,
        "created_at": _iso(work.created_at),
        "media": media_service.work_media_urls(work) if work.status == "ready" else None,
        # кандидаты по ширине для плитки витрины (см. media_service.work_srcset)
        "srcset": media_service.work_srcset(work) if work.status == "ready" else "",
        # Откуда сотруднику забрать ИСХОДНИК. Ссылка отдаётся у любой работы, в
        # том числе необработанной: исходник ложится на диск при загрузке, а
        # `ready` говорит лишь о превью — и работа, у которой обработка
        # отвалилась, как раз та, которую иначе не забрать никак.
        #
        # На витрину не попадает: ниже стоит список РАЗРЕШЁННОГО, и новое поле
        # само по себе наружу не уходит. Это ровно тот случай, ради которого
        # список и заведён.
        "download_url": f"/api/v1/boards/{work.board_id}/works/{work.id}/download",
    }
    if not public:
        return visible
    #: Что уходит наружу. Список разрешённого, а не запрещённого: новое поле в
    #: `work_out` не должно попадать на публичную страницу само по себе.
    return {
        key: visible[key]
        for key in ("kind", "title", "description", "project_url", "width", "height",
                    "duration_sec", "blurhash", "preview_focus", "media", "srcset")
    }


def share_out(link: ShareLink, stats: dict | None = None) -> dict:
    from core.services.share_service import public_url

    data = {
        "id": link.id,
        "board_id": link.board_id,
        "token": link.token,
        "url": public_url(link),
        "is_active": link.is_active,
        "expires_at": _iso(link.expires_at),
        "has_pin": link.pin_hash is not None,
        "created_by": link.created_by,
        "created_at": _iso(link.created_at),
        "revoked_at": _iso(link.revoked_at),
    }
    if stats:
        data.update(stats)
    return data


def view_out(view: ShareView) -> dict:
    return {
        "id": view.id,
        "viewed_at": _iso(view.viewed_at),
        "visitor": view.ip_hash[:12],  # анонимный идентификатор посетителя
        "user_agent": view.user_agent,
    }


def mail_account_out(account: MailAccount) -> dict:
    """Ящик наружу. Пароля здесь нет и быть не может — только признак «задан».

    Единственная сериализация MailAccount в проекте: поле, не перечисленное
    здесь, наружу не уедет ни списком, ни карточкой. Поэтому словарь собирается
    руками, а не отдаётся модель через `from_attributes`, где новая колонка
    попала бы в ответ сама — а следующей такой колонкой может оказаться секрет.
    """
    return {
        "id": account.id,
        "title": account.title,
        "address": account.address,
        "imap_host": account.imap_host,
        "imap_port": account.imap_port,
        "imap_ssl": account.imap_ssl,
        "smtp_host": account.smtp_host,
        "smtp_port": account.smtp_port,
        "smtp_ssl": account.smtp_ssl,
        "login": account.login,
        "has_password": account.password_encrypted is not None,
        "is_active": account.is_active,
        "last_sync_at": _iso(account.last_sync_at),
        "last_error": account.last_error,
        "last_error_at": _iso(account.last_error_at),
        "created_at": _iso(account.created_at),
        "updated_at": _iso(account.updated_at),
    }


def mail_message_out(message: MailMessage, with_body: bool = False) -> dict:
    """Письмо. Тела отдаются только в карточке: в списке они не нужны и весят."""
    data = {
        "id": message.id,
        "account_id": message.account_id,
        "message_id": message.message_id,
        "direction": message.direction,
        "subject": message.subject,
        "from_addr": message.from_addr,
        "to_addrs": [a for a in (x.strip() for x in message.to_addrs.split(",")) if a],
        "sent_at": _iso(message.sent_at),
        "has_attachments": message.has_attachments,
        "client_id": message.client_id,
        "deal_id": message.deal_id,
        "is_read": message.is_read,
        "created_at": _iso(message.created_at),
    }
    # Кому сервер отказал при отправке. Не колонка, а след одного запроса:
    # `send_message` вешает его на объект, когда часть адресатов отвергнута.
    # Хранить незачем — беда уже записана в ящик и в журнал; здесь она нужна
    # ровно тому, кто нажал «отправить», и ровно сейчас.
    refused = getattr(message, "refused", None)
    if refused:
        data["refused"] = list(refused)
    if with_body:
        data["body_text"] = message.body_text
        data["body_html"] = message.body_html
    return data
def product_out(product: Product, stock_milli: int | None, amounts: bool = True) -> dict:
    """Карточка товара. Остаток приходит аргументом — его считает репозиторий запросом.

    Сериализатор не ходит за остатком сам не из принципа: в списке товаров это
    означало бы отдельный запрос на каждую строку, и склад на 500 позиций
    открывался бы секундами.

    `amounts=False` — нет права `warehouse.view_amounts`: закупочная и продажная
    цены уходят, остаток остаётся. Это и есть смысл отдельного права на деньги:
    кладовщик обязан видеть, сколько на полке, и не обязан знать, почём брали.
    """
    data = _product_fields(product, stock_milli)
    if not amounts:
        data["price"] = None
        data["cost"] = None
    return data


def _product_fields(product: Product, stock_milli: int | None) -> dict:
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "unit": product.unit,
        "price": product.price_minor,
        "cost": product.cost_minor,
        "is_service": product.is_service,
        "min_stock_milli": product.min_stock_milli,
        "note": product.note,
        "site_description": product.site_description,
        # У услуги остатка нет — именно null, а не 0: «остатка не бывает» и
        # «товар закончился» на экране должны выглядеть по-разному.
        "stock_milli": stock_milli,
        "low_stock": warehouse_service.is_low(product, stock_milli),
        "created_at": _iso(product.created_at),
        "updated_at": _iso(product.updated_at),
        "deleted_at": _iso(product.deleted_at),
    }


def stock_move_out(move: StockMove, amounts: bool = True) -> dict:
    return {
        "id": move.id,
        "product_id": move.product_id,
        # Знаковое: приход +, расход −. Отдаём как есть, чтобы клиенту не
        # приходилось выводить знак из вида движения второй раз.
        "quantity_milli": move.quantity_milli,
        "kind": move.kind,
        "deal_id": move.deal_id,
        "warehouse_id": move.warehouse_id,
        # Каким бланком вызвано: отгрузкой заказа, приёмкой, проведением акта.
        # Без этого экран не отличит списание по заказу от ручного, а отмена не
        # покажет, что именно откатывала.
        "document_id": move.document_id,
        # Не NULL — значит это половина переезда, а не самостоятельное движение.
        # Экран рисует такие строки стрелкой между складами, а не «приход/расход».
        "transfer_id": move.transfer_id,
        "cost": move.cost_minor if amounts else None,
        "comment": move.comment,
        "happened_at": _iso(move.happened_at),
        "created_at": _iso(move.created_at),
        "author_id": move.author_id,
    }


def media_file_out(item: dict) -> dict:
    """Строка менеджера файлов: даты — в ISO, остальное как есть."""
    return {
        **item,
        "created_at": _iso(item.get("created_at")),
        "last_viewed_at": _iso(item.get("last_viewed_at")),
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def paginated(items: list[dict], total: int, page: int, per_page: int) -> dict:
    return {"items": items, "total": total, "page": page, "per_page": per_page}


def barcode_out(barcode) -> dict:
    """Штрихкод товара для ответа.

    `pack_size_milli` отдаётся как есть, в тысячных: приводить к «штукам» на
    сервере значило бы делить целое на 1000 и однажды получить 0.999 у товара,
    который меряют в граммах. Делит тот, кто показывает.
    """
    return {
        "id": barcode.id,
        "product_id": barcode.product_id,
        "code": barcode.code,
        "kind": barcode.kind,
        "pack_size_milli": barcode.pack_size_milli,
        "is_primary": barcode.is_primary,
    }


def warehouse_out(warehouse) -> dict:
    return {
        "id": warehouse.id,
        "name": warehouse.name,
        "code": warehouse.code,
        "address": warehouse.address,
        "is_default": warehouse.is_default,
        "kind": warehouse.kind,
        "note": warehouse.note,
        "created_at": _iso(warehouse.created_at),
        "deleted_at": _iso(warehouse.deleted_at),
    }


def transfer_out(
    header, moves: list | None = None, names: dict | None = None, reverted: bool = False
) -> dict:
    """Переезд для журнала: шапка плюс строки.

    Строки приходят готовыми, а не догружаются здесь: журнал показывает их
    пачкой, и запрос на строку превратил бы страницу из пятидесяти переездов в
    пятьдесят обращений к базе.
    """
    names = names or {}
    rows = [
        {
            "product_id": move.product_id,
            "quantity_milli": abs(move.quantity_milli),
        }
        for move in (moves or [])
        # Строк у переезда две на позицию, а в журнале нужна одна: берём
        # расходную — она называет и товар, и сколько уехало.
        if move.quantity_milli < 0
    ]
    return {
        "id": header.id,
        "from_warehouse_id": header.from_warehouse_id,
        "from_warehouse_name": names.get(header.from_warehouse_id),
        "to_warehouse_id": header.to_warehouse_id,
        "to_warehouse_name": names.get(header.to_warehouse_id),
        "comment": header.comment,
        "reverses_id": header.reverses_id,
        # Уже отменён — кнопку «отменить» показывать не за чем: сервер откажет,
        # и это будет правильный отказ на неправильное предложение.
        "reverted": reverted,
        "happened_at": _iso(header.happened_at),
        "author_id": header.author_id,
        "items": rows,
    }


def order_line_out(line, amounts: bool = True) -> dict:
    """Строка заказа. Деньги закрываются тем же правом, что и в списке товаров."""
    return {
        "id": line.id,
        "document_id": line.document_id,
        "product_id": line.product_id,
        # Название — снимок на момент добавления: товар переименуют, а в заказе
        # клиента должно остаться то, что он заказывал.
        "name": line.name_snapshot,
        "quantity_milli": line.quantity_milli,
        # Собранное отдельно от заказанного: расхождение видно построчно ДО
        # отгрузки, а не на выдаче.
        "picked_milli": line.picked_milli,
        "price": line.price_minor if amounts else None,
        "cost": line.cost_minor if amounts else None,
        "sort_order": line.sort_order,
    }


def act_out(act, lines: list | None = None, amounts: bool = True) -> dict:
    """Акт выполненных работ вместе со строками.

    Отдельно от `order_out`, хотя половина полей совпадает: у акта нет ни
    собранного сканером, ни вида заказа, зато есть `next_stage` — то, куда он
    переводит заявку, — и себестоимость. Общая функция с ветвлением по виду
    отдавала бы каждому экрану половину чужих полей и заставляла бы обоих
    угадывать, какие из них сегодня заполнены.

    Ни сумма, ни себестоимость нигде не хранятся: обе считаются по строкам при
    каждом ответе (`document_service.total_minor`, `act_service.cost_minor`).
    Третье число в базе разошлось бы с ними при первой же правке строки.

    `amounts=False` гасит деньги, оставляя перечень работ. Отдельного права на
    суммы у бланков сегодня нет — акт видит тот, кто видит раздел; аргумент
    стоит здесь, чтобы место, где деньги вычёркиваются, было одно и заранее
    известное, как у заказа и у товара.
    """
    from core.services import act_service, document_service

    rows = lines or []
    return {
        "id": act.id,
        "number": act.number,
        "kind": act.kind,
        "status": act.status,
        "client_id": act.client_id,
        "deal_id": act.deal_id,
        "locale": act.locale,
        # Куда акт переводит заявку. Пусто — «решим при проведении»: тогда
        # берётся следующий этап воронки.
        "next_stage": act.next_stage,
        "payload": _payload_of(act),
        "lines": [order_line_out(line, amounts=amounts) for line in rows],
        "total": document_service.total_minor(rows) if amounts else None,
        # null — себестоимость неизвестна ни по одной строке: акт ещё не
        # проведён либо склад выключен. Это не то же самое, что ноль.
        "cost": act_service.cost_minor(rows) if amounts else None,
        "created_at": _iso(act.created_at),
        "updated_at": _iso(act.updated_at),
    }


def waybill_out(waybill, lines: list | None = None, amounts: bool = True) -> dict:
    """Накладная вместе со строками.

    Отдельно от `order_out`, хотя половина полей совпадает, — по тому же доводу,
    по которому отдельно живёт `act_out`. У накладной нет собранного сканером
    (её собирают, но `picked_milli` про заказ), зато есть три поля, которых нет
    ни у кого: склад, основание и признак «правится ли ещё».

    `pravitsya` считается здесь, а не догадывается экраном по статусу. Правило
    «черновик правится, проведённая нет» живёт в службе
    (`waybill_service._tolko_chernovik`), и экран, вычисляющий его заново,
    завёл бы второй ответ на тот же вопрос. Разойдутся они в тот день, когда в
    службе появится ещё одно условие.
    """
    from core.services import waybill_service

    rows = lines or []
    return {
        "id": waybill.id,
        "number": waybill.number,
        "kind": waybill.kind,
        "status": waybill.status,
        "client_id": waybill.client_id,
        "deal_id": waybill.deal_id,
        "basis_id": waybill.basis_id,
        "warehouse_id": waybill.warehouse_id,
        "locale": waybill.locale,
        "pravitsya": waybill.status == waybill_service.STATUS_DRAFT,
        "lines": [order_line_out(line, amounts=amounts) for line in rows],
        "total": waybill_service.total_minor(rows) if amounts else None,
        "created_at": _iso(waybill.created_at),
        "updated_at": _iso(waybill.updated_at),
    }


def order_out(order, lines: list | None = None, amounts: bool = True) -> dict:
    """Заказ вместе со строками.

    Строки приходят готовыми, а не догружаются здесь: список показывает их
    пачкой, и запрос на строку превратил бы страницу из пятидесяти заказов в
    пятьдесят обращений к базе.
    """
    from core.services import order_service

    rows = lines or []
    return {
        "id": order.id,
        "number": order.number,
        "assembled": order_service.sobran_po_strokam(rows),
        "kind": order.kind,
        "status": order.status,
        "client_id": order.client_id,
        "deal_id": order.deal_id,
        "locale": order.locale,
        "lines": [order_line_out(line, amounts=amounts) for line in rows],
        "total": order_service.total_minor(rows) if amounts else None,
        # Бронь с сайта: срок и истёк ли он. Истёкшая бронь товар не держит, а
        # заказ остаётся открытым — менеджер обязан это видеть, иначе решит, что
        # товар отложен, и обидит человека у стойки.
        "site_ref": order.site_ref,
        "reserved_until": _iso(order.reserved_until),
        "reserve_expired": bool(
            order.reserved_until
            and order.reserved_until <= now_utc().replace(tzinfo=None)
            and order.status in ("issued", "ready")
        ),
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
    }
