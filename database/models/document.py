from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Виды бланков. Пока один — приём вещи в работу; список расширяется, когда
# появится, скажем, акт выдачи.
KIND_INTAKE = "intake"
DOCUMENT_KINDS = (KIND_INTAKE,)

# Состояния документа. Отдельно от этапа сделки намеренно: сделка живёт в
# воронке, которую каждый бизнес настраивает под себя, а документ — бумага с
# коротким и общим для всех жизненным циклом. Смешай их — и скан квитанции
# начнёт двигать сделку по чужой воронке.
STATUS_ISSUED = "issued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_READY = "ready"
STATUS_CLOSED = "closed"
STATUS_CANCELLED = "cancelled"
DOCUMENT_STATUSES = (
    STATUS_ISSUED,
    STATUS_IN_PROGRESS,
    STATUS_READY,
    STATUS_CLOSED,
    STATUS_CANCELLED,
)

DOCUMENT_LOCALES = ("ru", "en", "uk")


class Document(Base):
    """Бланк, выданный на руки: половина клиенту, половина остаётся в мастерской.

    Хранится не картинкой, а данными: снимком того, что было напечатано, плюс
    номером и состоянием. Так бланк можно перепечатать на другом языке, найти
    сканом и провести по состояниям, ничего не теряя.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Номер человеку и штрихкоду: «2026-000123». Он же попадает в Code128 и в
    # адрес внутри QR — второго идентификатора заводить незачем.
    number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default=KIND_INTAKE)
    locale: Mapped[str] = mapped_column(String(2), default="ru")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ISSUED, index=True)

    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Снимок данных на момент выдачи, JSON. Именно снимок, а не ссылки на
    # клиента и сделку: у человека на руках бумага, и она обязана совпадать с
    # тем, что в базе, даже если клиента потом переименовали, телефон исправили,
    # а сделку удалили. Иначе спор «что вы мне выдали» решать нечем.
    payload: Mapped[str] = mapped_column(Text, default="{}")

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DocumentEvent(Base):
    """Что с бланком делали: выдали, приняли в работу, отдали.

    Нужен не для красоты: спор о сроках («когда вы сказали, что готово»)
    разрешается только записью с временем, а не текущим состоянием.
    """

    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(16), default="")
    to_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(200), default="")
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
