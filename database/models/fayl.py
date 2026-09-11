"""Файлы модуля: свои папки и свои файлы.

**Зачем своя таблица, когда файлы уже лежат в пяти других.** Работы досок,
вложения клиента, задачи и бланка, снимки товара — это файлы ЧЕГО-ТО: они
появляются вместе с карточкой и уходят вместе с ней. Файл, который человек
принёс сам («договор аренды», «шрифты», «макет визитки»), не принадлежит ни
одной карточке, и класть его в чужую таблицу значило бы привязать к тому, к
чему он отношения не имеет.

Экран «Файлы» показывает и то, и другое: чужие файлы — деревом по их хозяевам,
свои — папками, которые заводит человек.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import ExactString


class FileFolder(Base):
    """Папка, которую завёл человек. Вложенность — через `parent_id`.

    Глубина не ограничена схемой: ограничивать её значило бы решать за
    человека, как он раскладывает свои бумаги. От петли («папка внутри себя»)
    защищает служба обходом родителей — схема такого не умеет.
    """

    __tablename__ = "file_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE: снесли папку — ушли вложенные и их файлы. Диск при этом чистит
    # служба ДО удаления строк: база о файлах на диске не знает.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StoredFile(Base):
    """Файл, принесённый через модуль. Форма та же, что у вложений карточек:
    имя на диске в `file_uid`, в базе только след."""

    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL — корень «Загрузки». Папку снесли — файлы уходят с ней: поднимать их
    # в корень значило бы молча свалить в кучу то, что человек разложил.
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("file_folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    file_uid: Mapped[str] = mapped_column(String(64), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


#: Что можно делать по ссылке. «Смотреть» без «скачать» — отдельное решение, и
#: именно оно тянет за собой защиту просмотра.
REZHIM_SMOTRET = "view"
REZHIM_SKACHAT = "download"
REZHIMY = (REZHIM_SMOTRET, REZHIM_SKACHAT)

#: Кому открыта ссылка.
#:
#: `invited` отвечает не на «как не пустить чужого», а на «кто именно смотрел»:
#: почта называется на входе, сверяется со списком и попадает и в журнал, и в
#: водяной знак поверх файла. Утёкший снимок экрана показывает, от кого он ушёл.
KRUG_SSYLKA = "link"
KRUG_KOD = "code"
KRUG_GOSTI = "invited"
KRUGI = (KRUG_SSYLKA, KRUG_KOD, KRUG_GOSTI)


class FileLink(Base):
    """Ссылка на файл наружу.

    Два внешних ключа, и ровно один заполнен: файл модуля либо работа доски.
    Полиморфной пары «вид + номер» здесь нет намеренно — она не даёт ни
    каскада, ни целостности: снесли работу, а строка ссылки осталась и ведёт в
    никуда, убрать её может только уборщик, которого надо не забыть написать.
    Появится третий источник — появится третья колонка и миграция.

    Механика та же, что у витрин досок (`share_links`): токен, срок, код,
    счётчик открытий, отзыв. Разница в `rezhim`: витрину смотрят, а файл ещё и
    скачивают.
    """

    __tablename__ = "file_links"
    __table_args__ = (
        # Ровно один источник. Здесь это проверка схемы, а не запрос с замком:
        # правило про одну строку, а не про то, сколько их всего.
        CheckConstraint(
            "(stored_file_id IS NULL) <> (work_id IS NULL)",
            name="ck_file_links_odin_istochnik",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stored_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("stored_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Побайтно, как у витрин: регистронезависимое сравнение приравняло бы
    # токены, отличающиеся регистром, и удешевило перебор.
    token: Mapped[str] = mapped_column(ExactString(64), unique=True, index=True)
    rezhim: Mapped[str] = mapped_column(String(16), default=REZHIM_SMOTRET)
    krug: Mapped[str] = mapped_column(String(16), default=KRUG_SSYLKA)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pin_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FileLinkView(Base):
    """Одно открытие ссылки. Считаем открытия и разных смотрящих: «отозвать
    ссылку» решают, глядя на эти два числа."""

    __tablename__ = "file_link_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("file_links.id", ondelete="CASCADE"), index=True
    )
    viewed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    # Чья это была почта, если ссылка для приглашённых. Пусто — круг «по
    # ссылке» или «по коду»: там смотрящий анонимен по устройству, и писать
    # сюда «аноним» значило бы выдумать имя, которого никто не называл.
    guest_email: Mapped[str] = mapped_column(String(200), default="")


class FileLinkGuest(Base):
    """Приглашённый: одна почта в списке одной ссылки.

    Своя таблица, а не строка через запятую в `file_links`: список правят по
    одному адресу, ищут по одному адресу и считают по одному адресу, а строка
    через запятую не умеет ни первого, ни второго, ни третьего — и однажды
    получает адрес с запятой внутри.

    Почта лежит приведённой к нижнему регистру: «Ivan@X.ru» и «ivan@x.ru» —
    один человек, и пустить первого, отказав второму, значило бы сделать
    список зависящим от того, как гость набрал своё имя.
    """

    __tablename__ = "file_link_guests"
    __table_args__ = (
        UniqueConstraint("link_id", "email", name="uq_file_link_guests_link_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("file_links.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
