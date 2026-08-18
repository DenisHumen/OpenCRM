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

from datetime import datetime

from sqlalchemy import and_, case, delete, func, select
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


# --- рост таблицы и уборка старого -------------------------------------------
#
# `telegram_messages` растёт быстрее всех таблиц системы, и решение «сколько
# хранить» принимает владелец. Запросы ниже обслуживают ровно это: первый
# показывает, сколько переписка весит СЕЙЧАС и сколько уйдёт при каждом из
# предлагаемых сроков, остальные два убирают старое пачками.


def ves_perepiski(db: Session, granitsy: dict[int, datetime]) -> dict:
    """Вес переписки и цена каждого срока хранения — ОДНИМ запросом.

    Одним, а не запросом на срок, и это главное в этой функции. Сроков пять, и
    пять отдельных счётов означали бы пять полных проходов по самой большой
    таблице системы — при том что читаются они разом, на одном экране, ради
    одного решения. Условная сумма (`CASE WHEN`) даёт все числа за один проход.

    `oldest_at` считается здесь же, и он не украшение: без него «хранить 12
    месяцев» — отвлечённое число. Владельцу нужно видеть, что самая старая
    переписка от такого-то числа, иначе выбирать не из чего.

    Вес считается по `file_size` тех строк, у которых файл ДЕЙСТВИТЕЛЬНО забран
    (`file_path` не пуст). У видео размер известен, а файла у нас нет — он
    остался в телеграме, и наш диск не занимает.
    """
    est_fayl = TelegramMessage.file_path != ""
    razmer = func.coalesce(TelegramMessage.file_size, 0)

    polya = [
        func.count(TelegramMessage.id),
        func.sum(case((est_fayl, 1), else_=0)),
        func.sum(case((est_fayl, razmer), else_=0)),
        func.min(TelegramMessage.happened_at),
    ]
    poryadok = sorted(granitsy)
    for mesyatsev in poryadok:
        do = granitsy[mesyatsev]
        staroe = TelegramMessage.happened_at < do
        polya.append(func.sum(case((staroe, 1), else_=0)))
        polya.append(func.sum(case((and_(staroe, est_fayl), razmer), else_=0)))

    stroka = db.execute(select(*polya)).one()

    po_srokam: dict[int, dict[str, int]] = {}
    for nomer, mesyatsev in enumerate(poryadok):
        po_srokam[mesyatsev] = {
            "messages": int(stroka[4 + nomer * 2] or 0),
            "bytes": int(stroka[5 + nomer * 2] or 0),
        }

    return {
        "messages": int(stroka[0] or 0),
        "files": int(stroka[1] or 0),
        "file_bytes": int(stroka[2] or 0),
        "oldest_at": stroka[3],
        "po_srokam": po_srokam,
    }


def skolko_starykh(db: Session, do: datetime) -> int:
    """Сколько сообщений старше границы. Для показа «сколько уйдёт»."""
    return int(
        db.scalar(
            select(func.count(TelegramMessage.id)).where(TelegramMessage.happened_at < do)
        )
        or 0
    )


def starye(db: Session, do: datetime, predel: int) -> list[tuple[int, str]]:
    """Пачка самых старых сообщений: пары «идентификатор, путь к файлу».

    Пачкой, а не всем разом: удаление миллиона строк одной транзакцией держит
    журнал отката всё время работы и кладёт базу — а таблица здесь как раз та,
    где миллион строк это обычное дело.

    Самые старые сначала (`happened_at`, по возрастанию), и это не косметика.
    Прогон ограничен по времени и обрывается на середине; начиная с дальнего
    края, каждый прогон продвигает границу, и следующий начинает с того места,
    где кончил предыдущий. Начни мы с произвольного места — уборка топталась бы
    по всей таблице, ни разу не дочистив её до конца.

    Отбор идёт по `happened_at`, по которому есть индекс: у него же и условие, и
    порядок, то есть база читает подряд ровно столько строк, сколько попросили.
    """
    return [
        (int(id_), str(put or ""))
        for id_, put in db.execute(
            select(TelegramMessage.id, TelegramMessage.file_path)
            .where(TelegramMessage.happened_at < do)
            .order_by(TelegramMessage.happened_at.asc())
            .limit(predel)
        ).all()
    ]


def udalit_soobshcheniya(db: Session, ids: list[int]) -> int:
    """Удалить названные сообщения. Возвращает, сколько строк ушло.

    Именно по списку идентификаторов, а не повторением условия по времени.
    Условие отобрало строки мгновением раньше; повтори мы его в удалении —
    под него попало бы и то, чего в отобранной пачке не было, а мы бы
    отчитались числом из этой пачки. Расхождение числа в журнале с тем, что
    произошло на самом деле, — ровно то, чего эта затея не допускает.

    Диалоги при этом НЕ трогаются: диалог помнит, кто это, к какой карточке
    привязан и откуда пришёл. Убрать его вместе с последним сообщением значило
    бы стереть привязку, которую человек ставил руками.
    """
    if not ids:
        return 0
    itog = db.execute(delete(TelegramMessage).where(TelegramMessage.id.in_(ids)))
    return int(itog.rowcount or 0)
