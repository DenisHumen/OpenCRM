"""Журнал действий: кто, чем это вызвано и что именно поменялось.

Три вещи вместо одной, и каждая отвечает на свой вопрос:

* ``actor_id`` — **кто** сделал. Всегда живой человек, и он протаскивается по
  всей цепочке: провёл акт Иванов — значит и движение по складу, и смена этапа,
  и запись в ленте принадлежат Иванову, а не «системе».
* ``source`` — **чем вызвано**: рука, вебхук АТС, синхронизация почты. Без него
  в журнале не отличить «Иванов списал руками» от «списалось, потому что Иванов
  провёл акт», а разбираться будут именно в этом.
* ``source_ref`` — **чем именно**: номер того самого акта.

Почему отдельная таблица, а не поля в существующих. Исполнитель уже пишется в
``deal_stage_changes``, ``stock_moves.author_id``, ``client_notes.author_id``,
``module_states.updated_by`` — но это рабочие данные своих блоков, а не журнал:
по ним считается «сколько сделка простояла в этапе» и остаток склада, состав их
колонок диктует задача блока, и ``source`` там взяться неоткуда. Чтобы ответить
на «кто списал две матрицы», пришлось бы обойти десять таблиц с разными именами
колонок и сложить их вручную. Журнал не заменяет эти записи и не отменяет их —
он собирает свои, одной формы и в одном месте.

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

SOURCES = (SOURCE_MANUAL, SOURCE_TELEPHONY_WEBHOOK, SOURCE_MAIL_SYNC)

#: Источники, у которых исполнителя действительно нет.
#:
#: Список закрытый и короткий намеренно. Разреши пустого исполнителя вообще —
#: и «пусто» станет удобной заглушкой везде, где протащить человека оказалось
#: лень, а журнал перестанет отвечать на единственный вопрос, ради которого он
#: заведён.
FACELESS_SOURCES = (SOURCE_TELEPHONY_WEBHOOK, SOURCE_MAIL_SYNC)


class AuditEvent(Base):
    """Одна запись журнала.

    Пишется в той же транзакции, что и само изменение. Запись постфактум
    разойдётся с действительностью ровно тогда, когда операция упала на
    середине, — то есть когда журнал и нужен.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Что сделали: `deal.stage_changed`, `client.deleted`, `module.switched`.
    #: Строка вида «объект.действие», а не число: журнал читают люди, в том
    #: числе прямым запросом к базе, когда интерфейс недоступен.
    action: Mapped[str] = mapped_column(String(48))

    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Имя исполнителя на момент действия. Не роскошь и не денормализация ради
    #: скорости: внешний ключ обнуляется при удалении сотрудника (SET NULL, как
    #: везде в проекте), и журнал за прошлый год остался бы без имён ровно тогда,
    #: когда человек уволился, — то есть когда в него и приходят. Заодно
    #: переживает переименование: «было 5 000, стало 500, сделал Петров» должно
    #: читаться так, как читалось в день записи.
    actor_name: Mapped[str] = mapped_column(String(120), default="")

    source: Mapped[str] = mapped_column(String(32))
    #: Чем именно: номер акта, идентификатор события АТС. Пусто у ручного
    #: действия — ссылаться там не на что, кроме самого объекта.
    source_ref: Mapped[str] = mapped_column(String(64), default="")

    #: Над чем совершено действие: `deal`, `client`, `product`, `user`, `module`.
    entity_type: Mapped[str] = mapped_column(String(32))
    #: Идентификатор объекта. **Не** внешний ключ, и это осознанно: журнал обязан
    #: пережить удаление того, о чём он написан. CASCADE снёс бы запись об
    #: удалении вместе с удалённым, RESTRICT запретил бы удалять что-либо вовсе.
    #: У модулей идентификатор строковый — тогда здесь NULL, а ключ в `entity_label`.
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Снимок названия: «Иванов», «Матрица 15.6"», «warehouse». По той же
    #: причине, что и `actor_name`: «client 5 удалён» не отвечает на вопрос
    #: «кого удалили», а спросить больше не у кого — карточки уже нет.
    entity_label: Mapped[str] = mapped_column(String(200), default="")

    #: Было и стало. NULL и пустая строка различаются намеренно: NULL значит
    #: «значения не было» (сумму ещё не назвали, объекта ещё нет), пустая строка
    #: — «значение было пустым». Для действий без величины (удаление, выдача
    #: прав) оба NULL.
    value_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_after: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    # Журнал растёт быстрее всех остальных таблиц: он пополняется на каждое
    # значимое действие в каждом блоке. Оба составных индекса — под те два
    # вопроса, ради которых в журнал заходят.
    __table_args__ = (
        # «Что делали с этой заявкой»: сначала объект, потом время внутри него.
        Index("ix_audit_events_entity", "entity_type", "entity_id", "created_at"),
        # «Что делал этот сотрудник». Он же закрывает внешний ключ на users.
        Index("ix_audit_events_actor", "actor_id", "created_at"),
    )


# --- журнал только дописывается ---
#
# Запрет стоит на мэппере, а не в сервисе: сервис можно обойти, вызвав
# `db.delete(entry)` из соседнего модуля или из скрипта обслуживания, — и
# обойдут его не злонамеренно, а по невнимательности, при разборе как раз того
# случая, ради которого журнал вёлся. Образец такого запрета в проекте уже есть
# (`client_service.delete_note` для системных записей ленты), здесь он строже:
# у записи журнала нет ни одного законного основания измениться.
#
# Граница честная: прямой SQL мимо ORM это не закрывает. От того, у кого есть
# доступ к файлу базы, журнал не защищён ничем — как и всё остальное в системе.

_APPEND_ONLY = "The audit log is append-only"


@event.listens_for(AuditEvent, "before_update", propagate=True)
def _no_edits(mapper, connection, target) -> None:
    raise errors.ForbiddenError(_APPEND_ONLY, code="audit_append_only")


@event.listens_for(AuditEvent, "before_delete", propagate=True)
def _no_deletes(mapper, connection, target) -> None:
    raise errors.ForbiddenError(_APPEND_ONLY, code="audit_append_only")


# Две проверки выше стоят на **объекте**: их зовёт flush, разбирая изменённые и
# удалённые экземпляры. Массовая операция объектов не трогает вовсе —
# `session.execute(update(AuditEvent).values(...))` и `delete(AuditEvent)`
# уезжают в базу одним запросом, мимо мэппера и мимо обоих запретов. То есть
# журнал переписывался молча: одной строкой из соседнего сервиса или из скрипта
# обслуживания, и ровно тем человеком, которому ответ «кто это сделал» невыгоден.
#
# Поэтому вторая граница — на **сессии**. `do_orm_execute` видит всё, что уходит
# через `Session.execute`, до отправки в базу; здесь же ловится и Core-запрос по
# самой таблице (`update(AuditEvent.__table__)`), потому что смотрим на таблицу,
# а не на модель. Чтения не касаемся: журнал читают, и читать его надо.
#
# Что этим по-прежнему не закрыто — то же, что и раньше: `text("UPDATE ...")` и
# любой клиент базы снаружи. Граница проходит по ORM, и это сказано честно.


@event.listens_for(Session, "do_orm_execute")
def _no_bulk_edits(state) -> None:
    if not (state.is_update or state.is_delete):
        return
    target = getattr(state.statement, "table", None)
    if getattr(target, "name", None) == AuditEvent.__tablename__:
        raise errors.ForbiddenError(_APPEND_ONLY, code="audit_append_only")
