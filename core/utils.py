import re
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now_utc() -> datetime:
    """Naive UTC — в БД храним время без таймзоны, всегда UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def normalize_email(email: str) -> str:
    return email.strip().lower()
