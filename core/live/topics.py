"""Карта «модель → тема» и «событие → тема». Ответ обязателен, даже «ничего».

Полнота по построению (`docs/12-realtime.md` §9): каждая модель из
`database/models` названа здесь либо темой, либо `None` с доводом. Сторож —
`tests/test_realtime.py`: новая модель без строки роняет набор, а не молчит.

У темы три поля отбора: блок (выключен — не подписан никто), область прав
(`view`) и способ сужения — всем, кто видит раздел, или по ответственному.
Порядок проверки — блок, потом право, как в `require_perm`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from database.models import (
    ApiKey,
    ApiKeyHit,
    ApiKeyScope,
    Board,
    Client,
    ClientFile,
    ClientNote,
    Company,
    Deal,
    DealLine,
    DealStageChange,
    Document,
    DocumentEvent,
    DocumentFile,
    DocumentLine,
    FinanceBudget,
    FinanceCategory,
    FinanceOperation,
    FinanceRule,
    MailAccount,
    MailMessage,
    MessageTemplate,
    ModuleState,
    PhoneCall,
    PipelineStage,
    Product,
    ProductBarcode,
    ProductPhoto,
    Role,
    RolePermission,
    ShareLink,
    ShareView,
    SiteSetting,
    SnakeScore,
    StockMove,
    StockTransfer,
    Task,
    TelegramChat,
    TelegramMessage,
    User,
    UserSession,
    Warehouse,
    Work,
)
from database.models.audit import AuditEvent
from database.models.notification import Notification
from database.models.document import KIND_RETURN, ORDER_KINDS, WAYBILL_KINDS

EVERYONE = "everyone"
#: По ответственному: `permissions_service.deals_scope` — `None` (все) или свой id.
BY_MANAGER = "by_manager"
#: Только тому, кому адресовано: `scope_key` — номер сотрудника.
BY_USER = "by_user"


@dataclass(frozen=True)
class Topic:
    name: str
    #: Блок, без которого тема не существует. `None` — вне блоков (реестр, штат).
    module: str | None
    #: Область прав, чьё `view` нужно. `None` — видят все сотрудники.
    area: str | None
    scope: str = EVERYONE
    #: Атрибут записи, из которого берётся `scope_key` (`manager_id`).
    scope_attr: str | None = None
    #: Атрибут записи, из которого берётся номер для намёка: у строки заявки
    #: это сама заявка, у снимка товара — товар. Пусто — свой `id`.
    id_attr: str = "id"
    #: Поля, правка которых намёка не рождает: отметка присутствия сотрудника
    #: пишется каждым сердцебиением, и намёк на неё гнал бы всех на перечитку
    #: штата раз в минуту ради точки «в сети».
    tikhie: frozenset = frozenset()


T_DEALS = Topic("deals", "deals", "deals", BY_MANAGER, "manager_id")
#: Строки заявки и история этапов не знают ответственного без запроса, а
#: запросов у слушателя нет. Отбор консервативный: «ответственный неизвестен»
#: доставляется только тем, кто видит все заявки (`access.delivers`).
T_DEALS_CHILD = Topic("deals", "deals", "deals", BY_MANAGER, None, "deal_id")
T_CLIENTS = Topic("clients", "clients", "clients")
T_CLIENT_FEED = Topic("client_notes", "clients", "clients", id_attr="client_id")
T_CLIENT_FILES = Topic("clients", "clients", "clients", id_attr="client_id")
T_BOARDS = Topic("boards", "boards", "boards")
T_WORKS = Topic("boards", "boards", "boards", id_attr="board_id")
T_COMPANIES = Topic("companies", "companies", "companies")
T_PIPELINE = Topic("pipeline", "deals", "deals")
T_DOCUMENTS = Topic("documents", "documents", "documents")
T_ORDERS = Topic("orders", "orders", "orders")
T_WAYBILLS = Topic("waybills", "waybills", "waybills")
T_TASKS = Topic("tasks", "tasks", "tasks")
T_TEMPLATES = Topic("templates", "templates", "templates")
T_MAIL = Topic("mail", "mail", "mail")
T_WAREHOUSE = Topic("warehouse", "warehouse", "warehouse")
T_PRODUCT_CHILD = Topic("warehouse", "warehouse", "warehouse", id_attr="product_id")
T_PLACES = Topic("warehouses", "warehouse", "warehouse")
T_TELEPHONY = Topic("telephony", "telephony", "telephony")
T_FINANCE = Topic("finance", "finance", "finance")
T_STAFF = Topic("staff", None, "staff", tikhie=frozenset({"last_seen_at"}))
T_ROLES = Topic("roles", None, "roles")
T_ROLE_PERMS = Topic("roles", None, "roles", id_attr="role_id")
T_MODULES = Topic("modules", None, None)
T_API_KEYS = Topic("api_keys", None, "settings")
T_NOTIFICATIONS = Topic("notifications", None, None, BY_USER, "user_id")
T_API_KEY_SCOPES = Topic("api_keys", None, "settings", id_attr="api_key_id")


def _po_vidu_blanka(document) -> Topic:
    """Заказ, накладная и квитанция лежат в одной таблице, а смотрят их разными правами."""
    # Возврат — действие по заказу: смотрят его те же люди и с той же карточки.
    if document.kind in ORDER_KINDS or document.kind == KIND_RETURN:
        return T_ORDERS
    if document.kind in WAYBILL_KINDS:
        return T_WAYBILLS
    return T_DOCUMENTS


#: Значение — тема, функция «запись → тема», либо `None` с доводом рядом.
TOPICS: dict[type, Topic | Callable | None] = {
    Deal: T_DEALS,
    DealLine: T_DEALS_CHILD,
    DealStageChange: T_DEALS_CHILD,
    Client: T_CLIENTS,
    ClientNote: T_CLIENT_FEED,
    ClientFile: T_CLIENT_FILES,
    Board: T_BOARDS,
    Work: T_WORKS,
    ShareLink: T_BOARDS,
    # Просмотры витрины пишутся на каждый заход посетителя — это счётчик, а не
    # то, на что смотрят живьём; сводка перечитывает их сама.
    ShareView: None,
    Company: T_COMPANIES,
    PipelineStage: T_PIPELINE,
    Document: _po_vidu_blanka,
    # Строка и событие бланка: вид бумаги известен только у самого бланка, и он
    # уже в сессии — слушатель берёт его из карты объектов без запроса.
    DocumentLine: "document",
    DocumentEvent: "document",
    DocumentFile: "document",
    Task: T_TASKS,
    MessageTemplate: T_TEMPLATES,
    MailAccount: T_MAIL,
    MailMessage: T_MAIL,
    Product: T_WAREHOUSE,
    ProductBarcode: T_PRODUCT_CHILD,
    ProductPhoto: T_PRODUCT_CHILD,
    StockMove: T_PRODUCT_CHILD,
    StockTransfer: T_WAREHOUSE,
    Warehouse: T_PLACES,
    # У мессенджера свой поток (`core/realtime.py`, `GET /telegram/stream`) со
    # своим догоном из базы по `?after=`; второй канал на те же строки дал бы два
    # способа узнать пропущенное, из которых проверяют один (§6 документа).
    TelegramChat: None,
    TelegramMessage: None,
    PhoneCall: T_TELEPHONY,
    FinanceBudget: T_FINANCE,
    FinanceCategory: T_FINANCE,
    FinanceOperation: T_FINANCE,
    FinanceRule: T_FINANCE,
    User: T_STAFF,
    Notification: T_NOTIFICATIONS,
    # Сессия пишется на каждом запросе (отметка присутствия) — это не изменение
    # данных, а пульс.
    UserSession: None,
    Role: T_ROLES,
    RolePermission: T_ROLE_PERMS,
    ModuleState: T_MODULES,
    # Настройки читает экран настроек у того, кто их и правит; режим
    # обслуживания и без того спрашивается на каждом запросе.
    SiteSetting: None,
    # Журнал читают, а не смотрят живьём.
    AuditEvent: None,
    # Игра.
    SnakeScore: None,
    ApiKey: T_API_KEYS,
    # Счётчик обращений намекает сам, не чаще раза в две секунды на ключ
    # (`api_stats_service`): намёк на каждый запрос сайта гнал бы экран
    # ключей на перечитку сводки по два раза в секунду.
    ApiKeyHit: None,
    ApiKeyScope: T_API_KEY_SCOPES,
}

#: Все темы по имени — для отбора по правам и для проверок.
BY_NAME: dict[str, Topic] = {}
for _znachenie in list(TOPICS.values()) + [T_ORDERS, T_WAYBILLS, T_DOCUMENTS, T_NOTIFICATIONS]:
    if isinstance(_znachenie, Topic):
        BY_NAME.setdefault(_znachenie.name, _znachenie)

#: События `core/events.py`: у каждого либо тема, либо «не транслируется» с доводом.
#: Живой слой не третий подписчик: он видит записи, которые эти события
#: оставили, — карта выше их и подхватит. Здесь только сверка полноты.
EVENT_TOPICS: dict[str, str | None] = {
    "deal.stage_changed": "deals",
    "deal.amount_changed": "deals",
    "deal.prepaid_changed": "deals",
    "stock.move_added": "warehouse",
    "stock.transferred": "warehouse",
    "waybill.reversed": "waybills",
    "order.closed": "orders",
    "deal.lines_changed": "deals",
    "deal.deleted": "deals",
    "order.cancelled": "orders",
    "order.lines_changed": "orders",
    "document.deleted": "documents",
    "return.posted": "orders",
    "act.completed": "documents",
    "lead.received": "deals",
    "stock.written_off": "warehouse",
    "document.issued": "documents",
    "document.closed": "documents",
    "waybill.posted": "waybills",
}
