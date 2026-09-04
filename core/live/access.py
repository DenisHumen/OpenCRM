"""Кому полагается намёк. Три вопроса в жёстком порядке, ни одного своего запроса.

Блок включён → есть право `view` → строка видна. Порядок тот же, что у
`require_perm`, и по той же причине: перестановка даёт неверную причину
отказа, а через год — неверный доступ. Права не кэшируются: у долгоживущего
соединения «прочитали при подключении» значило бы, что уволенный получает
поток до пересборки соединения (`docs/12-realtime.md` §7).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.live import topics
from core.live.message import Hint
from core.services import modules_service, permissions_service
from database.models import User


def delivers(db: Session, user: User, hint: Hint) -> bool:
    tema = topics.BY_NAME.get(hint.topic)
    if tema is None:
        return False
    if tema.module is not None and not modules_service.is_enabled(db, tema.module):
        return False
    if tema.area is not None and not permissions_service.has(db, user, tema.area, "view"):
        return False
    if tema.scope == topics.BY_MANAGER:
        svoi = permissions_service.deals_scope(db, user)
        if svoi is not None:
            # Ответственный неизвестен (строка заявки, история этапа) — только
            # тем, кто видит все: сузить нечем, а лишний намёк — утечка.
            if hint.scope_key is None or hint.scope_key != svoi:
                return False
    return True
