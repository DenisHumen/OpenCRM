import re
from datetime import date, datetime, timedelta, timezone

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Пользователь считается «в сети», если активность была не позже этого порога.
# Больше интервала heartbeat/поллинга фронта (45–120 c), чтобы не мигало на границе.
ONLINE_THRESHOLD_SECONDS = 150
# Не пишем last_seen на каждый запрос — не чаще раза в минуту.
PRESENCE_TOUCH_SECONDS = 60


def now_utc() -> datetime:
    """Naive UTC — в БД храним время без таймзоны, всегда UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def divide_money(total: int, count: int) -> int:
    """Деньги пополам: целочисленно и с округлением половины ОТ нуля.

    Два правила проекта разом. Первое: деньги — целые в минорных единицах и
    через `float` не проходят ни на секунду; `total / count` — как раз он.
    Второе: `round()` в Python округляет половину к ЧЁТНОМУ, то есть 100,5 даёт
    100, а 101,5 — 102. В отчёте о деньгах это ошибка в половину минорной
    единицы, зато то в одну сторону, то в другую, и объяснить её человеку,
    пересчитавшему средний чек на калькуляторе, нечем.

    `(|total| * 2 + count) // (count * 2)` — то же самое, что «прибавить
    половину делителя и отбросить дробь», но без деления с плавающей точкой.
    Знак снимается заранее: `//` в Python округляет ВНИЗ, и на отрицательной
    сумме половина уехала бы от нуля в другую сторону, чем на положительной.

    Лежит здесь, а не в отчётах, потому что средний чек считают двое: отчёт о
    выручке и плитка сводки. Пока правило было записано в отчётах, плитка
    делила по-своему — и два числа под одной подписью расходились на единицу.
    """
    sign = -1 if total < 0 else 1
    return sign * ((abs(total) * 2 + count) // (count * 2))


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Момент из запроса — к тому виду, в каком время лежит в базе: naive UTC.

    Клиент присылает абсолютный момент со смещением («18:00 по Киеву»), и
    сохранить его как есть нельзя: база сложит вместе aware и naive значения, а
    сравнение «просрочено ли» начнёт врать ровно на величину смещения. Ошибка
    при этом тихая — срок просто наступает не тогда.

    Живёт рядом с `now_utc`, а не в сервисе напоминаний, где был написан. Пока
    он лежал там, склад импортировал его из напоминаний (странная зависимость
    между несвязанными блоками), а сроки заявок про него не знали вовсе — и
    теряли смещение молча: «18:00+03:00» ложилось в базу как «18:00».

    Пришло без зоны — считаем, что это уже UTC. Другого разумного предположения
    нет: гадать о зоне отправителя хуже, чем принять соглашение и записать его.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def is_online(last_seen_at: datetime | None) -> bool:
    if last_seen_at is None:
        return False
    return (now_utc() - last_seen_at).total_seconds() <= ONLINE_THRESHOLD_SECONDS


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


MAX_URL_LENGTH = 500


def normalize_external_url(value: str | None) -> str:
    """Приводит внешнюю ссылку к безопасному виду или возвращает '' для пустой.

    Пускаем только ``http``/``https``: такие ссылки уходят в атрибут ``href`` на
    публичной витрине, а ``javascript:``/``data:`` там означали бы XSS — экранирование
    Jinja2 от этого не спасает, схему нужно проверять явно. Возвращает нормализованную
    строку; на непригодном значении бросает ValueError.
    """
    url = (value or "").strip()
    if not url:
        return ""
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL is too long (max {MAX_URL_LENGTH})")
    # управляющие символы в href ломают разметку и обходят проверку схемы
    if any(ord(ch) < 32 for ch in url):
        raise ValueError("URL contains control characters")
    # "//evil.com" браузер читает как protocol-relative и уводит на чужой домен
    if url.startswith("//"):
        raise ValueError("URL must start with http:// or https://")

    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        # чужая схема (javascript:, data:, ftp:) — отказ: ссылка идёт в href на витрине
        if ":" in url.split("/", 1)[0]:
            raise ValueError("URL must start with http:// or https://")
        # адрес без схемы («studio.site») — обычный способ набора, дописываем сами,
        # иначе сохранение молча падало бы и оставался прежний адрес
        url = "https://" + url
    return url


def normalize_email(email: str) -> str:
    return email.strip().lower()


# Короче этого — не абонент, а внутренний номер АТС (101, 2345) или служебная
# метка вроде «anonymous»: приводить такое к международному виду нечего.
PHONE_MIN_DIGITS = 7
# Длиннее этого номер уже содержит код страны, и дописывать ему ещё один —
# испортить данные. Национальные номера абонентов почти везде укладываются
# в 10 цифр (UA/PL — 9, US — 10, DE — до 11 с нулём).
PHONE_NATIONAL_MAX_DIGITS = 10


def normalize_phone(raw: str, country_code: str = "") -> str:
    """Приводит номер к единому виду для сравнения: только цифры, без «+».

    Один и тот же человек приходит из АТС, из формы и из импорта в разном
    написании: ``+380671234567``, ``380671234567``, ``0671234567``,
    ``+38 (067) 123-45-67``. Пока сравниваются исходные строки, звонок не
    находит клиента — это самая частая причина, по которой телефония «не
    работает». Поэтому рядом с исходным номером всегда хранится результат этой
    функции, и ищут по нему.

    «+» в каноническом виде не участвует: он ничего не добавляет к сравнению,
    а вариантов записи с ним и без него ровно вдвое больше, чем нужно.

    ``country_code`` (настройка ``default_country_code``, например ``380``)
    нужен, чтобы местный набор ``067…`` сошёлся с международным ``+380 67…``.
    Без него местные номера остаются как есть — это промах поиска, но не порча
    данных: дописать чужой код страны хуже, чем не дописать никакой.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    plus = value.startswith("+")
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return ""
    # «00» — международный префикс набора, тот же смысл, что «+»
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if len(digits) < PHONE_MIN_DIGITS:
        return digits

    code = "".join(ch for ch in (country_code or "") if ch.isdigit())
    if not plus and code and not digits.startswith(code):
        if digits.startswith("0"):
            # междугородний префикс страны: 067… → 380 67…
            digits = code + digits.lstrip("0")
        elif len(digits) <= PHONE_NATIONAL_MAX_DIGITS:
            # номер без префикса и без кода страны — считаем местным
            digits = code + digits
    return digits


def konets_dnya(den: date, shift: timedelta = timedelta()) -> datetime:
    """Верхняя граница периода: начало СЛЕДУЮЩЕГО дня, со смещением пояса.

    Следующий день, а не 23:59:59, — по тому же доводу, что и раньше: секунда
    без долей отрезала бы операции, попавшие в последнюю её долю.

    Отдельной функцией, потому что счёт этот стоял в трёх местах
    (`finance_service._period_bounds`, `report_service.parse_period` и соседняя
    выборка), и во всех трёх одинаково падал на краю календаря. Дата
    `9999-12-31` разбирается без единого возражения — `date.fromisoformat` её
    принимает, MySQL DATE хранит, — а `+ timedelta(days=1)` за ней бросает
    `OverflowError`. Это не `ValueError`, который ловится этажом выше, и не
    доменная ошибка: обработчика на него нет вовсе, и наружу он выходит
    пятисоткой.

    Хуже всего это у бюджета: план с таким концом СОХРАНЯЕТСЯ, а потом попадает
    в выдачу каждого месяца — то есть экран планов ложится навсегда, и починить
    его из интерфейса нельзя, потому что интерфейс не открывается.

    Запрещать далёкую дату не стали. «План до конца проекта» — законное
    намерение, и отвечать на него отказом значило бы чинить не то. Период,
    упирающийся в край календаря, кончается вместе с календарём: `datetime.max`
    — это и есть «до конца всего, что можно записать».
    """
    try:
        return datetime.combine(den + timedelta(days=1), datetime.min.time()) + shift
    except (OverflowError, ValueError):
        return datetime.max
