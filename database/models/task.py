from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import LongText, text_default

#: Пояс по умолчанию — тот же, по которому считает утренняя сводка.
POYAS_PO_UMOLCHANIYU = "Europe/Kyiv"


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
    #
    # У повторяющегося — ближайший НЕзакрытый раз: закрыли — срок уехал дальше.
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    #: Срок без часа («в пятницу»): звонит в `DEN_CHAS` по поясу напоминания.
    ves_den: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    #: Пояс, в котором повтор считает «каждый день в 9:00» (docs/bloki/29 §4).
    poyas: Mapped[str] = mapped_column(String(64), default=POYAS_PO_UMOLCHANIYU, server_default=POYAS_PO_UMOLCHANIYU)

    # Повтор — правило RFC 5545 (RRULE) без DTSTART: DTSTART лежит рядом.
    povtor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    povtor_nachalo: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: «Через N дней после выполнения», а не по календарю (как `every!` у Todoist).
    povtor_posle: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    #: Сколько раз закрыли повторяющееся — для COUNT в режиме «после выполнения».
    sdelano_raz: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    #: За сколько минут до срока звонить, через запятую: «0,15,1440». Пусто — молча.
    opovesheniya: Mapped[str] = mapped_column(String(64), default="0", server_default="0")
    #: Настойчивое: после срока звонить снова каждые N минут, пока не отметят.
    nastoychivo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Общая полка: видно всем с `tasks.view`, берёт любой. Так приходят заявки с сайта.
    obshchee: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Привязки к карточкам других блоков: у каждой свой ключ, чтобы база сама
    # держала целостность. Клиент и заявка уносят напоминание с собой (так было
    # всегда); бумага, товар и доска — только отвязывают: напоминание остаётся.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=True, index=True
    )
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL", name="fk_tasks_document_id"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL", name="fk_tasks_product_id"), nullable=True, index=True
    )
    board_id: Mapped[int | None] = mapped_column(
        ForeignKey("boards.id", ondelete="SET NULL", name="fk_tasks_board_id"), nullable=True, index=True
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


class TaskMember(Base):
    """Человек при напоминании: владелец, получатель звонка или наблюдатель.

    Роль — два признака, а не слово: владелец, поставивший напоминание другому,
    может получать звонок и сам («мне тоже»), а слово держало бы одно из двух.
    Наблюдатель — строка без обоих: видит и узнаёт о выполнении, но не звонит.
    """

    __tablename__ = "task_members"
    __table_args__ = (UniqueConstraint("task_id", "user_id", name="uq_task_members_task_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vladelets: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    poluchaet: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    #: «Отложить» — у каждого своё: один отложил, другому звонит как звонило.
    otlozheno_do: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TaskSignal(Base):
    """Сработавший звонок: кому, про какой раз и в какую минуту.

    Уникальность (напоминание, человек, минута) — и есть защита от двойного
    звонка: процессов несколько, и второй, взявшийся за ту же минуту, упрётся
    в ключ, а не позвонит ещё раз (docs/bloki/29 §6).
    """

    __tablename__ = "task_signals"
    __table_args__ = (
        UniqueConstraint("task_id", "user_id", "moment", name="uq_task_signals_task_user_moment"),
        Index("ix_task_signals_user_prinyato", "user_id", "prinyato"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: Минута звонка (UTC) и срок того раза, о котором звонок.
    moment: Mapped[datetime] = mapped_column(DateTime)
    srok: Mapped[datetime] = mapped_column(DateTime)
    #: `early` — заранее, `due` — в срок, `nag` — настойчивый повтор, `snooze` — после «отложить».
    vid: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    #: Человек увидел («понял», «готово», «отложить»): настойчивость смолкает.
    prinyato: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TaskEvent(Base):
    """История напоминания: кто завёл, кто закрыл какой раз, кто отложил."""

    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_created", "task_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: created, done, reopened, skipped, snoozed, assigned, missed (раз прошёл незакрытым).
    vid: Mapped[str] = mapped_column(String(16))
    #: О каком разе речь — у повторяющегося их много.
    srok: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TaskStep(Base):
    """Шаг внутри напоминания: «позвонить → выставить счёт → отвезти»."""

    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(300))
    sdelan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    poryadok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TaskUrl(Base):
    """Ссылка при напоминании: договор в облаке, страница поставщика, трекинг."""

    __tablename__ = "task_urls"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    poryadok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
