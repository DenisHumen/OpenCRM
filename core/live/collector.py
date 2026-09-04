"""Намёки копятся в сессии и уходят после фиксации. Откат их выбрасывает.

Почему слушатель сессии, а не строка в конце обработчика (`docs/12-realtime.md`
§4): коммитят не только маршруты — синхронизация почты, вебхук АТС, скрипты,
посев на старте. «Отправить после фиксации», написанное в двухстах местах,
будет забыто в двести первом.

Откуда берутся намёки (§9): из того, что записалось. На сбросе сессии видно,
что добавлено, изменено и удалено; карта `topics.TOPICS` превращает это в намёки.
Явный `announce` остаётся для случаев, где смысл не совпадает со строкой.

Регистрация — из `core/`, чтобы `database/` не узнал про шину: направление
зависимостей остаётся `web → core → database`.
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from core.live import bus, topics
from core.live.message import ACTION_CREATED, ACTION_DELETED, ACTION_UPDATED, Hint
from database.models import Document

logger = logging.getLogger(__name__)

BUFFER = "live_hints"
#: Кто изменил — кладёт `web/api/deps.get_current_user`; у скриптов и вебхуков пусто.
ACTOR = "live_actor_id"


def announce(db: Session, hint: Hint) -> None:
    """Положить намёк в буфер транзакции. Уедет после фиксации, пропадёт при откате."""
    bufer = db.info.setdefault(BUFFER, {})
    byl = bufer.get(hint.key)
    # Созданное и тут же поправленное — одно «создано»: экрану важно, что
    # запись новая, а не сколько раз её трогали до фиксации.
    if byl is not None and byl.action == ACTION_CREATED and hint.action == ACTION_UPDATED:
        return
    bufer[hint.key] = hint


def _tema(session: Session, obj) -> topics.Topic | None:
    znachenie = topics.TOPICS.get(type(obj))
    if znachenie is None:
        return None
    if znachenie == "document":
        # Вид бумаги — у самой бумаги, и она уже в сессии: службы сначала
        # читают бланк, потом правят строки. Из карты объектов, не запросом.
        blank = session.identity_map.get(session.identity_key(Document, (obj.document_id,)))
        if blank is None:
            return topics.T_DOCUMENTS
        return topics._po_vidu_blanka(blank)
    if callable(znachenie):
        return znachenie(obj)
    return znachenie


def _namyok(session: Session, obj, action: str) -> Hint | None:
    tema = _tema(session, obj)
    if tema is None:
        return None
    nomer = getattr(obj, tema.id_attr, None)
    scope_key = getattr(obj, tema.scope_attr, None) if tema.scope_attr else None
    return Hint(
        topic=tema.name,
        action=action,
        id=nomer,
        scope_key=scope_key,
        actor_id=session.info.get(ACTOR),
        module=tema.module,
    )


@event.listens_for(Session, "after_flush")
def _sobrat(session: Session, _flush_context) -> None:
    """Что записалось — то и намёк. Ни одного запроса: только объекты сессии."""
    try:
        for obj in session.new:
            hint = _namyok(session, obj, ACTION_CREATED)
            if hint:
                announce(session, hint)
        for obj in session.dirty:
            if not session.is_modified(obj, include_collections=False):
                continue
            hint = _namyok(session, obj, ACTION_UPDATED)
            if hint:
                announce(session, hint)
        for obj in session.deleted:
            hint = _namyok(session, obj, ACTION_DELETED)
            if hint:
                announce(session, hint)
    except Exception as beda:  # noqa: BLE001 — сбор намёков не имеет права ронять запись
        logger.warning("живые обновления: намёки не собраны — %r", beda)


@event.listens_for(Session, "after_commit")
def _otpravit(session: Session) -> None:
    """Транзакция закрылась — запись в базе есть, теперь можно объявлять."""
    bufer = session.info.pop(BUFFER, None)
    if not bufer:
        return
    for hint in bufer.values():
        try:
            bus.publish(hint)
        except Exception as beda:  # noqa: BLE001 — упавшая отправка не роняет фиксацию
            logger.warning("живые обновления: намёк не отправлен — %r", beda)


@event.listens_for(Session, "after_rollback")
@event.listens_for(Session, "after_soft_rollback")
def _zabyt(session: Session, *_) -> None:
    session.info.pop(BUFFER, None)
