import hashlib
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text

from database.types import ExactString, LongText, text_default
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Направление письма. Строки, а не числа: в дампе базы и в логе `in`/`out`
# читаются без сверки со справочником.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
MAIL_DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

# Message-ID по RFC 5322 длину не ограничивает, на практике он короче сотни
# символов. 320 — с большим запасом и укладывается в предел индекса MySQL.
MESSAGE_ID_LENGTH = 320

# Ширины полей ящика объявлены здесь, а не числами по месту: по ним же сервис
# проверяет ввод (`mail_service._ACCOUNT_FIELDS`). Разойдись проверка с колонкой
# — строку длиннее колонки база отвергнет пятисоткой вместо внятного отказа.
TITLE_LENGTH = 120
HOST_LENGTH = 255
ADDRESS_LENGTH = 320

#: Метка отличает посчитанный отпечаток от настоящего Message-ID.
MESSAGE_ID_DIGEST_PREFIX = "opencrm-sha256:"


def message_id_key(raw: str) -> str:
    """Message-ID → значение, которое ляжет в колонку ключа идемпотентности.

    Обрезка ключа роняла письма: длинные идентификаторы с общим началом (так устроены
    рассылки) слипались, второе уходило в `skipped` — рапорт об успехе, а письма в ленте нет.
    Не влезло — sha256 всего значения, один в любом процессе; разбор — docs/03-database.md.
    """
    value = (raw or "").strip()
    if len(value) <= MESSAGE_ID_LENGTH:
        return value
    return MESSAGE_ID_DIGEST_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


class MailAccount(Base):
    """Почтовый ящик фирмы. Настройка уровня компании, правит только root."""

    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(TITLE_LENGTH), default="")
    address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), index=True)

    imap_host: Mapped[str] = mapped_column(String(HOST_LENGTH), default="")
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_ssl: Mapped[bool] = mapped_column(Boolean, default=True)

    smtp_host: Mapped[str] = mapped_column(String(HOST_LENGTH), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_ssl: Mapped[bool] = mapped_column(Boolean, default=True)

    login: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), default="")
    # Зашифровано core/security/secretbox на ключе из OPENCRM_SECRET_KEY.
    # NULL — пароль не задан; пустая строка означала бы «пароль есть, и он
    # пустой», а это разные состояния.
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # NULL — не синхронизировали ни разу. Это не то же самое, что «давно»:
    # по нулю нельзя отличить новый ящик от сломанного.
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # NULL — ошибок не было. Пустая строка была бы «ошибка без текста».
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MailMessage(Base):
    """Письмо, забранное с сервера или отправленное из CRM.

    Зеркало переписки, а не лента: историю общения показывает `client_notes`
    (см. `mail_service._add_feed_entry`), здесь — подробности письма.
    """

    __tablename__ = "mail_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        # Удалили ящик — зеркало уезжает следом: без доступа к серверу оно
        # больше не обновится. История общения НЕ пропадает — она отдельными
        # строками в ленте клиента и от ящика не зависит.
        ForeignKey("mail_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    # UID письма в папке IMAP. NULL у исходящих: мы их отправили сами, на сервере
    # входящей почты их нет. 0 — валидный UID, поэтому именно NULL, а не ноль.
    uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ключ идемпотентности — в паре с ящиком (индекс ниже), а не глобально: письмо на два
    # ящика фирмы у второго не сохранялось, `last_uid` пустовал, ящик КАЖДЫЙ раз тянулся с
    # начала. Побайтно: Message-ID регистрозависим (RFC 5322), а MySQL слил бы два в одно.
    message_id: Mapped[str] = mapped_column(ExactString(MESSAGE_ID_LENGTH))
    direction: Mapped[str] = mapped_column(String(3))

    subject: Mapped[str] = mapped_column(String(500), default="")
    # Тела большие (html — сотни килобайт), в списке не нужны: без `deferred`
    # страница писем читала бы мегабайты ради заголовков.
    body_text: Mapped[str] = mapped_column(LongText, default="", deferred=True)
    body_html: Mapped[str] = mapped_column(LongText, default="", deferred=True)

    from_addr: Mapped[str] = mapped_column(String(320), default="", index=True)
    # Получателей может быть несколько; отдельная таблица адресатов здесь не
    # нужна — по ним не ищут и не группируют, их только показывают.
    to_addrs: Mapped[str] = mapped_column(Text, default="")

    # Момент отправки письма, приведённый к UTC (naive), а НЕ момент синхронизации:
    # иначе вся почта, забранная одним заходом, встала бы в ленте одной кучей.
    sent_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)

    client_id: Mapped[int | None] = mapped_column(
        # Клиента вычистили — письмо остаётся, но теряет привязку. CASCADE стёр
        # бы переписку вместе с карточкой, а это документы: по ним разбираются
        # в спорах и после ухода клиента.
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    deal_id: Mapped[int | None] = mapped_column(
        # Та же причина, что у клиента: заявку закрыли и вычистили — переписка
        # по ней остаётся, она сама по себе документ.
        ForeignKey("deals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Хранится, а не собирается на лету: `References` длинной ветки берётся только
    # из письма, на которое отвечают, — не сохранив его, наш ответ рвёт ветку у
    # собеседника с третьего сообщения. Текстом: родителя может не быть у нас.
    in_reply_to: Mapped[str] = mapped_column(String(MESSAGE_ID_LENGTH), default="")
    # Вся ветка одной строкой, как в заголовке, но усечённой: на сотне писем
    # заголовок разрастается до килобайтов, а клиентам хватает хвоста ссылок.
    references: Mapped[str] = mapped_column(Text, default="", server_default=text_default())

    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    # Когда письмо появилось у нас. Отличается от sent_at: письмо трёхдневной
    # давности может приехать сегодня, и по этой паре видно задержку синхронизации.
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # Идемпотентность: в одном ящике письмо лежит один раз. Индекс, а не
        # UNIQUE-ограничение: в MySQL это одно и то же. Ключ 1284 байта (320
        # знаков utf8mb4 плюс int) при пределе InnoDB в 3072.
        Index(
            "uq_mail_messages_account_message_id",
            "account_id",
            "message_id",
            unique=True,
        ),
        # «С какого UID забирать дальше» — единственный горячий запрос синхронизации.
        Index("ix_mail_messages_account_uid", "account_id", "uid"),
        # Три способа смотреть почту глазами, и все три сортируются по `sent_at`.
        # По одному признаку мало: письма ящика находились, а потом тридцать
        # тысяч строк уезжали во временное дерево сортировки.
        Index("ix_mail_messages_account_sent", "account_id", "sent_at"),
        Index("ix_mail_messages_client_sent", "client_id", "sent_at"),
        # Признак двузначный, но «непрочитанные» — единственный фильтр на экране
        # почты, а счётчик к нему читал таблицу целиком. Пара закрывает оба.
        Index("ix_mail_messages_unread_sent", "is_read", "sent_at"),
    )
