"""Звонки: журнал АТС внутри CRM.

Одна строка — один звонок, а не одно событие: АТС присылает по звонку несколько
сообщений (начался, ответили, завершился), и все они находят свою строку по
``external_id``. Поэтому на нём уникальный индекс — идемпотентность держится
на базе, а не на аккуратности кода.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

CALL_DIRECTIONS = ("in", "out")

# Стабильные строковые ключи: их видит АТС в вебхуке, фронт в фильтрах и человек
# в базе. NULL в колонке итога — не «неизвестно навсегда», а «звонок ещё идёт».
CALL_OUTCOMES = ("answered", "missed", "busy", "failed", "canceled")


class PhoneCall(Base):
    __tablename__ = "phone_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    # идентификатор звонка у АТС; по нему события склеиваются в одну запись
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(8))

    # Номера храним дважды: как прислала АТС (это показывают человеку и по
    # этому звонят обратно) и в нормализованном виде (по нему ищут клиента).
    # Одного вида не хватает: исходный не сравнивается, нормализованный теряет
    # форматирование, к которому клиент привык.
    from_number: Mapped[str] = mapped_column(String(64), default="")
    from_number_norm: Mapped[str] = mapped_column(String(32), default="", index=True)
    to_number: Mapped[str] = mapped_column(String(64), default="")
    to_number_norm: Mapped[str] = mapped_column(String(32), default="", index=True)

    # naive UTC, как и всё остальное время в базе; АТС присылает своё местное —
    # приводит core/services/telephony_service.py
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # NULL — длительность неизвестна (звонок ещё идёт или АТС её не прислала).
    # Это не то же самое, что 0: ноль означает «соединились и сразу положили».
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL — звонок не завершён. Пропущенный (missed) отличается от отвеченного
    # нулевой длительности именно здесь, а не по duration_sec.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    recording_path: Mapped[str] = mapped_column(String(500), default="")

    # Удалили клиента — звонок остаётся: это факт, который был, и терять его
    # вместе с карточкой нельзя (SET NULL, а не CASCADE).
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Заявка необязательна: «позвонил спросить про цены» бывает и без неё, и
    # придумывать заявку под каждый звонок значит засорять воронку.
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # кто говорил из сотрудников; уволенный сотрудник обнуляется, звонок живёт
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Запись в общей ленте, созданная этим звонком. Хранится ссылкой, чтобы
    # повторное событие по тому же звонку правило существующую запись, а не
    # добавляло в ленту вторую. Индекс — обратный путь: из строки ленты к
    # карточке звонка, где длительность и запись разговора.
    note_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_notes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # История звонков в карточке клиента: отобрать по клиенту и показать
    # свежие сверху. По одному `client_id` вторая половина работы делается
    # сортировкой всей выборки.
    __table_args__ = (Index("ix_phone_calls_client_started", "client_id", "started_at"),)
