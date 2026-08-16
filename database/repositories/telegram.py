"""Диалоги и сообщения телеграма: запросы.

Мессенджер отличается от прочих разделов тем, что его читают ПОСТОЯННО: экран
открыт весь рабочий день, список диалогов обновляется живьём. Значит запросы
здесь обязаны быть дешёвыми не «в среднем», а всегда: лишний запрос на строку
превращается в лишний запрос на строку каждые несколько секунд у каждого
менеджера.

Отсюда два решения, которые видно в коде:

- список диалогов сортируется по `last_message_at` — колонке, а не по
  соединению с таблицей сообщений. Соединение здесь стоило бы прохода по самой
  быстрорастущей таблице системы на каждый показ;
- счётчик непрочитанного НЕ хранится и НЕ считается на каждый диалог отдельно:
  он берётся одним запросом с группировкой на всю страницу сразу.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Client, TelegramChat, TelegramMessage
from database.models.telegram import DIRECTION_IN
from database.query import contains, page_of


def get_chat(db: Session, chat_row_id: int) -> TelegramChat | None:
    return db.get(TelegramChat, chat_row_id)


def get_by_chat_id(db: Session, chat_id: int) -> TelegramChat | None:
    """Диалог по идентификатору телеграма — точка склейки входящих."""
    return db.scalar(select(TelegramChat).where(TelegramChat.chat_id == chat_id))


def vzyat_pod_pravku(db: Session, chat_id: int) -> TelegramChat | None:
    """То же, но строка запирается до конца транзакции.

    Нужно там, где входящее событие ПРАВИТ существующий диалог. Телеграм
    повторяет доставку при обрыве, и два обновления об одном сообщении приходят
    разом. Урок уже оплачен в телефонии: без замка оба читали строку, оба
    видели пустое поле, и в ленте клиента появлялись две записи об одном
    разговоре. SQLAlchemy не перечитывает загруженный объект после снятия
    блокировки, поэтому спасает только `FOR UPDATE` — он ждёт чужого коммита ДО
    чтения.
    """
    return db.scalar(
        select(TelegramChat).where(TelegramChat.chat_id == chat_id).with_for_update()
    )


def create_chat(db: Session, **polya) -> TelegramChat:
    row = TelegramChat(**polya)
    db.add(row)
    db.flush()
    return row


def find_client_by_phone(db: Session, phone_norm: str) -> Client | None:
    """Карточка по нормализованному номеру — единственная точная привязка.

    Именно по номеру и только по нему. Привязка по имени запрещена, и это не
    осторожность, а оплаченный урок: в заказах совпадение по частичному имени
    уводило деньги и товар на чужую карточку. Здесь ценой была бы чужая
    переписка в чужой карточке — то есть переписка, которую видит не тот
    человек.

    Удалённые карточки не считаются: клиент в корзине не должен молча получать
    новую переписку.
    """
    if not phone_norm:
        return None
    return db.scalars(
        select(Client)
        .where(Client.phone_norm == phone_norm, Client.deleted_at.is_(None))
        .order_by(Client.updated_at.desc())
        .limit(1)
    ).first()


def spisok_dialogov(
    db: Session, *, q: str = "", page: int = 1, per_page: int = 50
) -> tuple[list[TelegramChat], int]:
    """Страница списка диалогов, свежие сверху.

    Сортировка по `last_message_at` убыванием, а пустые — в конец: диалог, где
    ещё ничего не сказано, не должен занимать первую строку.
    """
    zapros = select(TelegramChat)
    if q:
        # Через `contains`, а не через `like` руками: `%` и `_` в поисковой
        # строке для LIKE означают не себя, и человек, ищущий «100%», получил бы
        # весь список. Приём общий на весь проект и стережётся отдельно
        # (`tests/test_query.py`).
        zapros = zapros.where(
            contains(TelegramChat.title, q)
            | contains(TelegramChat.username, q)
            | contains(TelegramChat.phone, q)
        )
    # Страницу нарезает общий `page_of`, а не смещение, посчитанное здесь.
    # Своя арифметика страниц — это второе место, где считается одно и то же, и
    # разъезжаются они на первой же правке предела.
    return page_of(
        db,
        zapros.order_by(
            TelegramChat.last_message_at.is_(None),
            TelegramChat.last_message_at.desc(),
            TelegramChat.id.desc(),
        ),
        page=page,
        per_page=per_page,
    )


def neprochitannye(db: Session, chat_ids: list[int], granitsy: dict[int, datetime]) -> dict[int, int]:
    """Сколько непрочитанного в каждом диалоге — ОДНИМ запросом на всю страницу.

    Не по одному запросу на диалог: страница списка это полсотни строк, и
    запрос на строку означал бы полсотни обращений каждые несколько секунд у
    каждого открытого экрана.

    Граница «прочитано» приходит снаружи, из живого состояния (Redis): она
    производная и по правилу проекта не хранится. Диалог, о котором граница
    неизвестна, считается непрочитанным целиком — это честнее, чем показать
    ноль и дать сообщению потеряться.
    """
    if not chat_ids:
        return {}
    stroki = db.execute(
        select(TelegramMessage.chat_id, func.count(TelegramMessage.id), func.max(TelegramMessage.happened_at))
        .where(
            TelegramMessage.chat_id.in_(chat_ids),
            TelegramMessage.direction == DIRECTION_IN,
        )
        .group_by(TelegramMessage.chat_id)
    ).all()

    itog: dict[int, int] = {}
    for chat_row_id, vsego, _posledneye in stroki:
        granitsa = granitsy.get(chat_row_id)
        if granitsa is None:
            itog[chat_row_id] = int(vsego)
            continue
        itog[chat_row_id] = int(
            db.scalar(
                select(func.count(TelegramMessage.id)).where(
                    TelegramMessage.chat_id == chat_row_id,
                    TelegramMessage.direction == DIRECTION_IN,
                    TelegramMessage.happened_at > granitsa,
                )
            )
            or 0
        )
    return itog


def lenta(
    db: Session, chat_row_id: int, *, do_id: int | None = None, limit: int = 50
) -> list[TelegramMessage]:
    """Страница переписки: свежие сверху, дальше — «показать ещё».

    Листание по идентификатору, а не по смещению. Смещение на живой переписке
    врёт: пока человек читает, приходят новые сообщения, и вторая страница
    показывает то, что уже было на первой.
    """
    zapros = select(TelegramMessage).where(TelegramMessage.chat_id == chat_row_id)
    if do_id is not None:
        zapros = zapros.where(TelegramMessage.id < do_id)
    return list(db.scalars(zapros.order_by(TelegramMessage.id.desc()).limit(limit)))


def novee(db: Session, chat_row_id: int, posle_id: int) -> list[TelegramMessage]:
    """Что появилось в диалоге после известного сообщения.

    Запасной путь живого обновления: соединение оборвалось, браузер вернулся и
    дочитывает пропущенное. Начинать с чистого листа нельзя — потерянное при
    обрыве сообщение в переписке с клиентом заметит клиент, а не мы.
    """
    return list(
        db.scalars(
            select(TelegramMessage)
            .where(TelegramMessage.chat_id == chat_row_id, TelegramMessage.id > posle_id)
            .order_by(TelegramMessage.id.asc())
            .limit(200)
        )
    )


def dobavit_soobshchenie(db: Session, **polya) -> TelegramMessage:
    """Записать сообщение и подтянуть время последнего у диалога.

    Двумя действиями в одной транзакции: строка сообщения и отметка у диалога.
    Разъехаться они не могут — либо оба, либо ни одного.
    """
    row = TelegramMessage(**polya)
    db.add(row)
    db.flush()
    chat = db.get(TelegramChat, row.chat_id)
    if chat is not None and (
        chat.last_message_at is None or row.happened_at > chat.last_message_at
    ):
        chat.last_message_at = row.happened_at
    return row


def po_vneshnemu_id(db: Session, chat_row_id: int, external_id: int) -> TelegramMessage | None:
    """Сообщение по идентификатору телеграма — защита от повторной доставки."""
    return db.scalar(
        select(TelegramMessage).where(
            TelegramMessage.chat_id == chat_row_id,
            TelegramMessage.external_id == external_id,
        )
    )
