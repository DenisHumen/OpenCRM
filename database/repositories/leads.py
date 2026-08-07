"""Заявки с сайта: найти своего клиента, узнать поток.

Запросов здесь три, и ни один из них не повторяет уже написанные.

**Поиск клиента не пишется заново.** Почта ищет карточку по адресу
(`mail.find_client_by_email`), телефония — по нормализованному номеру
(`telephony.find_client_by_number`). Оба приёма выстраданы: первый сравнивает
через `lower()`, а не `ilike`, потому что `%` и `_` в адресе законны и для LIKE
означают не себя; второй смотрит в `phone_norm`, потому что «067…» и
«+380 67…» — один и тот же человек. Третья копия этих правил разошлась бы с
первыми двумя на первой же починке, причём молча: заявка просто завела бы
второго клиента там, где почта нашла бы первого.

Форма при этом не принадлежит ни почте, ни телефонии — поэтому «в каком порядке
спрашивать» решается здесь, одним местом, а не в сервисе.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Client, Deal
from database.repositories import mail as mail_repo
from database.repositories import telephony as telephony_repo


def find_client(db: Session, email: str, phone_norm: str) -> Client | None:
    """Клиент, которого система уже знает: сначала по почте, потом по номеру.

    Порядок не случайный. Адрес почты личный: два человека делят один
    семейный номер сплошь и рядом, один ящик — почти никогда. Совпади в заявке
    и адрес, и номер, но у разных карточек — берём ту, что нашлась по адресу.

    Пустое значение до запроса не доезжает: обе функции про пустую строку
    отвечают `None` сами.
    """
    by_email = mail_repo.find_client_by_email(db, email)
    if by_email is not None:
        return by_email
    return telephony_repo.find_client_by_number(db, phone_norm)


def deal_since(db: Session, client_id: int, since: datetime) -> Deal | None:
    """Свежая заявка этого клиента — та, из-за которой не нужна вторая.

    Отдельно от `deals.for_client`: тот отдаёт ВСЕ заявки клиента, чтобы
    показать их в карточке, а здесь нужен ответ «да или нет» на один вопрос и
    ровно одна строка. Разница видна не на трёх заявках, а на постоянном
    клиенте с сотней — и видна она на публичной ручке, куда стучатся снаружи.
    """
    return db.scalars(
        select(Deal)
        .where(
            Deal.client_id == client_id,
            Deal.deleted_at.is_(None),
            Deal.created_at >= since,
        )
        .order_by(Deal.created_at.desc(), Deal.id.desc())
        .limit(1)
    ).first()


def count_clients_since(db: Session, source: str, since: datetime) -> int:
    """Сколько карточек завёл этот источник за период.

    Считаем и мягко удалённых. Вопрос здесь не «сколько клиентов у бизнеса», а
    «сколько строк форма успела написать за час»; вычеркни удалённые — и
    разбор завала (владелец выделил мусор и нажал «удалить») сам же открывал бы
    ворота для следующей тысячи.
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(Client)
            .where(Client.source == source, Client.created_at >= since)
        )
        or 0
    )
