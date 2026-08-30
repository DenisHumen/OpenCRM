"""Звонки: журнал АТС внутри CRM.

Одна строка — один звонок, а не событие: несколько сообщений АТС находят строку
по уникальному ``external_id``, и идемпотентность держит база, а не код.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

CALL_DIRECTIONS = ("in", "out")

# Стабильные строковые ключи: их видит и АТС в вебхуке, и фронт в фильтрах, и
# человек в базе.
CALL_OUTCOMES = ("answered", "missed", "busy", "failed", "canceled")


class PhoneCall(Base):
    __tablename__ = "phone_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(8))

    # Номера храним дважды: как прислала АТС (это показывают и по этому звонят
    # обратно) и нормализованно (по нему ищут клиента). Одного вида мало:
    # исходный не сравнивается, нормализованный теряет привычный клиенту вид.
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
    # Запись в общей ленте от этого звонка: ссылкой, чтобы повторное событие
    # правило существующую строку ленты, а не добавляло вторую. Индекс —
    # обратный путь из ленты к карточке звонка с длительностью и разговором.
    note_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_notes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # История звонков в карточке: отобрать по клиенту и показать свежие сверху.
    # По одному `client_id` вторая половина досталась бы сортировке всей выборки.
    __table_args__ = (Index("ix_phone_calls_client_started", "client_id", "started_at"),)
