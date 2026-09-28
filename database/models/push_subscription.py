"""Подписка браузера на Web Push: куда слать звонок, когда вкладка закрыта (docs/bloki/31-web-push.md)."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import ExactString


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: Адрес службы браузера длиннее, чем уникальный индекс MySQL берёт целиком, — уникален отпечаток.
    endpoint: Mapped[str] = mapped_column(String(1000))
    endpoint_hash: Mapped[str] = mapped_column(ExactString(64), unique=True, index=True)
    p256dh: Mapped[str] = mapped_column(String(128))
    auth: Mapped[str] = mapped_column(String(64))
    #: «Chrome · Windows» — чтобы в профиле было понятно, какое устройство отключать.
    nazvanie: Mapped[str] = mapped_column(String(120), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
