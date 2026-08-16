"""Ограничитель попыток: N неудач за окно → отказ.

Счётчик ОБЩИЙ НА ВСЕ РАБОЧИЕ ПРОЦЕССЫ. Раньше он жил в памяти процесса, и это
работало ровно до тех пор, пока процесс был один. Переезд на MySQL снимает
запрет на несколько процессов — и вместе с ним снял бы единственное, на чём
держалась защита от подбора: восемь воркеров это восемь независимых счётчиков,
порог умножается на восемь, и ошибки при этом не возникает ни одной. Снаружи
защита выглядит работающей. Ровно поэтому Redis в этом продукте обязателен, а
не выбирается (подробности — в шапке `core/redis_client.py`).

--------------------------------------------------------------------------
Что делает система, когда Redis недоступен: ОТКАЗЫВАЕТ
--------------------------------------------------------------------------

Выбор был между двумя ответами, и оба платные:

  * **пропускать всех, крича в лог** — сайт продолжает работать, но защита от
    подбора выключена целиком на всё время аварии;
  * **отказывать** — вход, PIN, приём заявок и вебхук АТС отвечают 503, пока
    Redis не вернётся.

Выбран отказ, и вот довод. Ограничитель сторожит ВХОД: пароль сотрудника,
четырёхзначный PIN публичной ссылки, сквозные номера бланков. Не имея счётчика,
он не может отличить шестую попытку от первой — то есть «пропускать всех»
означает не «работать чуть хуже», а «подбор без всякого ограничения». Хуже
того, это ослабление наступает ровно в тот момент, когда за системой никто не
смотрит (авария), и наступает БЕСШУМНО для атакующего: снаружи ничего не
меняется, кроме того, что попытки перестали кончаться. Атака на Redis
превращается в обход защиты входа — то есть отказ в обслуживании одной службы
покупает обход аутентификации.

Цена отказа, наоборот, ограничена и видна: 503 на пяти ручках, всё остальное
(чтение CRM, витрина, файлы) продолжает работать; авария громкая — метрика
`opencrm_ratelimiter_unavailable_total`, строка в логе и правило тревоги.
Восстановление — поднять контейнер, который и так поднимается сам
(`restart: unless-stopped`).

Отказ применяется ко всем пяти ограничителям одинаково, включая те, что считают
поток, а не промахи (заявки с формы, вебхук АТС). Отдельное правило для них
означало бы две разных реакции на одну и ту же аварию и второй путь, который
проверяют один раз, а потом забывают.

**Отмечать неудачу и сбрасывать счётчик при этом НЕ падают.** Вердикт по
текущему запросу уже вынесен `is_blocked`, и если Redis отвечал ему — он
ответит и здесь; а если не отвечал, то запрос и так отвергнут. Превращать
бухгалтерию в пятисотую ошибку незачем.
"""

import math
import threading
import time
import uuid

from core import exceptions as errors
from core import redis_client

#: После скольких разных ключей чистим просроченные (только в памяти процесса).
#:
#: Порог, а не таймер: уборка по расписанию требует фонового потока, а он в
#: однопроцессном приложении лишний. Тысяча ключей — это меньше сотни килобайт,
#: то есть чистка включается задолго до того, как размер начинает что-то значить.
#:
#: В Redis этого не нужно вовсе: у каждого ключа стоит TTL длиной в окно, и
#: просроченные исчезают сами. Приём с `SWEEP_AFTER` заводился именно против
#: ключей, приходящих с улицы, — TTL закрывает ту же дыру надёжнее.
SWEEP_AFTER = 1024

#: Сколько раз ограничители не смогли дозвониться до Redis. Читает `/metrics`.
_unavailable_total = 0
_unavailable_lock = threading.Lock()
_last_shout = 0.0
_last_failure = 0.0

#: За сколько секунд назад считаем аварию «происходящей прямо сейчас».
#:
#: Нужно `/healthz`, который отвечает про состояние счётчика БЕЗ обращения к
#: Redis. Обращение там недопустимо: `/healthz` опрашивает docker каждые десять
#: секунд с потолком в пять, а разрешение имени остановленного контейнера
#: занимает больше (проверено живьём — заход отвалился по таймауту). Получилась
#: бы связка «лежит Redis → приложение объявлено нездоровым → контейнер
#: перезапущен», то есть авария соседней службы роняла бы сайт.
RECENT_SECONDS = 60.0

#: Не чаще раза в столько секунд пишем в лог про недоступный Redis.
#:
#: Без этого авария на входе, открытом в интернет, залила бы журнал сотней
#: строк в секунду — и настоящую причину в нём было бы уже не найти.
SHOUT_EVERY_SECONDS = 30.0


def unavailable_total() -> int:
    """Сколько раз ограничитель не достучался до Redis за жизнь процесса."""
    return _unavailable_total


def recently_unavailable(seconds: float = RECENT_SECONDS) -> bool:
    """Была ли авария счётчика в последние `seconds`. Ничего не спрашивает у сети.

    Ответ по факту работы, а не по опросу: если ограничитель за минуту ни разу
    не сбоил, значит он и не нужен был никому — обращений не было. «Redis лежит,
    но об этом никто не узнал» существует ровно до первой попытки входа, а она
    сразу же и отметится здесь.
    """
    if not _last_failure:
        return False
    return time.monotonic() - _last_failure < seconds


def _otboy() -> None:
    """Счётчик ответил — авария кончилась.

    Без этого `/healthz` держал бы «redis: down» ещё минуту после того, как
    служба вернулась: контейнер перезапускается сам (`restart: unless-stopped`),
    и мимолётная нехватка в момент перезапуска — обычное дело, а не повод
    минуту врать про состояние. Проверено живьём: после перезапуска всего стека
    первый вход попал в окно, когда Redis ещё поднимался.
    """
    global _last_failure
    if not _last_failure:
        return
    with _unavailable_lock:
        _last_failure = 0.0


def _alarm(action: str, exc: Exception) -> None:
    """Громко сказать, что общего счётчика нет. Тихого варианта здесь не бывает."""
    global _unavailable_total, _last_shout, _last_failure
    with _unavailable_lock:
        _unavailable_total += 1
        now = time.monotonic()
        _last_failure = now
        shout = now - _last_shout >= SHOUT_EVERY_SECONDS
        if shout:
            _last_shout = now
    if shout:
        print(
            f"[opencrm] ТРЕВОГА: общий счётчик попыток недоступен ({action}): {exc}. "
            "Защита от подбора не может работать, вход отвечает 503 до возвращения Redis.",
            flush=True,
        )


class SlidingWindowLimiter:
    """N неудач за окно → блокировка. Окно скользящее, счётчик общий.

    Наружу — те же четыре действия, что и были: спросить, отметить неудачу,
    сбросить, посчитать ключи. Окно и порог задаются вызывающим и этой правкой
    не менялись.

    `name` разделяет ограничители в общем хранилище. Без него подбор PIN и
    перебор номеров бланков считались бы в один счётчик: ключ у обоих — хэш
    адреса посетителя, и совпал бы он не по случайности, а всегда.
    """

    def __init__(self, max_attempts: int, window_seconds: float, name: str = "default"):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self.name = name
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # --- общий счётчик (Redis) ------------------------------------------------

    def _key(self, key: str) -> str:
        return f"{redis_client.PREFIX}rl:{self.name}:{key}"

    def _ttl(self) -> int:
        """TTL ключа: окно с запасом в секунду, но не меньше секунды.

        Запас нужен из-за округления вверх: ключ, истёкший на миллисекунду
        раньше своей последней попытки, отпустил бы блокировку досрочно.
        """
        return max(1, math.ceil(self.window) + 1)

    def is_blocked(self, key: str) -> bool:
        """Заблокирован ли ключ. Читающая проверка ничего не заводит.

        Раньше заводила: неизвестный ключ записывался с пустым списком, и одного
        запроса с новым адресом хватало, чтобы оставить след в памяти. Проверка
        вызывается на КАЖДОМ входе — то есть след оставляли и те, кто ввёл
        пароль правильно с первого раза. В Redis это свойство сохраняется само:
        `ZREMRANGEBYSCORE` и `ZCARD` по несуществующему ключу его не создают.
        """
        client = redis_client.get_client()
        if client is None:
            return self._is_blocked_local(key)
        now = time.time()
        try:
            pipe = client.pipeline(transaction=True)
            pipe.zremrangebyscore(self._key(key), "-inf", now - self.window)
            pipe.zcard(self._key(key))
            _, skolko = pipe.execute()
        except Exception as exc:  # noqa: BLE001 — сеть, таймаут, отказ сервера
            _alarm("проверка", exc)
            raise errors.LimiterUnavailableError(
                "Rate limiter storage is unavailable", code="limiter_unavailable"
            ) from exc
        _otboy()
        return int(skolko) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        client = redis_client.get_client()
        if client is None:
            self._record_failure_local(key)
            return
        now = time.time()
        try:
            pipe = client.pipeline(transaction=True)
            # Метка уникальная, а не сам момент: две неудачи в одну и ту же
            # миллисекунду (двойное нажатие, две вкладки) записались бы одним
            # членом множества и посчитались бы за одну попытку.
            pipe.zadd(self._key(key), {f"{now}:{uuid.uuid4().hex}": now})
            pipe.zremrangebyscore(self._key(key), "-inf", now - self.window)
            pipe.expire(self._key(key), self._ttl())
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            # Не падаем: вердикт по этому запросу уже вынесен, а следующий
            # всё равно упрётся в отказ на проверке. См. шапку модуля.
            _alarm("отметка неудачи", exc)

    def reset(self, key: str) -> None:
        client = redis_client.get_client()
        if client is None:
            self._reset_local(key)
            return
        try:
            client.delete(self._key(key))
        except Exception as exc:  # noqa: BLE001
            # Несброшенный счётчик ошибается в безопасную сторону: человек,
            # вошедший с третьей попытки, доживёт до конца окна с тремя
            # отметками вместо нуля. Ронять из-за этого удачный вход незачем.
            _alarm("сброс", exc)

    def ochistit(self) -> None:
        """Забыть ВСЁ, что помнит этот ограничитель.

        Нужно проверкам, и это не поблажка им, а следствие устройства. Пока
        счётчик жил в памяти процесса, проверка сбрасывала его, обнулив словарь.
        С общим счётчиком в Redis такой сброс перестал что-либо значить: словарь
        пуст, а счётчик полон — и соседние проверки начинают влиять друг на
        друга. Поймано ровно так: добавили Redis в ворота, и проверка формы с
        сайта стала падать на счётчике, набранном её же соседкой.

        В горячем пути не зовётся никем: это `SCAN` по приставке, то есть обход,
        а не обращение по ключу.
        """
        with self._lock:
            self._attempts.clear()
        client = redis_client.get_client()
        if client is None:
            return
        try:
            obrazets = f"{redis_client.PREFIX}rl:{self.name}:*"
            for klyuch in client.scan_iter(match=obrazets, count=200):
                client.delete(klyuch)
        except Exception as exc:  # noqa: BLE001
            _alarm("очистка", exc)

    def tracked(self) -> int:
        """Сколько ключей помнит ограничитель. Нужно проверке, что он не пухнет.

        В Redis это перебор ключей по приставке (`SCAN`), то есть вещь для
        диагностики и тестов, а не для горячего пути. На горячем пути её никто
        и не зовёт.
        """
        client = redis_client.get_client()
        if client is None:
            with self._lock:
                return len(self._attempts)
        try:
            return sum(1 for _ in client.scan_iter(match=self._key("*"), count=500))
        except Exception as exc:  # noqa: BLE001
            _alarm("подсчёт ключей", exc)
            raise errors.LimiterUnavailableError(
                "Rate limiter storage is unavailable", code="limiter_unavailable"
            ) from exc

    # --- счётчик в памяти процесса -------------------------------------------
    #
    # Остаётся ровно для одного сочетания: адрес Redis не задан, процесс один,
    # окружение не боевое (`config/settings.config_errors` не даёт другому
    # сочетанию подняться). В нём счётчик в памяти И ЕСТЬ общий счётчик.
    #
    # **Ключи не копятся.** Это не про аккуратность, а про то, что список
    # ключей наполняется с улицы: ключ входа — присланный адрес почты, ключ PIN
    # — адрес посетителя. Пока просроченные записи оставались лежать, миллион
    # запросов с разными адресами (перебор — обычное дело для формы входа,
    # открытой в интернет) оставлял миллион записей в памяти навсегда. Отказа
    # при этом никто бы не увидел: процесс просто пухнет, пока его не убьёт хост.

    def _is_blocked_local(self, key: str) -> bool:
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

    def _record_failure_local(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(time.monotonic())
            if len(self._attempts) > SWEEP_AFTER:
                self._sweep()

    def _reset_local(self, key: str) -> None:
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
