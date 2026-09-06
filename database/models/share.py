from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String

from database.types import ExactString
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), index=True
    )
    # Побайтно: по токену пускают на витрину, а регистронезависимое сравнение
    # приравняло бы токены, отличающиеся регистром, — и удешевило перебор.
    token: Mapped[str] = mapped_column(ExactString(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pin_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ShareView(Base):
    __tablename__ = "share_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    share_link_id: Mapped[int] = mapped_column(
        ForeignKey("share_links.id", ondelete="CASCADE"), index=True
    )
    viewed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    # Часовой пояс браузера гостя — сырой признак места (`Europe/Kyiv`).
    # Разрешения он не спрашивает и точнее города не бывает; координаты
    # выводятся при чтении (docs/bloki/25-globus.md §5.2).
    tz: Mapped[str] = mapped_column(String(64), default="", server_default="")
