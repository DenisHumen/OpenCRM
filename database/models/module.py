from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base


class ModuleState(Base):
    """Включён ли блок системы у этого бизнеса.

    В таблице только состояние. Какие блоки вообще бывают, знает код
    (core/modules.py): строка, оставшаяся от снесённого блока, не должна
    «включать» то, чего больше нет, а новый блок обязан появляться со своим
    значением по умолчанию, без правки базы руками.

    Кто и когда переключил — не украшение: выключенный блок пропадает из меню
    целиком, и на вопрос «куда делся склад» нужен ответ, а не догадки.
    """

    __tablename__ = "module_states"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
