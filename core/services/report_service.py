"""Отчёты: воронка, выручка, источники.

Отчёту либо верят, либо им не пользуются, поэтому здесь важнее всего три вещи,
на которых отчёты обычно и врут.

**Вид этапа, а не название.** У ремонта «Выдано», у магазина «Получен», у
агентства «Закрыта» — это всё `won`. Считать по названиям значит написать
отчёт, который работает ровно у одного бизнеса.

**Границы периода.** «За июль» обязано включать 31 июля целиком, до последней
секунды. Полуоткрытый интервал [начало; начало следующего дня) — единственный
способ не потерять сделки, закрытые вечером последнего дня, и не поймать
округление времени.

**Часовой пояс.** В базе время naive UTC, а «июль» человек понимает по своему
календарю. Браузер присылает смещение (`Date#getTimezoneOffset()`), и границы
считаются от местной полуночи. Взять UTC-полночь значило бы для Киева отрезать
три вечерних часа последнего дня и приписать три часа предыдущего месяца.

**NULL ≠ 0.** Сделка без названной суммы не попадает ни в числитель, ни в
знаменатель среднего чека. Ноль в знаменателе даёт не ноль, а прочерк: «пока
не о чем говорить», а не «работаем даром».
"""

import csv
import io
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import pipeline_service
from core.utils import now_utc
from database.models.client import CLIENT_SOURCES
from database.models.pipeline import KIND_LOST, KIND_OPEN, KIND_WON
from database.repositories import reports as reports_repo

# Дальше пяти лет период не пускаем. Это не про производительность запроса, а
# про то, что помесячная разбивка на 300 колонок нечитаема, а CASE по месяцам
# растёт вместе с ней.
MAX_MONTHS = 60


def parse_period(
    date_from: str | None, date_to: str | None, tz_offset: int = 0
) -> tuple[datetime, datetime, date, date]:
    """Даты «с» и «по» из запроса в полуоткрытый интервал UTC [start; end).

    `tz_offset` — минуты из `Date#getTimezoneOffset()` браузера: сколько нужно
    ПРИБАВИТЬ к местному времени, чтобы получить UTC (для Киева летом это -180).

    «По» разворачивается в начало СЛЕДУЮЩЕГО дня. Соблазн взять 23:59:59 велик и
    неверен: сделка, закрытая в 23:59:59.4, в отчёт бы попала, а закрытая в
    23:59:59.6 — уже нет, и объяснить это было бы нечем.
    """
    today = now_utc().date()
    start_day = _parse_day(date_from, today.replace(day=1), "date_from")
    end_day = _parse_day(date_to, today, "date_to")
    if end_day < start_day:
        raise errors.ValidationError(
            "Period end is before its start", code="bad_period"
        )
    # Смещение приходит от браузера, то есть от кого угодно: без потолка чужое
    # значение уводило бы границы отчёта на произвольную дату.
    if abs(tz_offset) > 14 * 60:
        raise errors.ValidationError("Bad timezone offset", code="bad_tz_offset")

    shift = timedelta(minutes=tz_offset)
    start = datetime.combine(start_day, datetime.min.time()) + shift
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time()) + shift
    return start, end, start_day, end_day


def _parse_day(value: str | None, fallback: date, field: str) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise errors.ValidationError(
            f"{field} must be a date like 2026-07-01", code="bad_date"
        ) from None


def month_buckets(
    start_day: date, end_day: date, tz_offset: int
) -> list[tuple[str, datetime, datetime]]:
    """Месяцы периода границами в UTC: (метка «2026-07», начало, конец).

    Месяцы нарезаются по МЕСТНОМУ календарю и только потом сдвигаются в UTC.
    Считать месяц выражением над колонкой (`strftime('%Y-%m', closed_at)`)
    нельзя дважды: оно диалектное и оно считало бы месяц по UTC.

    Пустые месяцы остаются в списке. Провал в середине года видно только тогда,
    когда месяц нарисован нулём, а не пропущен.
    """
    shift = timedelta(minutes=tz_offset)
    buckets: list[tuple[str, datetime, datetime]] = []
    cursor = start_day.replace(day=1)
    last = end_day.replace(day=1)
    while cursor <= last:
        following = _next_month(cursor)
        buckets.append(
            (
                cursor.strftime("%Y-%m"),
                datetime.combine(cursor, datetime.min.time()) + shift,
                datetime.combine(following, datetime.min.time()) + shift,
            )
        )
        if len(buckets) > MAX_MONTHS:
            raise errors.ValidationError(
                f"Period is longer than {MAX_MONTHS} months", code="period_too_long"
            )
        cursor = following
    return buckets


def _next_month(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def share(part: int, whole: int) -> float | None:
    """Доля в процентах или None, если делить не на что.

    Ноль здесь был бы враньём: «конверсия 0%» читается как «ни одна сделка не
    дошла», а верный ответ на пустом периоде — «сделок не было».
    """
    if not whole:
        return None
    return round(part * 100 / whole, 1)


# --- воронка ---

def funnel(db: Session, start: datetime, end: datetime) -> dict:
    """Сколько заявок вошло в каждый этап и сколько дошло до конца."""
    stages = pipeline_service.list_stages(db)
    entered = reports_repo.stage_entries(db, start, end)
    by_kind = reports_repo.entries_by_kind(db, start, end)

    steps: list[dict] = []
    previous: int | None = None
    for stage in stages:
        count = entered.get(stage.key, 0)
        if stage.kind == KIND_OPEN:
            # Конверсия к предыдущему шагу: именно она показывает, ГДЕ теряют,
            # а доля от начала — только сколько потеряли всего.
            step_share = share(count, previous) if previous is not None else None
            previous = count
        else:
            # Закрывающие этапы в цепочку не входят. Они стоят в воронке рядом,
            # но «из выигранных в потерянные» не переходят, и доля одного от
            # другого — просто деление двух чисел, ничего не означающее. Итог по
            # ним — общая конверсия и доля потерь ниже, от одного знаменателя.
            step_share = None
        steps.append(
            {
                "key": stage.key,
                "name": stage.name,
                "kind": stage.kind,
                "entered": count,
                "from_previous": step_share,
            }
        )

    # Знаменатель общей конверсии — вход в первый открытый этап воронки: это и
    # есть «пришла заявка». Брать сумму по всем этапам нельзя — одна сделка
    # проходит через несколько и посчиталась бы много раз.
    #
    # Из этого следует, что конверсия за период может выйти больше 100%, и это
    # не ошибка: в июле закрывают и то, что пришло в июне. Кто станет «чинить»
    # это ограничением сверху — заменит правду на красивое число; чинить надо
    # не отчёт, а вопрос, который к нему задают.
    first_open = next((s for s in stages if s.kind == KIND_OPEN), None)
    entries = entered.get(first_open.key, 0) if first_open else 0
    won = by_kind.get(KIND_WON, 0)
    lost = by_kind.get(KIND_LOST, 0)

    return {
        "stages": steps,
        "entered": entries,
        "won": won,
        "lost": lost,
        "conversion": share(won, entries),
        # Доля потерь считается от того же знаменателя, что и конверсия, иначе
        # два процента на одном экране означали бы разное.
        "loss_rate": share(lost, entries),
        "lost_reasons": reports_repo.lost_reasons(db, start, end),
    }


# --- выручка ---

def revenue(db: Session, start_day: date, end_day: date, tz_offset: int) -> dict:
    """Деньги по месяцам, по видам этапов и средний чек."""
    buckets = month_buckets(start_day, end_day, tz_offset)
    money = reports_repo.money_by_month(db, [(s, e) for _label, s, e in buckets])

    months: list[dict] = []
    totals = {kind: {"count": 0, "priced": 0, "total": 0} for kind in (KIND_WON, KIND_LOST)}
    for index, (label, _s, _e) in enumerate(buckets):
        row = {"month": label}
        for kind in (KIND_WON, KIND_LOST):
            cell = money.get((index, kind), {"count": 0, "priced": 0, "total": 0})
            row[f"{kind}_count"] = cell["count"]
            row[f"{kind}_priced"] = cell["priced"]
            row[f"{kind}_amount"] = cell["total"]
            for field in ("count", "priced", "total"):
                totals[kind][field] += cell[field]
        # Средний чек месяца — по сделкам этого месяца с названной ценой. Месяц
        # без таких сделок даёт прочерк, а не ноль.
        row["avg_check"] = _avg(money.get((index, KIND_WON)))
        months.append(row)

    won, lost = totals[KIND_WON], totals[KIND_LOST]
    return {
        "months": months,
        "won_amount": won["total"],
        "won_count": won["count"],
        "won_priced": won["priced"],
        # Сколько денег ушло вместе с отказами — по сделкам с названной ценой.
        "lost_amount": lost["total"],
        "lost_count": lost["count"],
        "lost_priced": lost["priced"],
        "avg_check": _avg(won),
        # Доля выигранных среди ЗАКРЫТЫХ за период: сделки в работе ничем ещё не
        # кончились, и в знаменателе им не место.
        "conversion": share(won["count"], won["count"] + lost["count"]),
    }


def _avg(totals: dict | None) -> int | None:
    """Средний чек: сумма делится на сделки С НАЗВАННОЙ ценой.

    Возьми в знаменатель все выигранные — и каждая сделка без цены будет тихо
    занижать чек. Число останется правдоподобным, и заметить подмену можно
    будет только пересчитав вручную.
    """
    if not totals or not totals.get("priced"):
        return None
    return round(totals["total"] / totals["priced"])


# --- источники ---

def sources(db: Session, start: datetime, end: datetime) -> dict:
    """Сколько клиентов и денег пришло с каждого источника.

    Клиенты считаются по дате появления, деньги — по дате закрытия сделки, и это
    намеренно разные оси. Привяжи деньги к дате появления клиента — и выручка
    июля от январского клиента исчезнет из отчёта совсем; привяжи клиентов к
    закрытию — и месяц без сделок покажет ноль новых клиентов, которых на самом
    деле пришло два десятка.
    """
    clients = reports_repo.clients_by_source(db, start, end)
    deals = reports_repo.closed_deals_by_source(db, start, end)

    # Пресет показываем всегда: «с рекламы ноль» — это ответ, ради которого
    # отчёт и открывают. Плюс свои значения и те, что встретились в периоде.
    keys: list[str | None] = list(CLIENT_SOURCES)
    for key in reports_repo.known_sources(db):
        if key not in keys:
            keys.append(key)
    # «Источник не указан» — отдельная строка, а не тихое слияние с «другое».
    # Пробел в работе менеджеров виден только если он нарисован.
    keys.append(None)

    rows = []
    for key in keys:
        won = deals.get((key, KIND_WON), {"count": 0, "priced": 0, "total": 0})
        lost = deals.get((key, KIND_LOST), {"count": 0, "priced": 0, "total": 0})
        closed = won["count"] + lost["count"]
        rows.append(
            {
                "source": key,
                "clients": clients.get(key, 0),
                "won_count": won["count"],
                "lost_count": lost["count"],
                "revenue": won["total"],
                # Конверсия источника — доля выигранных среди ЗАКРЫТЫХ сделок.
                # Сделки в работе в знаменатель не идут: они ещё ничем не
                # кончились, а источник с большим объёмом работы выглядел бы
                # тем хуже, чем больше по нему делают прямо сейчас.
                "conversion": share(won["count"], closed),
            }
        )

    rows.sort(key=lambda row: (-row["revenue"], -row["clients"], str(row["source"] or "")))
    return {
        "items": rows,
        "clients_total": sum(row["clients"] for row in rows),
        "revenue_total": sum(row["revenue"] for row in rows),
    }


# --- выгрузка ---

# Заголовки колонок в обеих раскладках: выгрузку открывает человек, а не
# программа, и «won_amount» ему ни о чём не говорит.
CSV_HEADERS = {
    "funnel": {
        "en": ["Stage", "Kind", "Entered", "Conversion from previous, %"],
        "ru": ["Этап", "Вид", "Вошло заявок", "Конверсия к предыдущему, %"],
    },
    "revenue": {
        "en": ["Month", "Won", "Won with a price", "Revenue", "Lost", "Lost value", "Average deal"],
        "ru": ["Месяц", "Выиграно", "Из них с ценой", "Выручка", "Потеряно", "Сумма потерь", "Средний чек"],
    },
    "sources": {
        "en": ["Source", "Clients", "Won", "Lost", "Revenue", "Conversion, %"],
        "ru": ["Источник", "Клиентов", "Выиграно", "Потеряно", "Выручка", "Конверсия, %"],
    },
}

SOURCE_NAMES = {
    "en": {
        "referral": "Word of mouth", "search": "Search", "social": "Social",
        "ads": "Ads", "repeat": "Returning", "other": "Other", None: "Not asked",
    },
    "ru": {
        "referral": "Сарафан", "search": "Поиск", "social": "Соцсети",
        "ads": "Реклама", "repeat": "Повторный", "other": "Другое", None: "Не указан",
    },
}

STAGE_KIND_NAMES = {
    "en": {KIND_OPEN: "In progress", KIND_WON: "Won", KIND_LOST: "Lost"},
    "ru": {KIND_OPEN: "В работе", KIND_WON: "Выиграна", KIND_LOST: "Потеряна"},
}


def to_csv(rows: list[list], header: list[str]) -> bytes:
    """Строки в CSV для Excel.

    Два решения, из-за которых выгрузку обычно приходится «чинить» руками.

    **UTF-8 с BOM.** Без метки Excel открывает файл в системной ANSI-кодировке,
    и кириллица превращается в кракозябры — при том, что сам файл верный.
    Три байта в начале и есть та метка, по которой Excel понимает UTF-8.

    **Разделитель `;` и запятая в дробях.** Excel в русской, украинской и
    европейских локалях делит строку именно по точке с запятой, а десятичным
    разделителем считает запятую. Это связка: выбрав `;`, обязан взять и `,` —
    иначе суммы приедут текстом. Файл с запятыми-разделителями в тех же Excel
    свалился бы в один столбец.

    Перевод строки — CRLF, как требует RFC 4180 и как ждёт Excel.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def money_cell(minor: int | None) -> str:
    """Сумма из минимальных единиц в вид для таблицы.

    None — пустая ячейка, а не ноль: «сумму не назвали» и «ноль денег» в
    выгрузке обязаны отличаться так же, как в базе.
    """
    if minor is None:
        return ""
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(minor), 100)
    return f"{sign}{whole},{cents:02d}"


def percent_cell(value: float | None) -> str:
    """Прочерк вместо нуля, когда делить было не на что."""
    return "" if value is None else f"{value:.1f}".replace(".", ",")


def funnel_csv(data: dict, locale: str) -> bytes:
    lang = "ru" if locale == "ru" else "en"
    kinds = STAGE_KIND_NAMES[lang]
    rows = [
        [
            step["name"],
            kinds.get(step["kind"], step["kind"]),
            step["entered"],
            percent_cell(step["from_previous"]),
        ]
        for step in data["stages"]
    ]
    return to_csv(rows, CSV_HEADERS["funnel"][lang])


def revenue_csv(data: dict, locale: str) -> bytes:
    lang = "ru" if locale == "ru" else "en"
    rows = [
        [
            month["month"],
            month["won_count"],
            month["won_priced"],
            money_cell(month["won_amount"]),
            month["lost_count"],
            money_cell(month["lost_amount"]),
            money_cell(month["avg_check"]),
        ]
        for month in data["months"]
    ]
    return to_csv(rows, CSV_HEADERS["revenue"][lang])


def sources_csv(data: dict, locale: str) -> bytes:
    lang = "ru" if locale == "ru" else "en"
    names = SOURCE_NAMES[lang]
    rows = [
        [
            names.get(row["source"], row["source"] or ""),
            row["clients"],
            row["won_count"],
            row["lost_count"],
            money_cell(row["revenue"]),
            percent_cell(row["conversion"]),
        ]
        for row in data["items"]
    ]
    return to_csv(rows, CSV_HEADERS["sources"][lang])
