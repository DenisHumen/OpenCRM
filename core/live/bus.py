"""Шина намёков: поток Redis между процессами, память — когда Redis не задан.

Выбор — `docs/ustroystvo/12-zhivye-obnovleniya.md` §5 и §11. Внутрипроцессная шина законна ровно в
одном сочетании: адрес Redis не задан, процесс один, окружение не боевое —
ноутбук разработчика и набор тестов. При заданном, но лежащем Redis на неё
НЕ переключаемся: часть людей получала бы обновления, часть нет, и никто не
знал бы, в какой он половине. Намёк тогда теряется, счётчик потерь растёт, а
продукт работает как до живого слоя.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

from core import redis_client
from core.live.message import Hint

logger = logging.getLogger(__name__)

#: Хвост потока. Тысяча — до замера на живой памяти (§14 п. 5): короткий хвост
#: стоит перечитывания, длинный вытесняет счётчики попыток входа из того же
#: Redis (64 МБ, `allkeys-lru`) — то есть тише работающую защиту от подбора.
MAXLEN = 1000
STREAM = f"{redis_client.PREFIX}live:stream"
#: Как часто жаловаться в журнал на лежащий Redis (приём `core/ratelimit._alarm`).
ALARM_SECONDS = 30.0
#: Сколько ждёт `XREAD BLOCK` — короче, чем нужно, чтобы остановка не тянулась.
BLOCK_MS = 1000

_lock = threading.Lock()
#: Счётчики для метрик. Целые под замком; Prometheus скребёт один процесс, и
#: этого достаточно — считается доля, а не сумма.
published_total = 0
dropped_total = 0
connections = 0


def _schitat(imya: str, delta: int = 1) -> None:
    global published_total, dropped_total, connections
    with _lock:
        if imya == "published":
            published_total += delta
        elif imya == "dropped":
            dropped_total += delta
        elif imya == "connections":
            connections += delta


class Podpiska:
    """Очередь одного слушателя. `get` ждёт не дольше `timeout`, `None` — тишина."""

    def __init__(self, shina: "_Shina") -> None:
        self._ochered: queue.Queue = queue.Queue()
        self._shina = shina
        self.zakryta = False

    def polozhit(self, nomer: str, hint: Hint) -> None:
        self._ochered.put((nomer, hint))

    def get(self, timeout: float = 0.0) -> tuple[str, Hint] | None:
        try:
            return self._ochered.get(timeout=timeout) if timeout else self._ochered.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self.zakryta = True
        self._shina.otpisat(self)


class _Shina:
    def __init__(self) -> None:
        self._podpiski: set[Podpiska] = set()
        self._zamok = threading.Lock()

    def podpisatsya(self) -> Podpiska:
        p = Podpiska(self)
        with self._zamok:
            self._podpiski.add(p)
        return p

    def otpisat(self, p: Podpiska) -> None:
        with self._zamok:
            self._podpiski.discard(p)

    def _razdat(self, nomer: str, hint: Hint) -> None:
        with self._zamok:
            komu = list(self._podpiski)
        for p in komu:
            p.polozhit(nomer, hint)

    def publish(self, hint: Hint) -> str | None:  # pragma: no cover — переопределяется
        raise NotImplementedError

    def catch_up(self, since: str) -> list[tuple[str, Hint]] | None:  # pragma: no cover
        raise NotImplementedError

    def zhiva(self) -> bool:
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class _Pamyat(_Shina):
    """Память процесса: тот же хвост и те же номера вида `<мс>-<порядок>`."""

    def __init__(self) -> None:
        super().__init__()
        self._hvost: deque[tuple[str, Hint]] = deque(maxlen=MAXLEN)
        self._poslednee_ms = 0
        self._poryadok = 0

    def _nomer(self) -> str:
        ms = int(time.time() * 1000)
        with self._zamok:
            if ms <= self._poslednee_ms:
                ms = self._poslednee_ms
                self._poryadok += 1
            else:
                self._poslednee_ms = ms
                self._poryadok = 0
            return f"{ms}-{self._poryadok}"

    def publish(self, hint: Hint) -> str | None:
        nomer = self._nomer()
        self._hvost.append((nomer, hint))
        _schitat("published")
        self._razdat(nomer, hint)
        return nomer

    def catch_up(self, since: str) -> list[tuple[str, Hint]] | None:
        hvost = list(self._hvost)
        nomera = [n for n, _ in hvost]
        if since not in nomera:
            return None
        return hvost[nomera.index(since) + 1 :]


def _nomer_bolshe(a: str, b: str) -> bool:
    """Сравнение номеров потока `<мс>-<порядок>` как чисел, а не строк."""
    try:
        am, ap = (int(x) for x in a.split("-", 1))
        bm, bp = (int(x) for x in b.split("-", 1))
    except ValueError:
        return a > b
    return (am, ap) > (bm, bp)


class _Potok(_Shina):
    """Поток Redis: `XADD MAXLEN ~ N`, чтение `XREAD BLOCK` в фоновом потоке процесса."""

    def __init__(self) -> None:
        super().__init__()
        self._chitatel: threading.Thread | None = None
        self._stop = threading.Event()
        self._poslednyaya_zhaloba = 0.0

    def _pozhalovatsya(self, chto: str, beda: Exception) -> None:
        seychas = time.monotonic()
        if seychas - self._poslednyaya_zhaloba >= ALARM_SECONDS:
            self._poslednyaya_zhaloba = seychas
            logger.warning("живые обновления: %s не удалось — %r", chto, beda)

    def zhiva(self) -> bool:
        return redis_client.ping()

    def publish(self, hint: Hint) -> str | None:
        client = redis_client.get_client()
        if client is None:
            _schitat("dropped")
            return None
        try:
            nomer = client.xadd(STREAM, {"h": hint.to_json()}, maxlen=MAXLEN, approximate=True)
        except Exception as beda:  # noqa: BLE001 — шина не обязана быть живой
            _schitat("dropped")
            self._pozhalovatsya("запись в поток", beda)
            return None
        _schitat("published")
        return nomer.decode() if isinstance(nomer, bytes) else str(nomer)

    @staticmethod
    def _razobrat(zapis) -> tuple[str, Hint] | None:
        nomer, polya = zapis
        nomer = nomer.decode() if isinstance(nomer, bytes) else str(nomer)
        telo = polya.get(b"h") if b"h" in polya else polya.get("h")
        if isinstance(telo, bytes):
            telo = telo.decode("utf-8", "replace")
        try:
            return nomer, Hint.from_json(telo)
        except (ValueError, TypeError):
            return None

    def catch_up(self, since: str) -> list[tuple[str, Hint]] | None:
        client = redis_client.get_client()
        if client is None:
            return None
        try:
            pervaya = client.xrange(STREAM, "-", "+", count=1)
            if not pervaya:
                return None
            nachalo = pervaya[0][0]
            nachalo = nachalo.decode() if isinstance(nachalo, bytes) else str(nachalo)
            # Номер старше первого в хвосте — подрезали или Redis перезапустился:
            # догнать нечем, и честный ответ — `resync`, а не половина.
            if _nomer_bolshe(nachalo, since) and nachalo != since:
                return None
            zapisi = client.xrange(STREAM, f"({since}", "+")
        except Exception as beda:  # noqa: BLE001
            self._pozhalovatsya("догон по потоку", beda)
            return None
        itog = []
        for zapis in zapisi:
            razobrano = self._razobrat(zapis)
            if razobrano:
                itog.append(razobrano)
        return itog

    def podpisatsya(self) -> Podpiska:
        self.start()
        return super().podpisatsya()

    def start(self) -> None:
        with self._zamok:
            if self._chitatel is not None and self._chitatel.is_alive():
                return
            self._stop.clear()
            self._chitatel = threading.Thread(target=self._chitat, daemon=True, name="live-stream")
            self._chitatel.start()

    def stop(self) -> None:
        self._stop.set()
        chitatel = self._chitatel
        if chitatel is not None:
            chitatel.join(timeout=BLOCK_MS / 1000 + 1)
        self._chitatel = None

    def _chitat(self) -> None:
        """Читает с «сейчас»: прошлое раздаёт `catch_up` тому, кто пришёл с номером.

        Соединение своё, со сроком длиннее `BLOCK_MS` (`redis_client.blocking_client`):
        на общем клиенте тихий поток выглядел как обрыв каждую секунду.
        """
        posledniy = "$"
        client = None
        while not self._stop.is_set():
            if client is None:
                client = redis_client.blocking_client(BLOCK_MS / 1000)
            if client is None:
                time.sleep(1.0)
                continue
            try:
                otvet = client.xread({STREAM: posledniy}, block=BLOCK_MS, count=100)
            except Exception as beda:  # noqa: BLE001 — Redis лёг: ждём и пробуем снова
                self._pozhalovatsya("чтение потока", beda)
                try:
                    client.close()
                except Exception:  # noqa: BLE001 — соединение и так мертво
                    pass
                client = None
                time.sleep(1.0)
                continue
            for _klyuch, zapisi in otvet or []:
                for zapis in zapisi:
                    razobrano = self._razobrat(zapis)
                    if razobrano is None:
                        continue
                    posledniy = razobrano[0]
                    self._razdat(*razobrano)


_shina: _Shina | None = None


def shina() -> _Shina:
    """Одна шина на процесс. Redis задан — поток; не задан — память."""
    global _shina
    with _lock:
        if _shina is None:
            _shina = _Potok() if redis_client.configured() else _Pamyat()
        return _shina


def sbrosit() -> None:
    """Забыть шину — для проверок, которые подменяют адрес Redis."""
    global _shina
    with _lock:
        staraya, _shina = _shina, None
    if staraya is not None:
        staraya.stop()


def publish(hint: Hint) -> str | None:
    return shina().publish(hint)


def podpisatsya() -> Podpiska:
    return shina().podpisatsya()


def dognat(since: str) -> list[tuple[str, Hint]] | None:
    return shina().catch_up(since)


def zhiva() -> bool:
    return shina().zhiva()
