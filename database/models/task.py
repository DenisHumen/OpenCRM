from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import LongText, text_default


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
    # Важность одним из четырёх слов. Не число: «важность 3» через полгода
    # никто не прочтёт, а `urgent` читается и в базе, и в журнале.
    #
    # Без индекса: значений четыре, и по такому столбцу MySQL всё равно идёт
    # перебором, а сортировка вдобавок считается выражением `CASE`.
    vazhnost: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal")
    # Подробности: что именно сделать, с чем сверяться, куда звонить.
    # Заголовок в 300 знаков — строка списка, а сюда кладут разбор.
    #
    # `LongText`, а не `Text`: 65 535 БАЙТ обычного TEXT — это всего 16 тысяч
    # эмодзи, и разбор с картинками из мессенджера обрезался бы молча.
    # `deferred`: в списке из двухсот строк подробности не нужны — там от них
    # спрашивают только «есть ли», а весят они до потолка каждая.
    note: Mapped[str] = mapped_column(
        LongText, default="", server_default=text_default(), deferred=True
    )

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


#: Важность, от срочного к низкому. Порядок важен: по нему сортируют список.
VAZHNOSTI = ("urgent", "high", "normal", "low")
VAZHNOST_PO_UMOLCHANIYU = "normal"


class TaskFile(Base):
    """Снимок или видео, приложенные к напоминанию.

    Файл на диске, в базе след — то же решение, что у вложений бумаг: снимок
    «что привезли» и видео «как гудит» отвечают на вопрос быстрее описания.
    """

    __tablename__ = "task_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    file_uid: Mapped[str] = mapped_column(String(64), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
