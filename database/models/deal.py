from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

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
    # Индекса «живые, свежие сверху» здесь намеренно нет, хотя список заявок
    # просит именно его и на тридцати тысячах строк сортирует всю выборку во
    # временном дереве (12–17 мс на страницу). Составной `(deleted_at,
    # updated_at)` этот запрос ускоряет в шестьдесят раз и ровно во столько же
    # замедляет сводку и отчёты: `sqlite_stat1` считает `deleted_at IS NULL`
    # избирательным условием (среднее 60 строк на значение при настоящих
    # 29 500), и планировщик тащит такой индекс в каждый запрос про живые
    # записи. Разбор вариантов и замеры — в миграции f9b41c7e2d08.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


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
