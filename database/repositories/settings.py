"""Настройки: пары «ключ — значение» в одной таблице.

Таблица общая, а хозяев у неё двое: общие настройки студии
(`core/services/settings_service.py`) и настройки телефонии, которые живут под
своей приставкой в тех же строках. Оба ходили сюда своими запросами, и один из
них вдобавок собирал `LIKE` вручную по приставке.

Здесь запросы одни на двоих — не ради экономии, а потому что правило хранения у
них общее: **строка появляется при первой записи, а не при установке**. Значит
любая запись это «поправить, если есть, иначе завести», и написать её надо один
раз.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.utils import now_utc
from database.models import SiteSetting


def all_rows(db: Session) -> list[SiteSetting]:
    return list(db.scalars(select(SiteSetting)))


def rows_with_prefix(db: Session, prefix: str) -> list[SiteSetting]:
    """Настройки одного блока — они хранятся под общей приставкой в ключе."""
    return list(
        db.scalars(select(SiteSetting).where(SiteSetting.key.startswith(prefix, autoescape=True)))
    )


def existing_keys(db: Session) -> set[str]:
    return set(db.scalars(select(SiteSetting.key)))


def key_exists(db: Session, key: str) -> bool:
    return db.scalar(select(SiteSetting.id).where(SiteSetting.key == key)) is not None


def get_row(db: Session, key: str) -> SiteSetting | None:
    return db.scalar(select(SiteSetting).where(SiteSetting.key == key))


def write(db: Session, key: str, value: str) -> None:
    """Записать значение: поправить строку или завести её.

    Время правки проставляется здесь, а не в модели: `onupdate` сработал бы и
    тогда, когда значение переписали тем же самым, — а «настройку меняли вчера»
    и «вчера нажали сохранить, ничего не изменив» это разные утверждения.
    """
    row = get_row(db, key)
    if row is None:
        db.add(SiteSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = now_utc()
    db.flush()


def write_many(db: Session, changes: dict[str, str]) -> None:
    """Записать пачку значений одним проходом.

    Строки читаются разом, а не по одной на ключ: настроек в форме два десятка,
    и запрос на каждую превратил бы одно нажатие «Сохранить» в сорок обращений.
    """
    existing = {row.key: row for row in all_rows(db)}
    for key, value in changes.items():
        row = existing.get(key)
        if row is None:
            db.add(SiteSetting(key=key, value=value))
        else:
            row.value = value
            row.updated_at = now_utc()
    db.flush()
