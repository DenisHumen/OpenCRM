"""Журнал действий: кто, чем это вызвано и что именно поменялось.

Отдельная таблица, а не поля в блоках: исполнитель есть и в ``stock_moves``, и в
``deal_stage_changes``, но ``source`` — «руками» или «потому что провёл акт» —
там взяться неоткуда, и ответ на «кто списал две матрицы» собирался бы обходом
десяти таблиц.

**Дописывается только.** Правка и удаление закрыты для всех, включая root, и
закрыты на уровне ORM (см. конец файла), а не вежливой проверкой в сервисе:
журнал, который можно поправить, ничего не доказывает.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.sql import func

from core import exceptions as errors
from database.session import Base

# --- чем вызвано действие ---

#: Человек нажал в интерфейсе. Умолчание для всего, что приходит через API.
SOURCE_MANUAL = "manual"
#: Событие от АТС. Человека нет — станция шлёт его сама.
SOURCE_TELEPHONY_WEBHOOK = "telephony_webhook"
#: Забор почты по расписанию. Человека нет — письмо пришло само.
SOURCE_MAIL_SYNC = "mail_sync"

#: Запрос по ключу сайта (`docs/ustroystvo/16-api-sayta.md`). Человека нет — за ключом
#: стоит чужая программа.
SOURCE_SITE_API = "site_api"

SOURCES = (SOURCE_MANUAL, SOURCE_TELEPHONY_WEBHOOK, SOURCE_MAIL_SYNC, SOURCE_SITE_API)

#: Источники, у которых исполнителя действительно нет.
#:
#: Список закрытый намеренно: разреши пустого исполнителя вообще — и «пусто»
#: станет заглушкой везде, где протащить человека оказалось лень.
FACELESS_SOURCES = (SOURCE_TELEPHONY_WEBHOOK, SOURCE_MAIL_SYNC, SOURCE_SITE_API)


class AuditEvent(Base):
    """Одна запись журнала.

    Пишется в той же транзакции, что и само изменение: запись постфактум
    разойдётся с действительностью, когда операция упала на середине.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Что сделали: `deal.stage_changed`, `client.deleted`, `module.switched`.
    #: Строка «объект.действие», а не число: журнал читают люди, в том числе
    #: прямым запросом к базе, когда интерфейс недоступен.
    action: Mapped[str] = mapped_column(String(48))

    #: **Кто** сделал. Всегда живой человек, и он протаскивается по всей
    #: цепочке: провёл акт Иванов — значит и движение по складу, и смена этапа,
    #: и запись в ленте принадлежат Иванову, а не «системе».
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Имя исполнителя на момент действия. Внешний ключ обнуляется при удалении
    #: сотрудника (SET NULL), и журнал за прошлый год остался бы без имён ровно
    #: тогда, когда человек уволился. Заодно переживает переименование.
    actor_name: Mapped[str] = mapped_column(String(120), default="")

    source: Mapped[str] = mapped_column(String(32))
    #: Чем именно: номер акта, идентификатор события АТС. Пусто у ручного
    #: действия — ссылаться там не на что, кроме самого объекта.
    source_ref: Mapped[str] = mapped_column(String(64), default="")

    #: Над чем совершено действие: `deal`, `client`, `product`, `user`, `module`.
    entity_type: Mapped[str] = mapped_column(String(32))
    #: Идентификатор объекта. **Не** внешний ключ: журнал обязан пережить
    #: удаление того, о чём написан — CASCADE снёс бы запись об удалении вместе с
    #: удалённым, RESTRICT запретил бы удалять вовсе. У модулей ключ строковый:
    #: тогда здесь NULL, а сам ключ в `entity_label`.
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Снимок названия. По той же причине, что и `actor_name`: «client 5 удалён»
    #: не отвечает на «кого удалили», а спросить больше не у кого.
    entity_label: Mapped[str] = mapped_column(String(200), default="")

    #: Было и стало. NULL и пустая строка различаются намеренно: NULL — «значения
    #: не было», пустая строка — «значение было пустым». Для действий без
    #: величины (удаление, выдача прав) оба NULL.
    value_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_after: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    # Журнал растёт быстрее остальных таблиц. Оба составных индекса — под те два
    # вопроса, ради которых в него заходят.
    __table_args__ = (
        # «Что делали с этой заявкой»: сначала объект, потом время внутри него.
        Index("ix_audit_events_entity", "entity_type", "entity_id", "created_at"),
        # «Что делал этот сотрудник». Он же закрывает внешний ключ на users.
        Index("ix_audit_events_actor", "actor_id", "created_at"),
    )


# --- журнал только дописывается ---
#
# Запрет стоит на мэппере, а не в сервисе: сервис обходят `db.delete(entry)` из
# соседнего модуля или скрипта обслуживания — и обходят не злонамеренно, а при
# разборе того самого случая. Прямой SQL мимо ORM этим, честно, не закрыт.

_APPEND_ONLY = "The audit log is append-only"


@event.listens_for(AuditEvent, "before_update", propagate=True)
def _no_edits(mapper, connection, target) -> None:
    raise errors.ForbiddenError(_APPEND_ONLY, code="audit_append_only")


@event.listens_for(AuditEvent, "before_delete", propagate=True)
def _no_deletes(mapper, connection, target) -> None:
    raise errors.ForbiddenError(_APPEND_ONLY, code="audit_append_only")


# Проверки выше стоят на **объекте**, их зовёт flush, — а массовая операция
# объектов не трогает: `session.execute(update(AuditEvent))` уезжает мимо
# мэппера, и журнал переписывался бы молча одной строкой. Отсюда вторая граница,
# на сессии: смотрим на таблицу, а не на модель, поэтому ловится и Core-запрос.


@event.listens_for(Session, "do_orm_execute")
def _no_bulk_edits(state) -> None:
    if not (state.is_update or state.is_delete):
        return
    target = getattr(state.statement, "table", None)
    if getattr(target, "name", None) == AuditEvent.__tablename__:
        raise errors.ForbiddenError(_APPEND_ONLY, code="audit_append_only")
