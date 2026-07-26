import re
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Пользователь считается «в сети», если активность была не позже этого порога.
# Больше интервала heartbeat/поллинга фронта (45–120 c), чтобы не мигало на границе.
ONLINE_THRESHOLD_SECONDS = 150
# Не пишем last_seen на каждый запрос — не чаще раза в минуту.
PRESENCE_TOUCH_SECONDS = 60


def now_utc() -> datetime:
    """Naive UTC — в БД храним время без таймзоны, всегда UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_online(last_seen_at: datetime | None) -> bool:
    if last_seen_at is None:
        return False
    return (now_utc() - last_seen_at).total_seconds() <= ONLINE_THRESHOLD_SECONDS


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def normalize_email(email: str) -> str:
    return email.strip().lower()
