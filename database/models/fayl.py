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

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base


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
