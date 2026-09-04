"""Уведомление сотруднику: «в системе случилось вот что, посмотри».

Строка на человека, а не на событие: одно событие рождает по строке каждому,
кому оно адресовано, и каждый читает своё. Текста здесь нет — только `kind` и
`params`: подпись собирает экран на языке читателя (`i18n.ts`, ключи `ntf*`),
а сервер данных по-английски не пишет ничего, кроме имени вида.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # Колокольчик спрашивает «сколько непрочитанных у меня» на каждом
        # намёке, список — «мои по свежести»: обе выборки идут от user_id.
        Index("ix_notifications_user_read", "user_id", "read_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Вид: `order_closed`, `waybill_posted`, `auto_waybill`… — ключ подписи.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Подстановки для подписи, JSON: номер бумаги, название заявки, этап.
    params: Mapped[str] = mapped_column(Text, default="{}")
    #: Куда вести по нажатию, путь внутри приложения.
    link: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
