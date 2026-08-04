from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base


class SnakeScore(Base):
    """Результат партии в змейку со страницы обслуживания.

    Таблицу пополняют без входа в систему — играют посетители, а не сотрудники.
    Поэтому здесь же лежит хэш адреса: по нему ограничивается частота отправки,
    а в открытом виде адрес не хранится (та же соль, что у просмотров витрины).

    `played_ms` хранится не ради статистики: по нему проверяется, что счёт вообще
    достижим за это время. Оставляем в базе, чтобы подозрительную запись можно
    было разобрать потом, а не только отклонить в момент приёма.
    """

    __tablename__ = "snake_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(24), default="")
    score: Mapped[int] = mapped_column(Integer, index=True)
    played_ms: Mapped[int] = mapped_column(Integer, default=0)
    ip_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
