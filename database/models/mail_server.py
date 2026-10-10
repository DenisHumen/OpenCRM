"""Свой почтовый сервер: где стоит, какие домены, как шла установка, кто видит ящики.

Задание владельца от 09.10.2026, разбор — docs/bloki/32-pochtovyy-server.md.
Сам сервер — docker-mailserver: на этой же машине (ставит обновлятор на хосте)
или на другой по SSH. Ящики сервера — это `MailAccount` с `server_id`: зеркало
писем, лента клиента и отправка работают с ними так же, как с внешними ящиками.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.models.mail import MESSAGE_ID_LENGTH
from database.session import Base
from database.types import ExactString, LongText, text_default

# Где стоит сервер. Строки, а не числа: в дампе и журнале читаются без справочника.
MODE_LOCAL = "local"
MODE_SSH = "ssh"
SERVER_MODES = (MODE_LOCAL, MODE_SSH)

# Жизнь сервера: черновик мастера → установка → работает; провал откатывается в failed.
STATUS_DRAFT = "draft"
STATUS_INSTALLING = "installing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
SERVER_STATUSES = (STATUS_DRAFT, STATUS_INSTALLING, STATUS_READY, STATUS_FAILED)

JOB_CHECK = "check"
JOB_INSTALL = "install"
JOB_ADOPT = "adopt"
JOB_REMOVE = "remove"
JOB_KINDS = (JOB_CHECK, JOB_INSTALL, JOB_ADOPT, JOB_REMOVE)

JOB_RUNNING = "running"
JOB_OK = "ok"
JOB_FAILED = "failed"
JOB_ROLLED_BACK = "rolled_back"
JOB_STATUSES = (JOB_RUNNING, JOB_OK, JOB_FAILED, JOB_ROLLED_BACK)

# Токен: все ящики сотрудника или только перечисленные (`UserTokenMailBox`).
TOKEN_MAIL_ALL = "all"
TOKEN_MAIL_LIST = "list"

HOSTNAME_LENGTH = 255
DOMAIN_LENGTH = 253
ADDRESS_LENGTH = 320
#: Путь папки IMAP. Побайтно: «Work» и «work» на сервере — две разные папки.
FOLDER_LENGTH = 500
DEFAULT_IMAGE = "ghcr.io/docker-mailserver/docker-mailserver:16.0.1"


class MailServer(Base):
    """Почтовый сервер под управлением CRM. Обычно один, но доменов на нём сколько угодно."""

    __tablename__ = "mail_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    mode: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default=STATUS_DRAFT)
    # Имя сервера в MX и в сертификате: mail.example.com. По нему же сверяется TLS.
    hostname: Mapped[str] = mapped_column(String(HOSTNAME_LENGTH), default="")
    public_ip: Mapped[str] = mapped_column(String(45), default="")

    ssh_host: Mapped[str] = mapped_column(String(HOSTNAME_LENGTH), default="")
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="")
    # Свой ключ ed25519, созданный CRM; пароль входа не хранится вовсе — им только
    # кладут ключ на сервер. NULL — ключа ещё нет.
    ssh_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_public_key: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Ключ хоста, подтверждённый человеком при первом входе. Другой ключ — отказ,
    # а не «запомнить новый»: подмена сервера выглядит именно так.
    ssh_host_key: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Пароль sudo — только если человек сам попросил его запомнить (нет docker-группы).
    sudo_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Как звать docker на машине: `docker` или `sudo -n docker` — выясняет проверка.
    docker_cmd: Mapped[str] = mapped_column(String(32), default="docker")

    base_dir: Mapped[str] = mapped_column(String(255), default="")
    container: Mapped[str] = mapped_column(String(64), default="opencrm-mail")
    image: Mapped[str] = mapped_column(String(200), default=DEFAULT_IMAGE)

    # Куда CRM ходит за письмами: на этой машине — имя контейнера в сети compose,
    # по SSH — публичное имя. Сертификат при этом сверяется с `hostname`.
    imap_host: Mapped[str] = mapped_column(String(HOSTNAME_LENGTH), default="")
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    # sha256 сертификата, когда он самоподписанный: доверие по отпечатку, а не
    # «не проверять». Пусто — обычная проверка цепочки и имени.
    tls_pin: Mapped[str] = mapped_column(String(64), default="")
    master_user: Mapped[str] = mapped_column(String(64), default="opencrm")
    master_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ключ к агенту внутри контейнера (вариант «на этой машине», HTTP по сети compose).
    agent_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Исходящие через посредника: без PTR и с закрытым 25-м Gmail письма не примет.
    relay_host: Mapped[str] = mapped_column(String(HOSTNAME_LENGTH), default="")
    relay_port: Mapped[int] = mapped_column(Integer, default=587)
    relay_user: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), default="")
    relay_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    installed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # NULL — ошибок нет; пустая строка была бы «ошибка без текста».
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MailDomain(Base):
    """Домен на сервере: example.com. Отсюда записи DNS, которые надо завести."""

    __tablename__ = "mail_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mail_servers.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), unique=True)
    dkim_selector: Mapped[str] = mapped_column(String(63), default="mail")
    # Значение TXT-записи `<selector>._domainkey.<domain>` — как его отдал сервер.
    dkim_txt: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Последняя проверка DNS целиком (JSON): экран показывает её, не спрашивая DNS заново.
    dns_report: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    dns_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MailJob(Base):
    """Проверка, установка, подключение или удаление сервера — с журналом по шагам.

    Журнал хранится целиком: «что пошло не так» спрашивают и через неделю после
    провала, а экран мастера показывает его тем же окном, пока работа идёт.
    """

    __tablename__ = "mail_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mail_servers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=JOB_RUNNING)
    # Шаги (JSON-список): код, состояние ok/warn/fail/running/skip, подробность.
    steps: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    log: Mapped[str] = mapped_column(LongText, default="", deferred=True)
    # Код беды для инструкции «как исправить»; пусто — беды нет.
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Работа идёт в потоке процесса; процесс перезапустили — отметка стареет, и
    # работа честно помечается прерванной, а не «идёт» вечно.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MailAlias(Base):
    """Псевдоним: письма на `source` ложатся в ящик `destination` (или уходят наружу)."""

    __tablename__ = "mail_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mail_servers.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    destination: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("server_id", "source", "destination", name="uq_mail_aliases_pair"),
    )


class MailBoxAccess(Base):
    """Кто из сотрудников работает с ящиком. Общий ящик (`is_shared`) виден и без строки."""

    __tablename__ = "mail_box_access"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Читать — да, отправлять от имени ящика — отдельно: стажёру дают посмотреть.
    can_send: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    granted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("account_id", "user_id", name="uq_mail_box_access"),)


class UserTokenMailBox(Base):
    """Ящик, которым ограничен токен с `mail_scope = "list"`.

    Пустой список при `list` — ни одного ящика, а не «все»: удалили единственный
    разрешённый ящик — токен не должен молча получить доступ ко всем остальным.
    """

    __tablename__ = "user_token_mail_boxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("user_tokens.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True
    )

    __table_args__ = (
        UniqueConstraint("token_id", "account_id", name="uq_user_token_mail_box"),
    )


class MailArchiveFolder(Base):
    """Докуда архив дочитал папку ящика: UIDVALIDITY и последний взятый UID."""

    __tablename__ = "mail_archive_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id", ondelete="CASCADE"))
    folder: Mapped[str] = mapped_column(ExactString(FOLDER_LENGTH))
    # 32 бита без знака: в знаковый INT влезают не всегда.
    uidvalidity: Mapped[int] = mapped_column(BigInteger, default=0)
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    # UIDNEXT сервера при последнем проходе: last_uid + 1 < uidnext — папка не дочитана.
    uidnext: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("account_id", "folder", name="uq_mail_archive_folders_box_folder"),
    )


class MailArchiveMessage(Base):
    """Копия письма в CRM: переживает удаление на сервере и гибель самого сервера.

    Само письмо (с вложениями) — файлом `.eml.gz` в storage: в MySQL ему не место,
    35 МБ на строку и сотни гигабайт дампа. В строке — то, по чему ищут и что
    показывают; разбор — docs/bloki/32-pochtovyy-server.md, «Архив писем в CRM».
    """

    __tablename__ = "mail_archive"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Ящик убрали из CRM — копия остаётся с адресом, видит её управляющий почтой.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="SET NULL"), nullable=True
    )
    address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    folder: Mapped[str] = mapped_column(ExactString(FOLDER_LENGTH))
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[str] = mapped_column(ExactString(MESSAGE_ID_LENGTH), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    from_addr: Mapped[str] = mapped_column(String(ADDRESS_LENGTH), default="", index=True)
    to_addrs: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    cc_addrs: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Date: письма в UTC; NULL — заголовка нет или он кривой.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # INTERNALDATE: когда письмо легло в ящик. По нему список и сортируется.
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    flags: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON: [{part, name, size, content_type, inline}] — сами байты в сыром файле.
    attachments: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    body_text: Mapped[str] = mapped_column(LongText, default="", deferred=True)
    # Уже очищенный nh3, как в веб-почте: показывается без второго разбора.
    body_html: Mapped[str] = mapped_column(LongText, default="", deferred=True)
    # Путь от корня storage; пусто — сырой файл не лёг (нет места), строка всё равно есть.
    raw_path: Mapped[str] = mapped_column(String(500), default="")
    raw_sha256: Mapped[str] = mapped_column(String(64), default="")
    # Письма больше нет на сервере. Копия при этом НЕ удаляется — ради неё архив и заведён.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "account_id", "folder", "uidvalidity", "uid", name="uq_mail_archive_box_folder_uid"
        ),
        Index("ix_mail_archive_box_folder_received", "account_id", "folder", "received_at"),
        Index("ix_mail_archive_box_sent", "account_id", "sent_at"),
        Index("ix_mail_archive_message_id", "message_id"),
    )
