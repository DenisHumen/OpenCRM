from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Тип этапа — то немногое, что НЕ настраивается.
#
# Названия у всех разные: у ремонта «диагностика», у салона «клиент пришёл», у
# магазина «отправлен». Заставлять их жить с чужими словами нельзя. Но отчёт
# должен работать у всех одинаково, а по названию считать невозможно — значит
# считаем по типу: воронка это все `open` по порядку, конверсия — доля `won`,
# потери — `lost`. Как этапы называются, отчёту всё равно.
KIND_OPEN = "open"
KIND_WON = "won"
KIND_LOST = "lost"
STAGE_KINDS = (KIND_OPEN, KIND_WON, KIND_LOST)
CLOSED_KINDS = (KIND_WON, KIND_LOST)


class PipelineStage(Base):
    """Этап воронки. Название редактируется, тип — нет."""

    __tablename__ = "pipeline_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Ключ стабилен и не меняется при переименовании: на него ссылаются сделки.
    # Иначе переименование этапа осиротило бы все карточки в нём.
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(8), default=KIND_OPEN, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # Пусто — колонка красится по типу. Свой цвет нужен тем, кто держит на доске
    # много этапов и различает их взглядом, а не чтением.
    color: Mapped[str] = mapped_column(String(7), default="")
    # Этапы не удаляем, а прячем: на них ссылаются закрытые сделки и журнал
    # перемещений, и удаление сделало бы историю нечитаемой.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
