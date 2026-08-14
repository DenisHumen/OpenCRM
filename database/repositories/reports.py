"""Агрегаты отчётов: воронка, выручка, источники.

Всё здесь считается ЗАПРОСОМ. Отчёт по году — это десятки тысяч строк, и
загрузить их, чтобы сложить в Python, значит съесть память ради числа, которое
база отдаёт одной строкой. Ошибка при этом была бы тихой: на тестовой базе всё
быстро, а на боевой отчёт просто не открывается.

Вид этапа (`open`/`won`/`lost`), а не его название — единственная ось, по
которой можно считать. Названия у каждого дела свои: «Выдано», «Оплачено»,
«Договор подписан» — это всё один и тот же `won`, а «Готово» у одного бизнеса
открытый этап, у другого выигранный.
"""

from datetime import datetime

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from database.models import Client, Deal, DealStageChange
from database.models.pipeline import CLOSED_KINDS, KIND_LOST
from database.repositories import pipeline as pipeline_repo


def _mine(only_manager_id: int | None):
    """Сужение отчёта до своих заявок — одним условием на все запросы файла.

    `deals.view_others` решает, ЧЬИ заявки вообще считаются, а не только какие
    карточки видно на экране. Иначе запрет декоративный: список показывает три
    заявки, а отчёт рядом — оборот всей фирмы, и узнать его как раз и было
    целью. Хочешь общие цифры сотруднику — выдай ему это право явно.

    Возвращаем кортеж условий, а не одно: пустой распаковывается в `where`
    бесследно, и ни один запрос не приходится писать в двух вариантах.
    """
    return () if only_manager_id is None else (Deal.manager_id == only_manager_id,)


def _my_clients(only_manager_id: int | None):
    """То же сужение, но по колонке клиента.

    Строка отчёта по источникам складывается из клиентов и из заявок. Пока
    сужались только заявки, в одной строке стояли числа с разным охватом:
    «клиентов» — по всей фирме, «выиграно» — по своим. Поделить одно на другое
    было нельзя, а выглядела строка обычной.

    Это про АРИФМЕТИКУ строки, а не про доступ к карточкам клиентов: «видит
    только своих клиентов» в систему не входит осознанно, и почему — написано в
    `permissions_service.deals_scope`. Здесь мы не прячем клиента, а считаем ту
    же совокупность, по которой посчитаны соседние колонки; иначе строку нельзя
    прочитать целиком.
    """
    return () if only_manager_id is None else (Client.manager_id == only_manager_id,)


def _closed_stages(db: Session) -> tuple[dict[str, str], list[str]]:
    """Справочник «ключ → тип» и ключи закрытых этапов — одним чтением на отчёт.

    Здесь кончилось соединение `deals JOIN pipeline_stages`. Отчёты отбирают
    заявки узким окном по `closed_at` — месяц из трёх лет, два процента
    таблицы, — и пока справочник стоял в запросе, планировщик норовил вести
    отчёт от него: пять строк наружу и десятки тысяч заявок на каждую. Разбор и
    замеры — в докстроке `pipeline_repo.kinds_by_key`.

    Тип этапа после этого приписывается в Python, по ключу из той же выдачи.
    Множество найденного не меняется ни на строку: `key` в справочнике
    уникален, поэтому внутреннее соединение и `stage IN (ключи)` отбирают одно
    и то же, а заявка с этапом, которого в справочнике нет, отсеивается и там,
    и там.
    """
    kinds = pipeline_repo.kinds_by_key(db)
    return kinds, [key for key, kind in kinds.items() if kind in CLOSED_KINDS]


def stage_entries(
    db: Session, start: datetime, end: datetime, only_manager_id: int | None = None
) -> dict[str, int]:
    """Сколько РАЗНЫХ сделок вошло в каждый этап за период.

    Считаем по журналу перемещений, а не по текущему этапу сделок. Текущий этап
    отвечает на вопрос «где всё лежит сейчас», а воронка — на «сколько прошло
    через каждый шаг»: сделка, ушедшая из «Согласования» в «Работу», обязана
    остаться в числе тех, кто через согласование прошёл.

    DISTINCT обязателен: сделку возвращают на предыдущий этап и двигают снова,
    и без него один и тот же клиент считался бы за двоих ровно там, где работа
    шла тяжелее всего.
    """
    rows = db.execute(
        select(DealStageChange.to_stage, func.count(func.distinct(DealStageChange.deal_id)))
        .join(Deal, Deal.id == DealStageChange.deal_id)
        .where(
            Deal.deleted_at.is_(None),
            *_mine(only_manager_id),
            DealStageChange.changed_at >= start,
            DealStageChange.changed_at < end,
        )
        .group_by(DealStageChange.to_stage)
    ).all()
    return {stage: int(count or 0) for stage, count in rows}


def closed_by_kind(
    db: Session, start: datetime, end: datetime, only_manager_id: int | None = None
) -> dict[str, int]:
    """Сколько сделок ЗАКРЫТО за период, по видам этапов.

    Ось та же, что у выручки и у таблицы источников: дата закрытия сделки плюс
    вид этапа, в котором она стоит. Одно «Выиграно» на весь экран получается
    только так.

    Считать журналом перемещений («сколько раз входили в выигранный этап») здесь
    нельзя, хотя соблазн есть — рядом стоит `stage_entries`, и он именно такой.
    Сделку возвращают из выигранного этапа в работу, и дата закрытия при этом
    обнуляется (`deal_service.move`): по журналу она навсегда остаётся
    выигранной, по факту — снова в работе. Два числа на одном экране начинали
    расходиться ровно с этого возврата, причём молча.

    `stage_entries` при этом остаётся журнальным, и это не противоречие:
    воронка отвечает на «сколько прошло через шаг», а плитка «Выиграно» — на
    «сколько выиграли». Разные вопросы, и подписи у них обязаны быть разные.
    """
    kinds, closed = _closed_stages(db)
    if not closed:
        return {}
    rows = db.execute(
        select(Deal.stage, func.count())
        .where(
            Deal.deleted_at.is_(None),
            *_mine(only_manager_id),
            Deal.stage.in_(closed),
            Deal.closed_at >= start,
            Deal.closed_at < end,
        )
        .group_by(Deal.stage)
    ).all()
    itog: dict[str, int] = {}
    for stage, count in rows:
        itog[kinds[stage]] = itog.get(kinds[stage], 0) + int(count or 0)
    return itog


def money_by_month(
    db: Session,
    buckets: list[tuple[datetime, datetime]],
    only_manager_id: int | None = None,
) -> dict[tuple[int, str], dict[str, int]]:
    """Деньги закрытых сделок по месяцам и видам этапов — одним запросом.

    Месяцы приходят готовыми границами в UTC, а не считаются в SQL: границу
    месяца задаёт календарь и зона того, кто смотрит отчёт, а не сервер.
    Выражение `strftime('%Y-%m', …)` посчитало бы месяц по UTC и увело бы
    закрытые вечером 31-го числа сделки в следующий месяц у половины часовых
    поясов — да ещё и было бы диалектным, чего проект себе не позволяет.

    Ключ результата — (номер месяца в списке, вид этапа).
    """
    if not buckets:
        return {}
    kinds, closed = _closed_stages(db)
    if not closed:
        return {}

    # CASE по границам месяцев: ANSI, переносится на MySQL без правок, и всё
    # ещё один запрос, а не по одному на месяц.
    bucket = case(
        *[
            (and_(Deal.closed_at >= start, Deal.closed_at < end), index)
            for index, (start, end) in enumerate(buckets)
        ],
        else_=None,
    )
    rows = db.execute(
        select(
            bucket.label("bucket"),
            Deal.stage,
            func.count(),
            # count(amount) не считает NULL — и это здесь главное. «Сумму не
            # назвали» и «работа бесплатная» — разные состояния; взяв в
            # знаменатель все сделки, средний чек тихо занижаешь.
            func.count(Deal.amount),
            func.coalesce(func.sum(Deal.amount), 0),
        )
        .where(
            Deal.deleted_at.is_(None),
            *_mine(only_manager_id),
            Deal.stage.in_(closed),
            Deal.closed_at >= buckets[0][0],
            Deal.closed_at < buckets[-1][1],
        )
        .group_by(bucket, Deal.stage)
    ).all()

    # Складываем этапы в тип уже здесь. Складывать приходится потому, что типов
    # меньше, чем этапов: «Выдано» и «Оплачено» — оба `won`, и раньше их
    # складывала группировка по `kind` в SQL. Сумма от места сложения не
    # меняется, а план запроса — меняется сильно.
    result: dict[tuple[int, str], dict[str, int]] = {}
    for index, stage, count, priced, total in rows:
        if index is None:  # сделка вне всех месяцев — быть не должно, но и врать не будем
            continue
        klyuch = (int(index), kinds[stage])
        yacheyka = result.setdefault(klyuch, {"count": 0, "priced": 0, "total": 0})
        yacheyka["count"] += int(count or 0)
        yacheyka["priced"] += int(priced or 0)
        yacheyka["total"] += int(total or 0)
    return result


def clients_by_source(
    db: Session, start: datetime, end: datetime, only_manager_id: int | None = None
) -> dict[str | None, int]:
    """Сколько клиентов пришло с каждого источника за период.

    Мягко удалённые не считаются — как и в сводке на дашборде: клиент в корзине
    ещё не удалён окончательно, но клиентом уже не является.

    Сужается тем же правом, что и заявки в соседних колонках той же строки. Без
    этого «клиентов» считалось по всей фирме, а «выиграно» рядом — по своим:
    делимое и делитель из разных множеств, и строка отчёта переставала быть
    строкой.
    """
    rows = db.execute(
        select(Client.source, func.count())
        .where(
            Client.deleted_at.is_(None),
            *_my_clients(only_manager_id),
            Client.created_at >= start,
            Client.created_at < end,
        )
        .group_by(Client.source)
    ).all()
    return {source: int(count or 0) for source, count in rows}


def closed_deals_by_source(
    db: Session, start: datetime, end: datetime, only_manager_id: int | None = None
) -> dict[tuple[str | None, str], dict[str, int]]:
    """Закрытые за период сделки в разрезе источника клиента и вида этапа.

    Мягко удалённых клиентов здесь НЕ отсеиваем, в отличие от их подсчёта выше.
    Иначе сумма по источникам перестала бы сходиться с общей выручкой за тот же
    период, а расхождение в двух числах на одном экране — самый быстрый способ
    перестать верить отчёту целиком. Деньги были получены; то, что карточку
    клиента потом убрали, их не отменяет.
    """
    kinds, closed = _closed_stages(db)
    if not closed:
        return {}
    rows = db.execute(
        select(
            Client.source,
            Deal.stage,
            func.count(),
            func.count(Deal.amount),
            func.coalesce(func.sum(Deal.amount), 0),
        )
        .join(Client, Client.id == Deal.client_id)
        .where(
            Deal.deleted_at.is_(None),
            *_mine(only_manager_id),
            Deal.stage.in_(closed),
            Deal.closed_at >= start,
            Deal.closed_at < end,
        )
        .group_by(Client.source, Deal.stage)
    ).all()
    itog: dict[tuple[str | None, str], dict[str, int]] = {}
    for source, stage, count, priced, total in rows:
        yacheyka = itog.setdefault(
            (source, kinds[stage]), {"count": 0, "priced": 0, "total": 0}
        )
        yacheyka["count"] += int(count or 0)
        yacheyka["priced"] += int(priced or 0)
        yacheyka["total"] += int(total or 0)
    return itog


def lost_reasons(
    db: Session, start: datetime, end: datetime, limit: int = 10,
    only_manager_id: int | None = None,
) -> list[dict]:
    """Почему не сложилось. Без этого отчёт по потерям — одно число.

    Пустая причина в список не попадает: строка «(не указано): 12» ничего не
    объясняет, а место занимает.
    """
    poteryannye = pipeline_repo.keys_of_kinds(db, (KIND_LOST,))
    if not poteryannye:
        return []
    rows = db.execute(
        select(Deal.lost_reason, func.count())
        .where(
            Deal.deleted_at.is_(None),
            *_mine(only_manager_id),
            Deal.stage.in_(poteryannye),
            Deal.lost_reason != "",
            Deal.closed_at >= start,
            Deal.closed_at < end,
        )
        .group_by(Deal.lost_reason)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [{"reason": reason, "count": int(count or 0)} for reason, count in rows]
