from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.query import search_norm
from database.session import Base

#: Виды, которые сотрудник заводит руками.
NOTE_KINDS = ("note", "call", "meeting", "email", "telegram")

#: Виды, которые появляются сами — от подписчика на событие, а не из формы.
#: Отделены от рукописных: иначе менеджер прислал бы «смену этапа», которой не
#: было. Правки полей не сюда: лента станет журналом. Им свой экран и `audit.py`.
KIND_STAGE = "stage"
KIND_DOCUMENT = "document"
KIND_STOCK = "stock"
SYSTEM_NOTE_KINDS = (KIND_STAGE, KIND_DOCUMENT, KIND_STOCK)

MAX_SOURCE = 32

#: Заявка с сайта. Ставит его приём (`core/services/lead_service.py`), а имя
#: ключа живёт здесь: источник показывают карточка клиента и отчёт, и ключ,
#: известный одному лишь приёму, стоял бы в них сырым словом `site`.
SOURCE_SITE = "site"

# Откуда пришёл клиент. Справочник по умолчанию — ключами, а не словами: слова
# у каждого дела свои («сарафан», «по рекомендации», «от знакомых» — это одно и
# то же), а отчёт должен складывать их в одну строку.
CLIENT_SOURCES = ("referral", "search", "social", "ads", SOURCE_SITE, "repeat", "other")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    # Тот же телефон, приведённый к сравнимому виду (core.utils.normalize_phone).
    # Показываем и набираем введённое менеджером, а ищем по этому: без колонки
    # звонок с 067… не находил бы карточку, где записано +380 67….
    phone_norm: Mapped[str] = mapped_column(String(32), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    messenger: Mapped[str] = mapped_column(String(255), default="")
    # Четыре колонки, а не строка: индекс, город и страна нужны по отдельности.
    # Пустая строка, а не NULL, — иначе вечное `IS NULL OR = ""` в запросах.
    # Разбор — docs/19-sborka-zakaza.md §Р7.
    country: Mapped[str] = mapped_column(String(2), default="", server_default="")
    city: Mapped[str] = mapped_column(String(120), default="", server_default="")
    # Индекс почтовый, а не число: в Канаде и Британии в нём буквы.
    zip_code: Mapped[str] = mapped_column(String(20), default="", server_default="")
    address: Mapped[str] = mapped_column(String(300), default="", server_default="")
    tags: Mapped[str] = mapped_column(String(500), default="")  # comma-separated (MVP)
    # Источник — ключом, а не ссылкой на справочник (цена таблицы и почему не
    # наоборот — docs/03-database.md, «clients»). NULL и "other" не сливать:
    # «не спросили» — дыра, «другое» — ответ клиента.
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Склейка полей поиска: пять `lower() LIKE` = миллион вызовов на 200 000
    # карточек, 295 → 38,6 мс. Индекса нет: `%` спереди не берёт, а префиксный
    # бессмыслен — склейка начинается с имени; `deferred` — иначе текст вдвойне.
    search_text: Mapped[str] = mapped_column(String(1300), default="", deferred=True)

    # «Живые, свежее сверху» — открытие и листание списка клиентов: без пары
    # 196 000 живых карточек шли в сортировку (269 и 314 мс, с парой 29 и 33),
    # а отказ в f9b41c7e2d08 мерили на SQLite. Разбор — docs/03-database.md.
    __table_args__ = (Index("ix_clients_alive_updated", "deleted_at", "updated_at"),)


def _sklejka_klienta(client: "Client") -> str:
    """Из чего собирается поисковая склейка клиента.

    Ровно те поля, по которым искали пятью отдельными `OR`. `messenger` не
    входит — его не искали и раньше, а склейка обязана находить то же самое.
    """
    return search_norm(
        client.name,
        client.company,
        client.phone,
        client.phone_norm,
        client.email,
        client.tags,
    )


# Склейку пересчитывает мэппер, а не сервис (образец — `models/audit.py`):
# `phone_norm` пишет один `client_service`, мимо него она молча пуста, а тут
# пустая = карточка не найдётся. Клиентов и заявки пишут сайт, почта и АТС.


@event.listens_for(Client, "before_insert", propagate=True)
@event.listens_for(Client, "before_update", propagate=True)
def _peresobrat_poisk_klienta(mapper, connection, target: "Client") -> None:
    target.search_text = _sklejka_klienta(target)[:1300]


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
    # Новые виды — значения, а не колонки: миграция не нужна. Это ось общей ленты:
    # письма, звонки и заметки в одном потоке — склеивать три журнала потом больно.
    kind: Mapped[str] = mapped_column(String(16), default="note")
    # Входящее или исходящее. Пусто у заметки: у неё направления нет, и «нет
    # направления» — это не то же самое, что «входящее».
    direction: Mapped[str] = mapped_column(String(3), default="")
    # Заявка, к которой относится запись. Необязательная: «клиент звонил
    # спросить про цены» бывает и без заявки. Клиент обязателен — запись о ком-то.
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text)
    # Индекс — лента сортируется по этому полю, а не по `created_at`: звонок
    # вчерашний, а занесли сегодня. Без индекса она читала таблицу целиком и
    # строила дерево на сотню тысяч записей ради полусотни строк на экране.
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
