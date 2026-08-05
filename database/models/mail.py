from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Направление письма. Строки, а не числа: в дампе базы и в логе `in`/`out`
# читаются без сверки со справочником.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
MAIL_DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

# Message-ID по RFC 5322 длину не ограничивает, на практике он короче сотни
# символов. 320 — с большим запасом и укладывается в предел индекса MySQL,
# куда проект собирается переезжать.
MESSAGE_ID_LENGTH = 320


class MailAccount(Base):
    """Почтовый ящик фирмы. Настройка уровня компании, правит только root."""

    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    address: Mapped[str] = mapped_column(String(320), index=True)

    imap_host: Mapped[str] = mapped_column(String(255), default="")
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_ssl: Mapped[bool] = mapped_column(Boolean, default=True)

    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_ssl: Mapped[bool] = mapped_column(Boolean, default=True)

    login: Mapped[str] = mapped_column(String(320), default="")
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
    # Уникален: на нём держится идемпотентность синхронизации. Побочный эффект —
    # письмо, пришедшее сразу на два наших ящика, сохранится один раз. Для ленты
    # клиента это правильно: разговор был один.
    message_id: Mapped[str] = mapped_column(
        String(MESSAGE_ID_LENGTH), unique=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(3))

    subject: Mapped[str] = mapped_column(String(500), default="")
    # Тела писем большие (html легко на сотни килобайт), а в списке они не нужны.
    # `deferred` — чтобы выборка списка не тянула их с диска: иначе страница
    # писем читала бы мегабайты ради заголовков.
    body_text: Mapped[str] = mapped_column(Text, default="", deferred=True)
    body_html: Mapped[str] = mapped_column(Text, default="", deferred=True)

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

    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    # Когда письмо появилось у нас. Отличается от sent_at: письмо трёхдневной
    # давности может приехать сегодня, и по этой паре видно задержку синхронизации.
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # «С какого UID забирать дальше» — единственный горячий запрос синхронизации.
        Index("ix_mail_messages_account_uid", "account_id", "uid"),
    )
