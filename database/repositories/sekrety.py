"""Шифротексты `secretbox` в базе: прочитать все и переписать.

Нужно ровно на одно: восстановление копии, снятой на ДРУГОЙ машине. Там токены
зашифрованы чужим `OPENCRM_SECRET_KEY`, и переложить их под нынешний может
только тот, кто ходит в базу. Реестр мест и сама перекладка — в
`core/services/sekrety_service.py`; здесь только запросы.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session


def shifrotexty(db: Session, model, kolonka) -> list[tuple[int, str]]:
    """[(id, токен), …] — непустые шифротексты одной колонки."""
    rows = db.execute(
        select(model.id, kolonka)
        .where(kolonka.is_not(None), kolonka != "")
        .order_by(model.id)
    ).all()
    return [(int(nomer), token) for nomer, token in rows]


def perepisat(db: Session, model, kolonka, novye: dict[int, str]) -> int:
    """Переписать токены по номерам строк. Возвращает, сколько переписано.

    Построчно, а не одним `UPDATE`: у каждой строки своё новое значение, и
    собирать из них `CASE` на десяток записей — сложность без выигрыша.
    """
    for nomer, token in novye.items():
        db.execute(update(model).where(model.id == nomer).values({kolonka.key: token}))
    return len(novye)
