from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

ROLE_ROOT = "root"
ROLE_MANAGER = "manager"

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

LOCALES = ("en", "ru")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(120))
    #: Root или нет. Роль в смысле «набора прав» здесь больше не хранится —
    #: она в `role_id`. Признак остался отдельным полем намеренно: root не
    #: описывается набором прав, у него они все и всегда, и снять их нельзя
    #: даже случайно (см. `core/services/permissions_service.has`).
    role: Mapped[str] = mapped_column(String(16), default=ROLE_MANAGER)
    #: Должность с набором прав. Пусто — прав нет никаких, кроме общих:
    #: сотрудник входит в систему и видит пустую CRM, а не чужие данные.
    #: SET NULL, а не CASCADE: удаление роли не должно уносить с собой людей.
    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING)
    locale: Mapped[str] = mapped_column(String(8), default="en")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    avatar_path: Mapped[str] = mapped_column(String(255), default="")
    # присутствие: обновляется на активность (throttle в auth_service), переживает logout
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
