"""Переписка с клиентами через бота фирмы.

Бот один на систему, таблицы аккаунтов нет: сессия менеджера через MTProto равна
доступу к его личному телеграму, а за автоматизацию личных аккаунтов телеграм
блокирует номер. Цена одна — бот не пишет первым, клиента приводят ссылкой, её
метка и есть `source`. Живое состояние (кто смотрит переписку) — в Redis.

Индикатора набора нет и не будет: решено, что конфликты показываются только
предупреждающим баннером — ни мягкого замка при наборе, ни закрепления диалога
за сотрудником.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base

#: Направление сообщения. Ключи стабильные: уходят в базу и в фильтры.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

#: Что за вложение. `text` — обычное сообщение без файла.
KIND_TEXT = "text"
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_DOCUMENT = "document"
KIND_VOICE = "voice"
KINDS = (KIND_TEXT, KIND_PHOTO, KIND_VIDEO, KIND_DOCUMENT, KIND_VOICE)

#: Состояние отправки исходящего сообщения.
#:
#: Телеграм может отказать (бота заблокировали, сеть легла), а человек уверен,
#: что ответил, и ждёт реакции: молча потерянное сообщение хуже, чем в почте.
SEND_PENDING = "pending"
SEND_SENT = "sent"
SEND_FAILED = "failed"
SEND_STATES = (SEND_PENDING, SEND_SENT, SEND_FAILED)


class TelegramChat(Base):
    """Диалог с одним человеком.

    Групп и каналов здесь нет по устройству: бот подключается к личной переписке.
    """

    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Идентификатор чата в телеграме. `BigInteger`, а не `Integer`: телеграм
    #: давно выдаёт больше двух миллиардов, и узкая колонка уже давала пятисотку
    #: из MySQL (`tests/test_potolki.py`) — здесь она стоила бы приёма сообщений.
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    #: Как человек подписан в телеграме. Снимком, а не ссылкой: имя меняют, а
    #: переписка обязана остаться понятной.
    username: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(200), default="")

    #: Телефон, если человек им поделился кнопкой. Нормализованный рядом, тем же
    #: приёмом, что у клиента: «067…» и «+380 67…» — один человек.
    phone: Mapped[str] = mapped_column(String(64), default="")
    phone_norm: Mapped[str] = mapped_column(String(32), default="", index=True)

    #: Карточка клиента, если диалог к ней привязан. Пусто — нормальное
    #: состояние: привязка по неточному совпадению запрещена, в заказах она
    #: уводила деньги и товар на чужую карточку.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Откуда человек пришёл: метка из ссылки `t.me/бот?start=метка`.
    source: Mapped[str] = mapped_column(String(64), default="")

    #: Последнее сообщение — для сортировки списка диалогов. Производное, но
    #: хранимое: иначе список требует соединения с самой быстрорастущей таблицей
    #: на каждый показ, а расхождение здесь безвредно — следующее перезапишет.
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    #: Фотография профиля: путь к забранному файлу относительно хранилища. Аватар
    #: НЕ приходит с сообщением, его спрашивают отдельным вызовом — отсюда и
    #: отметка рядом: без неё мы ходили бы за ним на каждый показ списка.
    avatar_path: Mapped[str] = mapped_column(String(255), default="")

    #: Когда СПРАШИВАЛИ у телеграма — не когда получили: у многих аватара нет
    #: вовсе, и это обычное состояние. Отметка ставится в обоих случаях, иначе
    #: безаватарный собеседник стоил бы вызова при каждом обращении.
    avatar_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: Есть ли у собеседника премиум телеграма. Приходит прямо в сообщении
    #: (`message.from.is_premium`), не стоит ни одного лишнего вызова. Снимком:
    #: между сообщениями врёт на срок молчания, а вечная правда стоила бы опроса.
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    #: Именной эмодзи: путь к забранной СТАТИЧНОЙ картинке. Сам эмодзи — стикер
    #: TGS или WEBM, браузер его не рисует; храним миниатюру, которую рисует.
    #: Бывает только у премиума — у него только и спрашиваем.
    emoji_status_path: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # `__table_args__` здесь нет намеренно: явный `Index` по `last_message_at`
    # при `index=True` у самой колонки давал ДВА одинаковых индекса — лишний вес
    # на каждой вставке и выбор из тождественных путей для планировщика.


class TelegramMessage(Base):
    """Одно сообщение переписки.

    Самая быстрорастущая таблица канала. Потолок и уборка старого — решение
    владельца, а не молчаливое удаление: переписка бывает доказательством.
    """

    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), index=True
    )

    #: Идентификатор сообщения в телеграме. У исходящих появляется только после
    #: успешной отправки, отсюда nullable: строка заводится ДО обращения к
    #: телеграму, чтобы двойное нажатие не отправило дважды.
    external_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    #: Куда шло сообщение. Своего одиночного индекса НЕТ намеренно: значений два,
    #: и замер показал, что MySQL брал его вместо составного и читал все входящие
    #: строки. Направление входит первым в `ix_telegram_messages_napravlenie`.
    direction: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(16), default=KIND_TEXT)

    #: Текст сообщения или подпись к файлу. `Text`, а не `String`: подпись
    #: считается отдельно от предела в 4096 знаков, а потолок колонки здесь —
    #: потерянный хвост чужого сообщения.
    body: Mapped[str] = mapped_column(Text, default="")

    #: Чем файл забрать у телеграма, если ещё не забрали. Нужен ровно для видео:
    #: сразу не тянем (гигабайт переписки за месяц съест диск), а ссылка живёт
    #: около часа — `file_id` постоянен, по нему заберём, когда попросят.
    file_id: Mapped[str] = mapped_column(String(200), default="")

    #: Файл в хранилище клиентских файлов, если он был.
    file_path: Mapped[str] = mapped_column(String(500), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Кто отправил из наших. Пусто у входящих: их отправил клиент. `SET NULL`,
    #: а не каскад: уволенный менеджер не уносит с собой переписку с клиентом.
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Сообщение, на которое отвечают. Ссылка на СВОЮ запись, а не на номер в
    #: телеграме: тот уникален лишь внутри чата и у исходящего появляется только
    #: ПОСЛЕ отправки, а цитату показывают сразу. `SET NULL`: ответ живёт без неё.
    reply_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_messages.id", ondelete="SET NULL"), nullable=True
    )

    send_state: Mapped[str] = mapped_column(String(16), default=SEND_SENT)
    #: Чем именно отказал телеграм. Пусто, пока всё хорошо.
    send_error: Mapped[str] = mapped_column(String(255), default="")

    #: Время события, а не время записи: у входящих это время телеграма.
    happened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # Лента одного диалога: страница за страницей, свежие сверху.
        Index("ix_telegram_messages_chat_time", "chat_id", "happened_at"),
        # Счёт непрочитанного по всей странице списка: условия ложатся на индекс
        # диапазонами, проход выходит ПОКРЫВАЮЩИМ (замер — docs/03-database.md,
        # «Счёт непрочитанного»). `id` в хвосте назван явно: так он попадает в
        # определение, и `alembic` с проверкой схемы видят то же, что база.
        Index("ix_telegram_messages_napravlenie", "direction", "chat_id", "id"),
    )
