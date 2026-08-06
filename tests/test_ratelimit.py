"""Ограничитель попыток: блокирует, отпускает и не пухнет.

Ключи ему приносят с улицы — адрес почты из формы входа, адрес посетителя у
PIN. Всё, что растёт от чужих запросов и не убывает, рано или поздно
превращается в способ уронить сервер, ничего при этом не взломав.
"""

import time

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


def test_the_window_lets_go():
    """Окно кончилось — блокировка снята.

    Запас между окном и ожиданием намеренно большой. Windows будит спящий поток
    с шагом около 16 мс, и разница в 10 мс, которая стояла здесь сначала, иногда
    не набиралась: тест падал не на поведении ограничителя, а на точности сна.
    Проверка про «окно кончается», а не про то, за сколько именно.
    """
    limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=0.1)
    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")

    time.sleep(0.5)
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
    limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=0.1)

    for i in range(SWEEP_AFTER + 200):
        limiter.record_failure(f"attacker-{i}@example.com")

    time.sleep(0.5)   # окно всех этих попыток истекло
    limiter.record_failure("someone-else@example.com")

    assert limiter.tracked() <= SWEEP_AFTER, (
        f"ограничитель помнит {limiter.tracked()} просроченных ключей"
    )


def test_a_sweep_never_unblocks_someone_who_is_still_trying():
    """Уборка не должна снимать блокировку с того, кто стучится прямо сейчас."""
    limiter = SlidingWindowLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("ivan@example.com")
    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")

    for i in range(SWEEP_AFTER + 10):
        limiter.record_failure(f"noise-{i}@example.com")

    assert limiter.is_blocked("ivan@example.com"), "уборка сняла живую блокировку"
