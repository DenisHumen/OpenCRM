import threading
import time


class SlidingWindowLimiter:
    """Простой in-memory rate limit: N неудач за окно → блокировка.

    Достаточно для одного процесса (MVP). При переходе на несколько
    воркеров — вынести в БД или Redis.
    """

    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = [t for t in self._attempts.get(key, []) if now - t < self.window]
            self._attempts[key] = attempts
            return len(attempts) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
