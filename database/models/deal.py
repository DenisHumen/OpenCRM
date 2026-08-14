from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.query import search_norm
from database.session import Base
from database.types import LongText, text_default

# Этапов здесь больше нет: они живут в таблице `pipeline_stages`
# (database/models/pipeline.py). CRM рассчитана на любой малый бизнес, а у
# ремонта техники «диагностика», у салона «клиент пришёл», у магазина
# «отправлен» — общего списка названий не существует.
#
# Настраивается название, фиксируется тип этапа (`open`/`won`/`lost`): отчёты
# считаются по типу, поэтому воронка, конверсия и потери работают одинаково при
# любых названиях. `Deal.stage` хранит ключ этапа.


class Deal(Base):
    """Сделка — работа для клиента от заявки до закрытия.

    То, чего системе не хватало: между «клиент появился» и «вот доска с
    результатом» не было ничего. Отчёты не из чего считать, письма и звонки
    некуда привязывать, кроме клиента вообще.
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
    # От чьего имени ведём работу. Необязательное поле: у большинства фирма
    # одна, и спрашивать её на каждой заявке — лишний вопрос ради ответа,
    # который всегда один и тот же. Пусто означает «от фирмы по умолчанию», а
    # не «неизвестно».
    #
    # SET NULL, а не CASCADE и не RESTRICT. CASCADE снёс бы заявки вместе с
    # фирмой — потерять работу из-за правки справочника недопустимо. RESTRICT
    # запретил бы удалять фирму, которой хоть раз пользовались, то есть любую.
    # Остаётся SET NULL: заявка теряет ссылку, но живёт. Выданные по ней бланки
    # при этом не меняются — там снимок реквизитов, а не ссылка.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Ключ этапа из `pipeline_stages`. Не внешний ключ намеренно: этап могут
    # заархивировать, а сделка обязана остаться читаемой.
    stage: Mapped[str] = mapped_column(String(32), index=True)
    # Порядок внутри колонки канбана — как `sort_order` у работ на доске.
    # Без него карточки прыгали бы при каждом обновлении списка.
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    # Деньги — целыми числами в минимальных единицах (копейки, центы), а не
    # float. На дробных типах округление вылезает всегда: 0.1 + 0.2 != 0.3, и
    # сумма по колонке канбана начинает расходиться с суммой карточек на
    # копейку — в CRM это выглядит как ошибка в расчётах, а не как особенность
    # двоичных дробей.
    #
    # Валюта одна на систему (настройка currency): мультивалютность малому
    # бизнесу не нужна, а отчёты усложняет сразу и навсегда — суммировать
    # разные валюты без курса на дату нельзя.
    #
    # None и 0 различаются намеренно: «сумму ещё не назвали» и «работа
    # бесплатная» — разные состояния, и в отчёте они считаются по-разному.
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

    # Название и описание, склеенные и приведённые к нижнему регистру. Разбор
    # приёма — у той же колонки в `database/models/client.py`; здесь только то,
    # что своё.
    #
    # Тип `LongText`: `description` — это `Text`, а обычный TEXT в MySQL меряется
    # в 65 535 БАЙТАХ, кириллица по два. Склейка длинного описания с названием
    # упёрлась бы в потолок и была бы молча обрезана в нестрогом режиме — то
    # есть заявка перестала бы находиться по концу описания, ничем этого не
    # объявив. Сторож — `tests/test_mysql_portability.py`.
    #
    # `server_default` выражением, а не литералом: MySQL запрещает обычный
    # DEFAULT у TEXT (ошибка 1101) и обрывает миграцию на середине.
    #
    # Индекса нет намеренно — по той же причине, что и у клиента.
    search_text: Mapped[str] = mapped_column(
        LongText, default="", server_default=text_default(), deferred=True
    )

    # Три пары под три способа смотреть на заявки. Замеры — в миграции
    # b3f18d5a2e47, там же «было/стало» по каждой.
    #
    # 1. Список: «живые, свежее сверху». Без пары — временное дерево на 394 000
    #    строк ради пятидесяти (442 мс на первой странице, 651 мс на
    #    пятидесятой), с парой — обратный проход по индексу (49 и 51 мс).
    #
    # 2. Колонка канбана: отбор по этапу и порядок ВНУТРИ колонки. Хвост
    #    `id DESC` — не украшение: `by_stage` сортирует `sort_order ASC,
    #    id DESC`, и без обратного хвоста MySQL берёт пересечение двух узких
    #    индексов и всё равно сортирует 97 000 строк. Одна колонка: 185 → 3.5 мс,
    #    вся доска (пять колонок плюс итоги) 1243 → 172 мс.
    #
    # 3. Счётчики и суммы по этапам (итоги над колонками доски, плитки сводки):
    #    `amount` третьей колонкой делает индекс покрывающим, и запрос перестаёт
    #    ходить в таблицу за каждым числом — `SUM(amount) GROUP BY stage`
    #    357 → 72 мс, `COUNT(*) GROUP BY stage` 331 → 58 мс.
    #
    #    Третья пара обязана стоять ВМЕСТЕ со второй, а не вместо неё. Со
    #    второй в одиночку планировщик берёт её же и на суммы — а `amount` в
    #    ней нет, и он лезет в таблицу за каждым числом: 496 мс, то есть хуже,
    #    чем было вовсе без индексов. Проверено замером.
    #
    # Пары 2 и 3 начинаются с `deleted_at` не для отбора, а чтобы не
    # переманивать планы отчётов: любой индекс, у которого `stage` стоит
    # первым, уводит отчёт от узкого окна по `closed_at` на обход этапов.
    # Впрочем, сами отчёты с тех пор соединение со справочником не делают
    # вовсе — см. `database/repositories/pipeline.kinds_by_key`.
    #
    # Объявлены ниже, а не в `__table_args__`: у одного из них хвост идёт по
    # УБЫВАНИЮ, а убывание выражается только настоящей колонкой — строкой в
    # `__table_args__` его не записать.


Index("ix_deals_alive_updated", Deal.deleted_at, Deal.updated_at)
Index(
    "ix_deals_alive_stage_sort",
    Deal.deleted_at,
    Deal.stage,
    Deal.sort_order,
    Deal.id.desc(),
)
Index("ix_deals_alive_stage_amount", Deal.deleted_at, Deal.stage, Deal.amount)


def _sklejka_zayavki(deal: "Deal") -> str:
    """Из чего собирается поисковая склейка заявки.

    Имя клиента сюда НЕ входит, хотя по нему тоже ищут («что там по Ромашке»).
    Оно живёт в чужой строке и меняется без ведома заявки: переименовали
    клиента — и склейка у сорока его заявок стала неправдой, а пересчитывать её
    пришлось бы каскадом. Поиск по имени клиента идёт подзапросом по
    `clients.search_text` (`database/repositories/deals.py`).
    """
    return search_norm(deal.title, deal.description)


@event.listens_for(Deal, "before_insert", propagate=True)
@event.listens_for(Deal, "before_update", propagate=True)
def _peresobrat_poisk_zayavki(mapper, connection, target: "Deal") -> None:
    target.search_text = _sklejka_zayavki(target)


class DealStageChange(Base):
    """Кто и когда передвинул сделку по этапам.

    Пишется на каждую смену этапа. Из этого журнала потом считается «сколько
    сделка простояла в каждом этапе» — единственный отчёт, который показывает,
    где именно затык, а не просто сколько сделок потеряли.
    """

    __tablename__ = "deal_stage_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    # Ключи этапов, а не свободный текст: та же величина, что в
    # `pipeline_stages.key` и `deals.stage`, и той же ширины (32). Ширина здесь
    # не украшение — `pipeline_service._free_key` выдаёт до 27 символов, а
    # воронка отчёта склеивает `to_stage` с `pipeline_stages.key`. Обрежься ключ
    # при записи — и этап покажет ноль входов, не сказав об этом ни слова.
    # Пусто у самой первой записи: до создания этапа не было.
    from_stage: Mapped[str] = mapped_column(String(32), default="")
    to_stage: Mapped[str] = mapped_column(String(32))
    changed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
