import threading
import time

#: После скольких разных ключей чистим просроченные.
#:
#: Порог, а не таймер: уборка по расписанию требует фонового потока, а он в
#: однопроцессном приложении лишний. Тысяча ключей — это меньше сотни килобайт,
#: то есть чистка включается задолго до того, как размер начинает что-то значить.
SWEEP_AFTER = 1024


class SlidingWindowLimiter:
    """Простой in-memory rate limit: N неудач за окно → блокировка.

    Достаточно для одного процесса (MVP). При переходе на несколько
    воркеров — вынести в БД или Redis.

    **Ключи не копятся.** Это не про аккуратность, а про то, что список ключей
    наполняется с улицы: ключ входа — присланный адрес почты, ключ PIN — адрес
    посетителя. Пока просроченные записи оставались лежать, миллион запросов с
    разными адресами (перебор — обычное дело для формы входа, открытой в
    интернет) оставлял миллион записей в памяти навсегда. Отказа при этом никто
    бы не увидел: процесс просто пухнет, пока его не убьёт хост.
    """

    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        """Заблокирован ли ключ. Читающая проверка ничего не заводит.

        Раньше заводила: неизвестный ключ записывался с пустым списком, и одного
        запроса с новым адресом хватало, чтобы оставить след в памяти. Проверка
        вызывается на КАЖДОМ входе — то есть след оставляли и те, кто ввёл
        пароль правильно с первого раза.
        """
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts.get(key)
            if not attempts:
                return False
            fresh = [t for t in attempts if now - t < self.window]
            if fresh:
                self._attempts[key] = fresh
            else:
                del self._attempts[key]
            return len(fresh) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(time.monotonic())
            if len(self._attempts) > SWEEP_AFTER:
                self._sweep()

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def _sweep(self) -> None:
        """Выбросить ключи, у которых все попытки просрочены. Под замком."""
        now = time.monotonic()
        expired = [
            key
            for key, attempts in self._attempts.items()
            if not attempts or now - attempts[-1] >= self.window
        ]
        for key in expired:
            del self._attempts[key]

    def tracked(self) -> int:
        """Сколько ключей помнит ограничитель. Нужно проверке, что он не пухнет."""
        with self._lock:
            return len(self._attempts)
