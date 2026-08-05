from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

NOTE_KINDS = ("note", "call", "meeting", "email")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    messenger: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")  # comma-separated (MVP)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ClientNote(Base):
    __tablename__ = "client_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Вид записи: note | call | meeting | email. Поле было заведено сразу, но
    # не использовалось — теперь это ось ленты: письма, звонки и заметки
    # попадают в один поток, а не в три отдельных журнала. Решение принимается
    # ДО почты и телефонии: склеивать три журнала потом больно.
    kind: Mapped[str] = mapped_column(String(16), default="note")
    # Входящее или исходящее. Пусто у заметки: у неё направления нет, и «нет
    # направления» — это не то же самое, что «входящее».
    direction: Mapped[str] = mapped_column(String(3), default="")
    # Заявка, к которой относится запись. Необязательная: «клиент звонил
    # спросить про цены» бывает и без заявки. Клиент при этом обязателен —
    # запись в ленте всегда о ком-то.
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text)
    happened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClientFile(Base):
    __tablename__ = "client_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    file_uid: Mapped[str] = mapped_column(String(64), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
