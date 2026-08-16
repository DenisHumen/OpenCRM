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
- листание переписки идёт по идентификатору, а не по смещению: смещение на
  живой переписке врёт, потому что пока человек читает, приходят новые
  сообщения, и вторая страница показывает то, что уже было на первой.

Счётчика непрочитанного здесь НЕТ, и это осознанно. Он требует границы
«прочитано» на сотрудника, а такой границы в системе пока не существует:
живое состояние знает, кто В чате, но не помнит, до какого места он дочитал.
Запрос, считающий непрочитанным весь диалог, был бы хуже отсутствия счётчика —
он показывал бы одно и то же число всегда.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Client, TelegramChat, TelegramMessage
from database.models.telegram import DIRECTION_IN
from database.query import contains, page_of


def get_chat(db: Session, chat_row_id: int) -> TelegramChat | None:
    return db.get(TelegramChat, chat_row_id)


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
    db: Session, *, q: str = "", source: str = "", page: int = 1, per_page: int = 50
) -> tuple[list[TelegramChat], int]:
    """Страница списка диалогов, свежие сверху.

    Сортировка по `last_message_at` убыванием, а пустые — в конец: диалог, где
    ещё ничего не сказано, не должен занимать первую строку.

    Отбор по метке (`source`) отвечает на вопрос «сколько пришло с наклейки, а
    сколько с сайта». Метка кладётся в диалог при первом `/start метка`, и без
    отбора она лежала бы мёртвым грузом: записана — и не спросишь.
    """
    zapros = select(TelegramChat)
    if source:
        # Точное совпадение, а не подстрока: метки короткие и назначает их
        # владелец сам. Подстрока склеила бы «сайт» и «сайт-акция».
        zapros = zapros.where(TelegramChat.source == source)
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


def po_id(db: Session, message_id: int) -> TelegramMessage | None:
    """Сообщение по своему идентификатору."""
    return db.get(TelegramMessage, message_id)


def neprochitannye(db: Session, chat_ids: list[int], granitsy: dict[int, int]) -> dict[int, int]:
    """Сколько ВХОДЯЩИХ пришло после границы «прочитано» — по всей странице сразу.

    Одним запросом с группировкой, а не запросом на диалог: страница списка это
    полсотни строк, и запрос на строку означал бы полсотни обращений каждые
    несколько секунд у каждого открытого экрана.

    Граница приходит снаружи (из Redis) и по каждому диалогу своя. Отсутствие
    границы значит «не читал вовсе» — тогда непрочитано всё входящее. Это
    честнее нуля: ошибиться в сторону «посмотри ещё раз» безвредно, а в
    обратную — значит спрятать сообщение клиента.

    Считаются только входящие: свои же ответы непрочитанными не бывают.
    """
    if not chat_ids:
        return {}
    # Границы у диалогов РАЗНЫЕ, и уложить их в одно условие SQL можно только
    # через `CASE` по всему словарю — то есть запросом длиной со страницу,
    # который вдобавок не воспользуется индексом. Поэтому отсев грубый (одно
    # число на всех), а точный счёт — по отобранным строкам.
    #
    # Строк после отсева единицы: это входящие, пришедшие после того, как самый
    # отставший сотрудник дочитал свой диалог. Их и показывают значком.
    nizhnyaya = min(granitsy.values(), default=0)
    stroki = db.execute(
        select(TelegramMessage.chat_id, TelegramMessage.id).where(
            TelegramMessage.chat_id.in_(chat_ids),
            TelegramMessage.direction == DIRECTION_IN,
            TelegramMessage.id > nizhnyaya,
        )
    ).all()

    itog: dict[int, int] = {cid: 0 for cid in chat_ids}
    for chat_row_id, message_id in stroki:
        if message_id > granitsy.get(chat_row_id, 0):
            itog[chat_row_id] += 1
    return itog
