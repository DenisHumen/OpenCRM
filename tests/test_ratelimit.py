"""Ограничитель попыток: блокирует, отпускает и не пухнет.

Ключи ему приносят с улицы — адрес почты из формы входа, адрес посетителя у
PIN. Всё, что растёт от чужих запросов и не убывает, рано или поздно
превращается в способ уронить сервер, ничего при этом не взломав.
"""

import time

from core import ratelimit, redis_client
from core.ratelimit import SWEEP_AFTER, SlidingWindowLimiter


def test_blocks_after_the_last_allowed_attempt():
    limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=60)

    for _ in range(2):
        limiter.record_failure("ivan@example.com")
        assert not limiter.is_blocked("ivan@example.com")

    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")
    # Чужой ключ не задет: блокируется попытка, а не форма целиком.
    assert not limiter.is_blocked("petr@example.com")


class SdvinutyeChasy:
    """Часы, ушедшие вперёд. Нужны обе стрелки: путь через Redis смотрит
    `time()`, запасной путь в памяти — `monotonic()`."""

    def __init__(self, sdvig: float) -> None:
        self.sdvig = sdvig

    def time(self) -> float:
        return time.time() + self.sdvig

    def monotonic(self) -> float:
        return time.monotonic() + self.sdvig


def test_the_window_lets_go(monkeypatch):
    """Окно кончилось — блокировка снята.

    Двигаем ЧАСЫ, а не ждём. Настоящая пауза здесь дважды подводила: сперва
    Windows будил спящий поток с шагом 16 мс, потом ворота уронил обратный
    проход при паузе в полсекунды на окне 0,1 с — пятикратном запасе.
    Воспроизвести не удалось, но одна сторона разобрана: запись и проверка
    меряют системными часами, а те в контейнере прыгают назад при поправке
    времени, и запись оказывается «моложе» окна, сколько ни спи.

    Ждать дольше — не выход, и это выяснилось подрывом. Ключ живёт TTL в две
    секунды (`_ttl`), и цикл ожидания на десять секунд зеленел БЕЗ подрезки
    окна вовсе: блокировка снималась истечением ключа. То есть проверка
    доказывала бы не то, ради чего написана.

    Сдвинутые часы разводят эти два случая: настоящего времени проходит
    столько, что ключ ещё жив, а окно для ограничителя уже кончилось.
    """
    limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=0.1)
    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")

    monkeypatch.setattr(ratelimit, "time", SdvinutyeChasy(1.0))
    assert not limiter.is_blocked("ivan@example.com"), "окно не отпустило"


def test_a_successful_login_forgets_the_failures():
    limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=60)
    limiter.record_failure("ivan@example.com")
    limiter.record_failure("ivan@example.com")
    limiter.reset("ivan@example.com")

    assert limiter.tracked() == 0, "после удачного входа след остался"


def test_checking_an_unknown_key_remembers_nothing():
    """Читающая проверка ничего не заводит.

    Она вызывается на каждом входе, в том числе удачном с первого раза. Пока
    она записывала неизвестный ключ пустым списком, след в памяти оставлял
    каждый, кто хоть раз ввёл адрес.
    """
    limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=60)

    for i in range(100):
        assert not limiter.is_blocked(f"nobody-{i}@example.com")

    assert limiter.tracked() == 0, "проверка завела ключи, которых не было"


def test_expired_keys_do_not_pile_up():
    """Перебор с миллионом разных адресов не должен оставаться в памяти.

    Форма входа открыта в интернет, и подбор по ней — не гипотеза, а фон. Ключ
    здесь — присланный адрес, то есть его выбирает тот, кто стучится. Пока
    просроченные записи оставались лежать, память росла на каждый новый адрес и
    не убывала никогда: отказа не видно, процесс просто пухнет.
    """
    # Своё имя, а не общее «default»: ключи ограничителя живут в общем Redis, и
    # на чужом имени этот перебор считал бы заодно всё, что оставили соседние
    # проверки.
    limiter = SlidingWindowLimiter(
        max_attempts=3, window_seconds=0.1, name="sweep-test"
    )
    limiter.ochistit()

    for i in range(SWEEP_AFTER + 200):
        limiter.record_failure(f"attacker-{i}@example.com")

    time.sleep(0.5)   # окно всех этих попыток истекло
    limiter.record_failure("someone-else@example.com")

    # **Свойство одно, а держится оно разными способами, и проверять их надо
    # по-разному.**
    #
    # В памяти процесса просроченное выметает уборка: без неё словарь растёт на
    # каждый новый адрес и не убывает никогда.
    #
    # В Redis уборки нет и не нужно — у каждого ключа стоит срок годности, и
    # сервер снимает их сам. Требовать здесь того же числа значило бы требовать
    #, чтобы Redis успел это сделать за полсекунды при сроке в две: проверка
    # краснела бы на исправном коде. Поэтому здесь проверяется ТО, ОТ ЧЕГО
    # свойство зависит, — что срок годности вообще проставлен.
    if redis_client.get_client() is None:
        assert limiter.tracked() <= SWEEP_AFTER, (
            f"ограничитель помнит {limiter.tracked()} просроченных ключей"
        )
        return

    client = redis_client.get_client()
    # `ttl` отвечает отрицательным на ДВА разных случая, и путать их нельзя:
    # `-1` — ключ есть, а срока у него нет (та самая беда, ради которой
    # написана проверка), `-2` — ключа уже нет вовсе. Второе означает, что срок
    # не просто проставлен, а уже сработал, — то есть лучший из возможных
    # исходов.
    #
    # Условие `< 0` считало вторым первое. Окно здесь ноль целых одна десятая
    # секунды, ключи истекают прямо во время обхода, и между `scan_iter` и
    # `ttl` их успевает не стать: проверка краснела на исправном коде, и тем
    # чаще, чем медленнее машина. Ловилось это как «мигающий тест», хотя код
    # ограничителя был ни при чём.
    bez_sroka = [
        klyuch
        for klyuch in client.scan_iter(match=f"{redis_client.PREFIX}rl:sweep-test:*", count=200)
        if client.ttl(klyuch) == -1
    ]
    assert bez_sroka == [], (
        f"у {len(bez_sroka)} ключей ограничителя нет срока годности — "
        "они останутся в Redis навсегда, а память будет расти на каждый адрес"
    )
    limiter.ochistit()


def test_a_sweep_never_unblocks_someone_who_is_still_trying():
    """Уборка не должна снимать блокировку с того, кто стучится прямо сейчас."""
    limiter = SlidingWindowLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("ivan@example.com")
    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")

    for i in range(SWEEP_AFTER + 10):
        limiter.record_failure(f"noise-{i}@example.com")

    assert limiter.is_blocked("ivan@example.com"), "уборка сняла живую блокировку"
