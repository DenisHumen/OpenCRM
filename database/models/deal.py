from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.query import search_norm
from database.session import Base
from database.types import LongText, text_default

# Этапов здесь нет: они в `pipeline_stages` (database/models/pipeline.py) — у
# ремонта «диагностика», у салона «клиент пришёл», общего списка не существует.
# Название настраивается, тип (`open`/`won`/`lost`) фиксирован: по нему и считают
# отчёты, поэтому воронка и конверсия работают при любых названиях.


class Deal(Base):
    """Сделка — работа для клиента от заявки до закрытия.

    Между «клиент появился» и «доска с результатом» не было ничего: отчёты не из
    чего считать, письма и звонки некуда привязывать, кроме клиента вообще.
    """

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # От чьего имени ведём работу. Пусто — «от фирмы по умолчанию», а не
    # «неизвестно». SET NULL: CASCADE снёс бы заявки вместе с фирмой, RESTRICT
    # запретил бы удалять любую фирму, которой пользовались; выданные бланки не
    # меняются — там снимок реквизитов, а не ссылка.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Ключ этапа из `pipeline_stages`. Не внешний ключ намеренно: этап могут
    # заархивировать, а сделка обязана остаться читаемой.
    stage: Mapped[str] = mapped_column(String(32), index=True)
    # Порядок внутри колонки канбана: без него карточки прыгают при каждом
    # обновлении списка.
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    # Деньги — целыми в минимальных единицах: на float сумма по колонке канбана
    # расходится с суммой карточек на копейку. Валюта одна на систему (настройка
    # currency): без курса на дату разные валюты не сложить. None и 0 разные
    # состояния: «сумму ещё не назвали» и «работа бесплатная».
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Сколько уже получено. Остаток не храним — он всегда amount минус это, и
    # третье поле начало бы расходиться с первыми двумя при любой правке.
    prepaid: Mapped[int] = mapped_column(Integer, default=0)

    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Причина отказа. Без неё отчёт по потерям показывает только число и ничем
    # не помогает: непонятно, дорого было, долго или клиент просто пропал.
    lost_reason: Mapped[str] = mapped_column(String(200), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Склейка названия и описания; приём и почему нет индекса — у той же колонки
    # в `client.py`, потолок TEXT и литерал 1101 — в `database/types.py`.
    # Сторож переносимости — `tests/test_mysql_portability.py`.
    search_text: Mapped[str] = mapped_column(
        LongText, default="", server_default=text_default(), deferred=True
    )

    # Индексы объявлены ниже, за классом: у одного хвост идёт по УБЫВАНИЮ, а
    # убывание строкой в `__table_args__` не записать.


# Три пары под три способа смотреть на заявки; замеры «было/стало» — в миграции
# b3f18d5a2e47. `deleted_at` стоит первым не для отбора, а чтобы не переманивать
# отчёты: индекс со `stage` в голове уводит их с окна по `closed_at` на этапы.

# Список «живые, свежее сверху»: без пары — временное дерево на 394 000 строк
# ради пятидесяти (442 мс первая страница, 651 мс пятидесятая), с парой —
# обратный проход по индексу (49 и 51 мс).
Index("ix_deals_alive_updated", Deal.deleted_at, Deal.updated_at)
# Колонка канбана. Хвост `id DESC` не украшение: `by_stage` сортирует
# `sort_order ASC, id DESC`, и без него MySQL всё равно сортирует 97 000 строк.
# Колонка 185 → 3.5 мс, доска целиком 1243 → 172 мс.
Index(
    "ix_deals_alive_stage_sort",
    Deal.deleted_at,
    Deal.stage,
    Deal.sort_order,
    Deal.id.desc(),
)
# Счётчики и суммы по этапам: `amount` третьей колонкой делает индекс покрывающим
# (SUM 357 → 72 мс, COUNT 331 → 58 мс). Нужен ВМЕСТЕ с предыдущим: в одиночку тот
# берётся и на суммы, но `amount` в нём нет — 496 мс, хуже, чем без индексов.
Index("ix_deals_alive_stage_amount", Deal.deleted_at, Deal.stage, Deal.amount)


def _sklejka_zayavki(deal: "Deal") -> str:
    """Из чего собирается поисковая склейка заявки.

    Имени клиента здесь нет: оно меняется без ведома заявки и делало бы склейку
    сорока его заявок неправдой. Поиск по нему — подзапросом по
    `clients.search_text` (`database/repositories/deals.py`).
    """
    return search_norm(deal.title, deal.description)


@event.listens_for(Deal, "before_insert", propagate=True)
@event.listens_for(Deal, "before_update", propagate=True)
def _peresobrat_poisk_zayavki(mapper, connection, target: "Deal") -> None:
    target.search_text = _sklejka_zayavki(target)


class DealStageChange(Base):
    """Кто и когда передвинул сделку по этапам.

    Из этого журнала считается «сколько сделка простояла в этапе» — единственный
    отчёт, показывающий, где именно затык, а не сколько сделок потеряли.
    """

    __tablename__ = "deal_stage_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    # Ключи этапов, а не свободный текст: та же величина и ширина (32), что у
    # `pipeline_stages.key` — `_free_key` выдаёт до 27 символов, а обрежься ключ
    # при записи, воронка молча покажет этапу ноль входов. Пусто у первой записи.
    from_stage: Mapped[str] = mapped_column(String(32), default="")
    to_stage: Mapped[str] = mapped_column(String(32))
    changed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class DealLine(Base):
    """Строка заявки: товар со склада или своя трата.

    Одна таблица на то и другое, а не две. Две означали бы, что сумма заявки
    складывается из двух источников, и первый же отчёт забудет один из них.
    Вид строки не хранится, а выводится: `product_id` пуст — своя трата
    (упаковка, доставка, работа без номенклатуры); заполнен, а у товара
    `is_service` — услуга из прайса, денег добавляет, склада не трогает;
    заполнен у обычного товара — бронирует и списывается.

    Разбор целиком — `docs/19-sborka-zakaza.md` §Р2.
    """

    __tablename__ = "deal_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE: строка — часть заявки, а не история. Штатное удаление заявки
    # мягкое (`deals.deleted_at`), так что ключ срабатывает только на прямом
    # DELETE в базе.
    deal_id: Mapped[int] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    # RESTRICT, как у движения склада: удалить товар вместе со строками
    # проданных заявок значит переписать историю продаж. NULL — своя трата.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # С какого склада берём. NULL у своих трат и услуг.
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Снимок названия: товар переименуют, а проданная заявка обязана остаться
    # такой, какой её подписал клиент.
    name_snapshot: Mapped[str] = mapped_column(String(200))
    quantity_milli: Mapped[int] = mapped_column(Integer)
    # Цена за единицу в минорных единицах. NULL — «не назвали», это не ноль:
    # ноль означал бы «отдаём бесплатно».
    price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Снимок себестоимости на момент добавления — для прибыли по заявке.
    cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # «Кто держит этот товар» — запрос от товара к заявкам. Без пары карточка
    # товара перебирала бы все строки всех заявок.
    __table_args__ = (Index("ix_deal_lines_product_deal", "product_id", "deal_id"),)
