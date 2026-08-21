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

# Ширины полей ящика. Объявлены здесь, а не числами по месту: по ним же сервис
# проверяет то, что ввёл человек (`mail_service._ACCOUNT_FIELDS`). Разойдись
# проверка с колонкой — получилась бы ровно та беда, ради которой проверка и
# заводилась: строка длиннее колонки, которую база отвергает уже на вставке,
# то есть пятисоткой вместо внятного отказа.
TITLE_LENGTH = 120
HOST_LENGTH = 255
ADDRESS_LENGTH = 320

#: Метка ключа, посчитанного отпечатком. Почему она вообще нужна — ниже.
MESSAGE_ID_DIGEST_PREFIX = "opencrm-sha256:"


def message_id_key(raw: str) -> str:
    """Message-ID → значение, которое ляжет в колонку ключа идемпотентности.

    **Обрезать ключ сравнения нельзя.** Раньше значение резалось
    `[:MESSAGE_ID_LENGTH]`, и два письма, у которых идентификаторы совпадают в
    первых 320 знаках, становились одной строкой: второе письмо считалось уже
    сохранённым и пропадало молча. Выглядит это как честная идемпотентность —
    в ответе синхронизации растёт `skipped`, — а на деле у клиента в ленте не
    хватает письма. Длинные идентификаторы с общим началом делают не только
    самодельные рассылки: так устроены `<кампания-дата-...-часть-N@mailer>`.

    Поэтому значение, которое в колонку не помещается, заменяется отпечатком
    **всего значения целиком**. Отпечаток различает письма ровно так же, как
    исходные строки, и устойчив: sha256 не солится и не зависит от хэша Python,
    то есть одно и то же письмо в любой заход и в любом процессе даёт один и
    тот же ключ — а на этом вся идемпотентность и держится.

    Короткое значение остаётся собой: по нему письмо находят глазами в базе и в
    логе, и менять это ради единообразия значило бы усложнить разбор жалобы
    «письмо не пришло» ради письма, которого не бывает.

    Метка `opencrm-sha256:` отделяет посчитанное от настоящего. Совпасть с
    настоящим Message-ID она теоретически может (RFC формат левой части не
    ограничивает), но тогда столкнулись бы конкретный короткий идентификатор
    вида `opencrm-sha256:<64 шестнадцатеричных знака>` и отпечаток конкретного
    длинного — цена такого совпадения одно потерянное письмо, а вероятность
    ниже, чем у совпадения самих sha256.
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
    # NULL — пароль не задан (ящик заведён, но не настроен до конца); пустая
    # строка означала бы «пароль есть, и он пустой» — это разные состояния.
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

    Это зеркало переписки, а не лента: показывать историю общения с клиентом
    обязана `client_notes` (см. `mail_service._add_feed_entry`). Здесь лежат
    подробности письма — заголовки, оба тела, адресаты.
    """

    __tablename__ = "mail_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        # Удалили ящик — его зеркало уезжает следом: без доступа к серверу оно
        # больше не обновится и не отличит настоящее письмо от устаревшего.
        # История общения при этом НЕ пропадает: она уже записана в ленту
        # клиента отдельными строками и от ящика не зависит.
        ForeignKey("mail_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    # UID письма в папке IMAP. NULL у исходящих: мы их отправили сами, на сервере
    # входящей почты их нет. 0 — валидный UID, поэтому именно NULL, а не ноль.
    uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ключ идемпотентности синхронизации — но не сам по себе, а в паре с ящиком
    # (уникальный индекс ниже). Глобальная уникальность здесь была ошибкой:
    # письмо, доставленное на два ящика фирмы, у второго ящика не сохранялось
    # вовсе, а раз у него не появлялось ни одной строки — `last_uid` (это
    # `max(uid)` по строкам ящика) оставался пустым, и КАЖДАЯ синхронизация
    # тянула ящик с начала. Навсегда.
    # Сравнение побайтное: Message-ID регистрозависим по RFC 5322, и в MySQL
    # с обычным сравнением два разных письма слиплись бы в одно.
    message_id: Mapped[str] = mapped_column(ExactString(MESSAGE_ID_LENGTH))
    direction: Mapped[str] = mapped_column(String(3))

    subject: Mapped[str] = mapped_column(String(500), default="")
    # Тела писем большие (html легко на сотни килобайт), а в списке они не нужны.
    # `deferred` — чтобы выборка списка не тянула их с диска: иначе страница
    # писем читала бы мегабайты ради заголовков.
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
        # Клиента вычистили из корзины — письмо остаётся, но теряет привязку.
        # CASCADE здесь стёр бы переписку фирмы вместе с карточкой, а это
        # документы: по ним разбираются в спорах и после ухода клиента.
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

    # Цепочка переписки: на что это письмо отвечает и вся ветка до него.
    #
    # ЗАЧЕМ ХРАНИТЬ, А НЕ СТАВИТЬ НА ЛЕТУ. Почтовые клиенты собирают переписку
    # по паре `In-Reply-To` + `References`, и `References` у длинной ветки —
    # это список ВСЕХ предыдущих писем. Взять его неоткуда, кроме как из того
    # письма, на которое отвечают: не сохранив его при синхронизации, наш ответ
    # унёс бы ссылку только на одно письмо, и у собеседника ветка распалась бы
    # на куски начиная с третьего сообщения.
    #
    # Текстом, а не связью на строку: письмо, на которое отвечают, может лежать
    # в чужом ящике или не лежать нигде — переписка начинается не всегда у нас.
    in_reply_to: Mapped[str] = mapped_column(String(MESSAGE_ID_LENGTH), default="")
    # Вся ветка одной строкой, через пробел, как в самом заголовке. Хранится
    # усечённой: у переписки на сотню писем заголовок разрастается до
    # килобайтов, а для сборки ветки хватает хвоста — клиенты смотрят на
    # последние ссылки.
    references: Mapped[str] = mapped_column(Text, default="", server_default=text_default())

    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    # Когда письмо появилось у нас. Отличается от sent_at: письмо трёхдневной
    # давности может приехать сегодня, и по этой паре видно задержку синхронизации.
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # Идемпотентность синхронизации: в одном ящике одно письмо лежит один
        # раз. Пара, а не один Message-ID, — почему, написано у колонки.
        #
        # Индекс, а не UNIQUE-ограничение: в MySQL это одно и то же. Длина
        # ключа считается так: 320 знаков utf8mb4 — 1280 байт, плюс int — 1284,
        # предел ключа InnoDB 3072.
        Index(
            "uq_mail_messages_account_message_id",
            "account_id",
            "message_id",
            unique=True,
        ),
        # «С какого UID забирать дальше» — единственный горячий запрос синхронизации.
        Index("ix_mail_messages_account_uid", "account_id", "uid"),
        # Дальше — три способа смотреть почту глазами, и все три сортируются по
        # `sent_at`. Индексов по одному признаку для этого мало: письма ящика
        # находились по `account_id`, а потом тридцать тысяч строк уезжали во
        # временное дерево сортировки.
        Index("ix_mail_messages_account_sent", "account_id", "sent_at"),
        Index("ix_mail_messages_client_sent", "client_id", "sent_at"),
        # Признак двузначный, и обычно такие в индекс не ставят. Но «непрочитанные»
        # — единственный фильтр на экране почты, а счётчик к нему читал таблицу
        # целиком. Пара закрывает и счётчик, и список.
        Index("ix_mail_messages_unread_sent", "is_read", "sent_at"),
    )
