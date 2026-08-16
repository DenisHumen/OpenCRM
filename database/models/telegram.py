"""Переписка с клиентами через бота фирмы.

**Что это и чем отличается от телефонии и почты.** Третий канал общения в той
же ленте клиента. Разница в том, что у него есть ЖИВОЕ состояние: кто сейчас
смотрит переписку, кто печатает. Всё это по правилу проекта не хранится —
оно производное от того, кто на связи, — и живёт в Redis ключами со сроком
годности. Здесь только то, что обязано пережить перезапуск: сами диалоги и
сами сообщения.

**Почему нет таблицы аккаунтов.** Бот один на всю систему. Развилка «личные
аккаунты менеджеров через MTProto» разобрана и закрыта: сессия менеджера на
сервере равна полному доступу к его личному телеграму, а автоматизацию личных
аккаунтов телеграм наказывает блокировкой личного номера. Бот — законный
способ, отзываемый секрет и ничего чужого на диске.

**Цена этого решения, и она одна.** Клиент обязан сам начать разговор с ботом:
телеграм не позволяет боту написать первым тому, кто его не запускал. Значит
переписка, которая уже идёт в личных телеграмах менеджеров, здесь не появится;
новых клиентов приводят к боту ссылкой, QR-кодом на квитанции, кнопкой на
сайте. Ссылка умеет нести метку (`t.me/бот?start=метка`) — отсюда `source` у
диалога.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
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
#: Нужно потому, что телеграм может отказать (клиент заблокировал бота, сеть
#: легла), и менеджер обязан это увидеть. Молча потерянное сообщение здесь
#: хуже, чем в почте: человек уверен, что ответил, и ждёт реакции.
SEND_PENDING = "pending"
SEND_SENT = "sent"
SEND_FAILED = "failed"
SEND_STATES = (SEND_PENDING, SEND_SENT, SEND_FAILED)


class TelegramChat(Base):
    """Диалог с одним человеком.

    Один на один: групп и каналов у этого канала нет по устройству — бот
    подключается к личной переписке, и заказчик подтвердил, что общение только
    один на один.
    """

    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Идентификатор чата в телеграме. `BigInteger`, а не `Integer`, и это не
    #: перестраховка: телеграм давно выдаёт идентификаторы больше двух
    #: миллиардов, а у нас уже был случай, когда потолок проверки оказался выше
    #: вместимости колонки и человек получал пятисотку из MySQL
    #: (`tests/test_potolki.py`). Здесь та же ошибка стоила бы приёма сообщений.
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    #: Как человек подписан в телеграме. Хранится снимком, а не ссылкой: имя
    #: меняют, а переписка обязана остаться понятной.
    username: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(200), default="")

    #: Телефон, если человек им поделился кнопкой. Нормализованный — рядом, тем
    #: же приёмом, что у клиента: «067…» и «+380 67…» это один человек.
    phone: Mapped[str] = mapped_column(String(64), default="")
    phone_norm: Mapped[str] = mapped_column(String(32), default="", index=True)

    #: Карточка клиента, если диалог к ней привязан.
    #:
    #: **Пусто — это нормальное состояние, а не ошибка.** Привязка по неточному
    #: совпадению запрещена: в заказах привязка по частичному совпадению имени
    #: уводила деньги и товар на чужую карточку, и чинили это отдельно. Здесь
    #: цена ошибки та же — чужая переписка в чужой карточке.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Откуда человек пришёл: метка из ссылки `t.me/бот?start=метка`.
    source: Mapped[str] = mapped_column(String(64), default="")

    #: Последнее сообщение — для сортировки списка диалогов.
    #:
    #: Хранится, хотя это производное, и вот почему это НЕ нарушение правила.
    #: Правило запрещает хранить то, что можно посчитать и что однажды
    #: разойдётся с истиной незаметно: остаток склада, себестоимость. Здесь же
    #: без этой колонки список диалогов, отсортированный по времени последнего
    #: сообщения, требует соединения с таблицей сообщений на каждый показ — а
    #: она самая быстрорастущая в системе. Расхождение при этом безвредно и
    #: самозаживает: следующее сообщение перезапишет.
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # Список диалогов: сначала непривязанные к клиенту не выделяются, а
        # сортировка всегда по времени последнего сообщения убыванием.
        Index("ix_telegram_chats_last_message", "last_message_at"),
    )


class TelegramMessage(Base):
    """Одно сообщение переписки.

    Самая быстрорастущая таблица канала. Потолок и уборка старого — отдельное
    решение владельца, а не молчаливое удаление: переписка с клиентом бывает
    доказательством.
    """

    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), index=True
    )

    #: Идентификатор сообщения в телеграме.
    #:
    #: У исходящих он появляется только после успешной отправки, поэтому
    #: nullable: строка заводится ДО обращения к телеграму, чтобы двойное
    #: нажатие не отправило дважды.
    external_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    direction: Mapped[str] = mapped_column(String(8), index=True)
    kind: Mapped[str] = mapped_column(String(16), default=KIND_TEXT)

    #: Текст сообщения или подпись к файлу. `Text`, а не `String`: предел
    #: телеграма 4096 знаков, но подпись к файлу считается отдельно, и упереться
    #: в потолок колонки здесь значило бы потерять хвост чужого сообщения.
    body: Mapped[str] = mapped_column(Text, default="")

    #: Файл в хранилище клиентских файлов, если он был.
    file_path: Mapped[str] = mapped_column(String(500), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Кто отправил из наших. Пусто у входящих: их отправил клиент.
    #:
    #: `SET NULL` при удалении сотрудника, а не каскад: уволенный менеджер не
    #: должен уносить с собой переписку с клиентом.
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
    )
