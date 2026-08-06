"""Кто что сделал: единственная точка записи в журнал.

Журнал пополняется отсюда и больше ниоткуда. Не из аккуратности, а потому что
инвариант «пустой исполнитель бывает только у вебхука и синхронизации почты»
надо где-то проверять, и проверка должна стоять на пути, а не рядом с ним:
десять `db.add(AuditEvent(...))` по сервисам означали бы десять мест, где
исполнителя можно тихо не указать.

**Пишется в той же транзакции, что и само изменение.** Поэтому `record` вызывают
сервисы, а не роуты и не наблюдатели событий: у роута изменение уже состоялось,
а наблюдатель работает под точкой отката и своё падение проглатывает (см.
`core/events._run_observer`) — журнал, теряющийся молча, хуже отсутствующего.
Упало `record` — не состоялось ничего, и это правильный исход: изменение без
записи о нём ничем не отличается от изменения, которого не было.

**Что пишем:** изменение денег, смена этапа, удаление чего угодно, выдача и
снятие прав, переключение блоков, движение по складу.

**Что не пишем:** открытие экранов, чтение списков, вход в систему, создание и
правку названий. Это другой объём (на порядки больше строк) и другая задача:
журнал отвечает на вопрос «что изменилось и с чьей руки», а не «кто куда
заходил».
"""

from sqlalchemy.orm import Session

from database.models import User
from database.models.audit import (
    FACELESS_SOURCES,
    SOURCE_MANUAL,
    SOURCES,
    AuditEvent,
)

__all__ = [
    "SOURCE_MANUAL",
    "SOURCES",
    "FACELESS_SOURCES",
    "assert_actor",
    "record",
    "record_deletion",
    "record_restore",
    "permissions_text",
    "money_text",
]

# --- что сделали ---
#
# Строки вида «объект.действие». Список открытый: интерфейс, не знающий действия,
# показывает его ключ как есть, а не падает, — иначе новое действие в сервисе
# требовало бы правки фронта в том же коммите, и его просто не стали бы писать.

ACTION_DEAL_STAGE_CHANGED = "deal.stage_changed"
ACTION_DEAL_AMOUNT_CHANGED = "deal.amount_changed"
ACTION_DEAL_PREPAID_CHANGED = "deal.prepaid_changed"
ACTION_STOCK_MOVE_ADDED = "stock.move_added"
ACTION_MODULE_SWITCHED = "module.switched"
#: Окончательная уборка мягко удалённого. Единственное действие, которое
#: стирает данные безвозвратно, — и потому обязано быть в журнале даже
#: тогда, когда стирать оказалось нечего.
ACTION_STORAGE_PURGED = "storage.purged"
#: Должность собрана, переписана или отдана человеку.
#:
#: Права — это и есть то, ради чего журнал заведён: вопрос «кто дал бухгалтеру
#: доступ к отчётам» задают ровно тогда, когда отвечать уже некому. До этих
#: трёх записей конструктор доступов не оставлял следа вообще: в журнале была
#: только смена «root ↔ менеджер», то есть единственная дверь из двух десятков.
ACTION_ROLE_CREATED = "role.created"
ACTION_ROLE_PERMISSIONS_CHANGED = "role.permissions_changed"
ACTION_ROLE_ASSIGNED = "role.assigned"

ACTION_STAFF_ROLE_CHANGED = "staff.role_changed"
ACTION_STAFF_APPROVED = "staff.approved"
ACTION_STAFF_REJECTED = "staff.rejected"
ACTION_STAFF_DISABLED = "staff.disabled"
ACTION_STAFF_ENABLED = "staff.enabled"
ACTION_STAFF_PASSWORD_RESET = "staff.password_reset"

# --- над чем ---

ENTITY_DEAL = "deal"
ENTITY_CLIENT = "client"
# Движение по складу записывается на ТОВАР, а не на само движение: спрашивают
# «что было с этой матрицей», а не «что было с движением №4117».
ENTITY_PRODUCT = "product"
ENTITY_USER = "user"
ENTITY_MODULE = "module"
#: Должность из конструктора доступов (не «root/менеджер» — тот у ENTITY_USER).
ENTITY_ROLE = "role"
#: Своё юрлицо: реквизиты, печать, подпись — то, что стоит на бумаге у клиента.
ENTITY_COMPANY = "company"
#: Доска работ и ссылка на неё: то, что видит клиент студии снаружи.
ENTITY_BOARD = "board"
ENTITY_SHARE = "share"
#: Почтовый ящик: чужой сервер, логин и пароль.
ENTITY_MAILBOX = "mailbox"
ENTITY_NOTE = "note"
ENTITY_FILE = "file"

MAX_LABEL = 200
MAX_SOURCE_REF = 64


def assert_actor(actor: User | None, source: str, what: str) -> None:
    """Проверить, что исполнителя можно не указывать.

    Отдельная функция, а не проверка внутри `record`, потому что запись в
    журнал — не единственное место, где появляется автор: у записи в ленте он
    тоже свой, и обезличить её так же легко. Правило одно, и оно должно быть
    записано один раз.

    Отказ — обычное исключение, а не доменная ошибка: это ошибка программиста, и
    падать она обязана громко, а не превращаться в 422 для пользователя.
    """
    if source not in SOURCES:
        raise ValueError(f"Unknown audit source: {source!r}")
    if actor is None and source not in FACELESS_SOURCES:
        raise ValueError(
            f"{what} from source {source!r} has no actor. Пустой исполнитель "
            "бывает только у вебхука АТС и синхронизации почты — там человека "
            "действительно нет. Протащите исполнителя."
        )


def record(
    db: Session,
    *,
    action: str,
    actor: User | None,
    source: str,
    entity_type: str,
    entity_id: int | None = None,
    entity_label: str = "",
    source_ref: str = "",
    before: str | None = None,
    after: str | None = None,
) -> AuditEvent:
    """Записать действие в журнал.

    `source` — обязательный именованный аргумент без умолчания. Умолчание здесь
    было бы «рука», и любое автоматическое списание, о котором забыли, выглядело
    бы в журнале как сделанное вручную — то есть журнал врал бы именно в том
    случае, ради которого его читают.
    """
    assert_actor(actor, source, f"Audit entry {action!r}")

    entry = AuditEvent(
        action=action,
        actor_id=actor.id if actor else None,
        actor_name=(actor.name if actor else "")[:120],
        source=source,
        source_ref=(source_ref or "")[:MAX_SOURCE_REF],
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=(entity_label or "")[:MAX_LABEL],
        value_before=before,
        value_after=after,
    )
    db.add(entry)
    # flush, а не ожидание общего: запись обязана дойти до базы вместе с самим
    # изменением, а не после — иначе нарушенное ограничение всплывёт на выходе
    # из запроса, когда объяснить его будет уже нечем.
    db.flush()
    return entry


def record_deletion(
    db: Session,
    *,
    actor: User | None,
    entity_type: str,
    entity_id: int | None,
    entity_label: str,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> AuditEvent:
    """Удаление чего угодно — одной формы записью.

    Отдельный вход, потому что у удаления нет ни «было», ни «стало»: величина
    не менялась, исчез сам объект. «Было» здесь — это его название, и оно уже
    лежит в `entity_label`; дублировать его в `value_before` значило бы
    показывать одну и ту же строку дважды в соседних колонках.

    Снимок названия обязателен и делается до удаления: спросить его потом будет
    не у кого, а «client 5 удалён» не отвечает на вопрос «кого».
    """
    if not entity_label:
        raise ValueError(
            f"Deletion of {entity_type}:{entity_id} recorded without a label — "
            "спросить название после удаления будет не у кого."
        )
    return record(
        db,
        action=f"{entity_type}.deleted",
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
    )


def record_restore(
    db: Session,
    *,
    actor: User | None,
    entity_type: str,
    entity_id: int | None,
    entity_label: str,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> AuditEvent:
    """Возврат из корзины — записью той же формы, что и удаление.

    Без неё журнал врал умолчанием: «удалил клиента» в нём было, а того, что
    клиента вернули через час, — нет. Читающий через месяц видит одно удаление
    и делает вывод, что карточки больше нет; вывод неверный, а спорить не с чем.

    Пары «было/стало» здесь нет по той же причине, что и у удаления: менялось не
    значение, а само наличие записи.
    """
    return record(
        db,
        action=f"{entity_type}.restored",
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
    )


def permissions_text(codes) -> str:
    """Набор прав в журнал — списком кодов через запятую.

    Именно коды, а не подписи: подписи живут в словаре интерфейса и меняются, а
    `deals.view_amounts` через год читается так же, как сегодня. По той же
    причине здесь не форматируются и деньги (см. `money_text`).

    Порядок задан сортировкой: две записи об одном и том же наборе обязаны
    выглядеть одинаково, иначе «изменил права» покажется правкой там, где просто
    другой порядок в запросе.
    """
    return ", ".join(sorted(codes))


def money_text(minor: int | None) -> str | None:
    """Деньги в журнал — теми же целыми в минорных единицах, что и в базе.

    Не форматируем и не приписываем валюту: форматирование зависит от локали
    читающего и от настройки валюты, а обе могут смениться. Строка «5000» через
    год читается так же, «5 000,00 ₽» — уже нет.
    """
    return None if minor is None else str(minor)
