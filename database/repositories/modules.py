"""Запросы по состоянию блоков.

В базе лежит только «включён или нет» и кто последний трогал. Что за блоки
существуют, какие несущие и что от чего зависит, знает `core/modules.py` —
**реестр главнее строки в базе**. Поэтому здесь нет ни списка ключей, ни
проверок: строка от снесённого блока безвредна, потому что её никто не спросит.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import ModuleState


def get(db: Session, key: str) -> ModuleState | None:
    return db.get(ModuleState, key)


def all_rows(db: Session) -> list[ModuleState]:
    return list(db.scalars(select(ModuleState)))


def enabled_map(db: Session) -> dict[str, bool]:
    return {row.key: row.enabled for row in all_rows(db)}
