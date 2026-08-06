from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

#: Виды, которые сотрудник заводит руками.
NOTE_KINDS = ("note", "call", "meeting", "email")

#: Виды, которые появляются сами — от подписчика на событие, а не из формы.
#: Отделены от рукописных намеренно: иначе менеджер мог бы прислать «смену
#: этапа», которой не было, и лента перестала бы быть свидетельством.
#:
#: Здесь только то, что человек назвал бы событием по заявке: этап сменили,
#: бумагу выдали, товар со склада ушёл. Каждая правка поля и каждый просмотр
#: сюда не идут — лента, ставшая журналом изменений, перестаёт читаться
#: целиком, и в ней тонут звонки с письмами, ради которых она делалась.
#: Полный аудит — отдельный экран и отдельное хранилище.
KIND_STAGE = "stage"
KIND_DOCUMENT = "document"
KIND_STOCK = "stock"
SYSTEM_NOTE_KINDS = (KIND_STAGE, KIND_DOCUMENT, KIND_STOCK)

MAX_SOURCE = 32

# Откуда пришёл клиент. Справочник по умолчанию — ключами, а не словами: слова
# у каждого дела свои («сарафан», «по рекомендации», «от знакомых» — это одно и
# то же), а отчёт должен складывать их в одну строку.
CLIENT_SOURCES = ("referral", "search", "social", "ads", "repeat", "other")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    # Тот же телефон, приведённый к сравнимому виду (core.utils.normalize_phone).
    # Держим рядом с исходным: показываем и набираем то, что ввёл менеджер, а
    # ищем — по этому. Без отдельной колонки звонок с 067… не находил бы карточку,
    # где записано +380 67…, и телефония «не работала» бы через раз.
    phone_norm: Mapped[str] = mapped_column(String(32), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    messenger: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[str] = mapped_column(String(500), default="")  # comma-separated (MVP)
    # Источник — колонкой с ключом, а не ссылкой на строку справочника.
    #
    # Справочник таблицей выглядит правильнее, но даёт ровно одно: редактируемое
    # название. У источника, в отличие от этапа воронки, больше ничего и нет —
    # ни вида, ни порядка, ни архивации; платить за одно название внешним
    # ключом, отдельным экраном и join'ом в каждом отчёте не за что.
    #
    # Разрастание такой выбор не запирает: ссылаться будут именно на ключ.
    # Понадобятся названия, цвета, слияние источников — рядом появится
    # `client_sources` с тем же ключом, и ни одна карточка клиента не переедет.
    # Обратный порядок (сразу таблица, потом выяснить, что ключ всё равно нужен)
    # стоил бы миграции данных.
    #
    # NULL и "other" — разные вещи, и складывать их нельзя: «не спросили,
    # откуда» — пробел в работе менеджера, «другое» — ответ клиента. Слитые
    # вместе, они превращают дыру в данных в факт о бизнесе.
    source: Mapped[str | None] = mapped_column(
        String(MAX_SOURCE), nullable=True, index=True
    )
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Индекс на дату появления: отчёт по источникам фильтрует клиентов периодом,
    # и без него отчёт за год читает таблицу целиком.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # Пары «живые + свежие сверху» здесь нет по той же причине, что и у заявок:
    # она ускоряет список и замедляет сводку с отчётами, потому что планировщик
    # SQLite переоценивает избирательность `deleted_at IS NULL`. Замеры — в
    # миграции f9b41c7e2d08.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ClientNote(Base):
    __tablename__ = "client_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Вид записи: note | call | meeting | email | stage | document | stock.
    # Новые виды — это значения, а не колонки: миграция под них не нужна. Поле было заведено
    # сразу, но не использовалось — теперь это ось ленты: письма, звонки и
    # заметки попадают в один поток, а не в три отдельных журнала. Решение
    # принимается ДО почты и телефонии: склеивать три журнала потом больно.
    kind: Mapped[str] = mapped_column(String(16), default="note")
    # Входящее или исходящее. Пусто у заметки: у неё направления нет, и «нет
    # направления» — это не то же самое, что «входящее».
    direction: Mapped[str] = mapped_column(String(3), default="")
    # Заявка, к которой относится запись. Необязательная: «клиент звонил
    # спросить про цены» бывает и без заявки. Клиент при этом обязателен —
    # запись в ленте всегда о ком-то.
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text)
    # Индекс — потому что лента сортируется именно по этому полю, а не по
    # `created_at`: звонок вчерашний, а занесли его сегодня. Без индекса общая
    # лента читала таблицу целиком и строила временное дерево на всю сотню
    # тысяч записей ради полусотни строк на экране.
    happened_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Лента почти всегда чья-то: карточки клиента и заявки открывают её с
    # фильтром. Пары «по кому + когда» превращают «найти и отсортировать» в
    # «прочитать полсотни подряд».
    __table_args__ = (
        Index("ix_client_notes_client_happened", "client_id", "happened_at"),
        Index("ix_client_notes_deal_happened", "deal_id", "happened_at"),
    )


class ClientFile(Base):
    __tablename__ = "client_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    file_uid: Mapped[str] = mapped_column(String(64), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
