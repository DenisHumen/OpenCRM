"""Токен сотрудника: вход в `/api/v1` для программ и агентов (docs/bloki/30-tokeny-i-mcp.md).

Токен действует правами своего сотрудника — не шире и не уже: отдельного
набора прав у него нет, и агенту (Claude через MCP) заводят сотрудника с той
ролью, которую ему не жалко. В базе — только отпечаток: строку показывают один раз.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import ExactString


class UserToken(Base):
    __tablename__ = "user_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    #: Первые знаки строки — сличить токен в конфиге агента с записью здесь.
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(ExactString(64), unique=True, index=True)
    #: Только чтение: всё, кроме GET/HEAD, отвечает 403 — агенту «посмотреть» не нужно право менять.
    tolko_chtenie: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    #: Пусто — бессрочный; такие в списке красным.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Отзыв — отметка, строка остаётся: «был ли у нас токен для агента» должно иметь ответ.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
