from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base


class Task(Base):
    """Напоминание: перезвонить, отправить счёт, забрать технику.

    CRM, которая не напоминает перезвонить, — записная книжка. Это самая
    дешёвая часть системы и самая заметная в ежедневной работе.

    Привязка к клиенту и заявке — необязательная и независимая. Бывает «позвонить
    Петрову» без заявки, бывает «заказать деталь» по заявке без разговора с
    клиентом, а бывает и просто «отвезти документы в банк».
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))

    # Срок. Naive UTC, как и всё остальное время в базе. Момент абсолютный:
    # «сегодня до 18:00» превращается в UTC ещё на клиенте, потому что 18:00 у
    # приёмщика в Киеве и у владельца в Варшаве — разные мгновения, и хранить
    # «18:00» без зоны значит однажды напомнить не тогда.
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Исполнитель. Пусто — задача общая, разберут с общей полки.
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=True, index=True
    )
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Отдельного поля «статус» нет: состояний ровно два, и дата закрытия
    # отвечает сразу на два вопроса — сделано ли и когда. Поле статуса рядом с
    # ней пришлось бы держать в согласии вручную.
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
