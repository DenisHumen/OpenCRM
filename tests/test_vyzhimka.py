"""Отчёт о неудаче обязан рассказывать о неудаче.

Проверки ниже написаны по живому случаю 25.08.2026: обновление откатилось на
шаге `tests`, владельцу пришло сообщение, и в нём про тесты не было ни слова —
только девять строк об остановке контейнеров и два сообщения mysqld о
завершении работы. Причина была структурной, а не случайной: компоуз пишет ход
контейнеров в поток ошибок, `Result.text` приклеивает поток ошибок ПОСЛЕ
обычного, и хвост вывода — это гарантированно уборка.

Поэтому первая проверка здесь — не «выжимка что-то вернула», а «в выжимке есть
имя упавшего теста, и НЕТ строк об остановке контейнеров». Проверка, довольная
любым непустым ответом, пропустила бы ровно ту беду, ради которой написана.
"""

from __future__ import annotations

import re

import pytest

from deploy import vyzhimka

#: Вывод шлюза деплоя ровно в том виде, в каком он склеивается в `Result.text`:
#: сначала весь вывод pytest, потом весь ход контейнеров.
ZHIVOY_SLUCHAY = """tests-1        | ============================= test session starts ==============================
tests-1        | platform linux -- Python 3.12.7, pytest-8.3.3
tests-1        | collected 1834 items
tests-1        |
tests-1        | tests/test_audit.py ................................              [  3%]
tests-1        | tests/test_speed.py .....F.......                                 [ 41%]
tests-1        |
tests-1        | =================================== FAILURES ===================================
tests-1        | ______________ test_otkrytie_vkladki_ne_rastyot_ot_obyoma _______________
tests-1        | E   AssertionError: /api/v1/deals: 61 запрос при потолке 44
tests-1        | E   assert 61 <= 44
tests-1        | =========================== short test summary info ============================
tests-1        | FAILED tests/test_speed.py::test_otkrytie_vkladki_ne_rastyot_ot_obyoma
tests-1        | ======================= 1 failed, 1833 passed in 512.44s =======================
 Container opencrm-tests-db-test-1  Healthy
 Container opencrm-tests-tests-1  Starting
 Container opencrm-tests-tests-1  Started
 Compose  Stopping
Aborting on container exit...
 Container opencrm-tests-tests-1  Stopping
 Container opencrm-tests-tests-1  Stopped
 Container opencrm-tests-redis-test-1  Stopping
 Container opencrm-tests-db-test-1  Stopping
db-test-1     | 2026-08-25T11:02:29.187649Z 0 [System] [MY-013172] [Server] Received SHUTDOWN from user <via user signal>. Shutting down mysqld (Version: 8.0.46).
 Container opencrm-tests-redis-test-1  Stopped
db-test-1     | 2026-08-25T11:02:29.933103Z 0 [System] [MY-010910] [Server] /usr/sbin/mysqld: Shutdown complete (mysqld 8.0.46)  MySQL Community Server - GPL.
 Container opencrm-tests-db-test-1  Stopped"""


def test_v_vyrezke_est_upavshiy_test_a_ne_uborka():
    """Тот самый случай: имя теста внутри, остановка контейнеров снаружи."""
    stroki, sposob = vyzhimka.vyzhat(ZHIVOY_SLUCHAY, 12)
    vmeste = "\n".join(stroki)

    assert "test_otkrytie_vkladki_ne_rastyot_ot_obyoma" in vmeste, (
        "в вырезке нет имени упавшего теста — то есть отчёт снова ни о чём:\n" + vmeste
    )
    assert "61 запрос при потолке 44" in vmeste, (
        "в вырезке нет текста утверждения, а он и есть ответ на вопрос «почему»:\n" + vmeste
    )
    assert "Stopped" not in vmeste and "Stopping" not in vmeste, (
        "ход остановки контейнеров вытеснил собой разбор — ровно то, что чинилось:\n" + vmeste
    )
    assert "mysqld" not in vmeste, "журнал базы к падению теста отношения не имеет"
    assert sposob, "способ выбора обязан быть назван"


def test_hvost_toy_zhe_dliny_soderzhit_odnu_uborku():
    """Контрольный замер: чем был плох прежний способ.

    Проверка не про новый код, а про старый, и стоит она здесь нарочно. Без
    неё «выжимка находит имя теста» звучит как случайная удача; рядом с ней
    видно, что прежний способ не мог найти его в принципе.
    """
    hvost = "\n".join(ZHIVOY_SLUCHAY.splitlines()[-12:])
    assert "test_otkrytie_vkladki_ne_rastyot_ot_obyoma" not in hvost
    assert "FAILED" not in hvost


def test_hod_kontejnerov_uznayotsya_bez_pytest():
    """Вывод из одной уборки — честно назван уборкой, а не разбором."""
    tolko_uborka = "\n".join(ZHIVOY_SLUCHAY.splitlines()[14:])
    stroki, sposob = vyzhimka.vyzhat(tolko_uborka, 12)
    assert "FAILED" not in "\n".join(stroki)
    # Ключевое — не что показано, а что способ НЕ выдаёт это за найденную причину.
    assert "признак" not in sposob, (
        f"уборка выдана за разбор: {sposob!r}"
    )


def test_pustoy_vyvod_nazyvaetsya_pustym():
    stroki, sposob = vyzhimka.vyzhat("", 12)
    assert stroki == []
    assert "не было" in sposob


def test_pristavka_sluzhby_ne_meshaet_poisku():
    """`tests-1  | FAILED …` — то же падение, что и `FAILED …`.

    Без снятия приставки не работает ничего: в шлюзе деплоя pytest ВСЕГДА идёт
    через компоуз, то есть всегда с приставкой. Проверка держит именно это —
    забытая приставка означала бы, что выжимка исправна только в тех условиях,
    в которых её никогда не зовут.
    """
    bez = "FAILED tests/test_a.py::test_b - assert 1 == 2"
    s_pristavkoy = "tests-1        | " + bez
    _, sposob_bez = vyzhimka.vyzhat(bez, 5)
    _, sposob_s = vyzhimka.vyzhat(s_pristavkoy, 5)
    assert sposob_bez == sposob_s, (
        f"приставка службы сбила поиск: {sposob_bez!r} против {sposob_s!r}"
    )


def test_pervoe_padenie_vazhnee_poslednego():
    """Из сорока падений показывается начало списка, а не конец.

    Довод записан в `vyzhat`: последующие падения обычно вызваны первым, и
    хвост списка не объясняет ничего. Проверка держит именно направление
    обрезки — если однажды её развернут, отчёт станет показывать следствия
    вместо причины и никто этого не заметит.
    """
    stroki = [f"FAILED tests/test_{n:02d}.py::test_x - assert {n}" for n in range(40)]
    vidno, sposob = vyzhimka.vyzhat("\n".join(stroki), 6)
    vmeste = "\n".join(vidno)
    assert "test_00.py" in vmeste, "первое падение потерялось"
    assert "test_39.py" not in vmeste
    assert "из" in sposob, f"умолчали, что показано не всё: {sposob!r}"


def test_traceback_pokazyvaetsya_s_tekstom_isklyucheniya():
    """Одна строка `Traceback` без последующих — бесполезна."""
    vyvod = "\n".join([
        "app-1  | обычная работа",
        "app-1  | Traceback (most recent call last):",
        "app-1  |   File \"/app/web/main.py\", line 10, in <module>",
        "app-1  |     raise RuntimeError('схема не сошлась')",
        "app-1  | RuntimeError: схема не сошлась",
    ])
    stroki, _ = vyzhimka.vyzhat(vyvod, 12)
    assert "схема не сошлась" in "\n".join(stroki), (
        "трассировка показана без текста исключения — то есть без ответа"
    )


@pytest.mark.parametrize("stroka", [
    " Container opencrm-tests-db-test-1  Stopped",
    " Container opencrm-tests-tests-1  Starting",
    " Network opencrm-tests_default  Created",
    "Aborting on container exit...",
    "db-test-1     | 2026-08-25T11:02:29.187649Z 0 [System] [MY-013172] [Server] Received SHUTDOWN",
    "#12 DONE 0.3s",
    "#8 CACHED",
    " => [internal] load build definition from Dockerfile",
])
def test_shum_uznayotsya(stroka):
    assert vyzhimka.ochistit([stroka]) == [], f"не опознано как ход дела: {stroka!r}"


@pytest.mark.parametrize("stroka", [
    "FAILED tests/test_a.py::test_b",
    "E   AssertionError: 61 <= 44",
    "ERROR: failed to solve: process did not complete successfully",
    "sqlalchemy.exc.OperationalError: (2003, \"Can't connect to MySQL server\")",
    "Traceback (most recent call last):",
])
def test_beda_ne_schitaetsya_shumom(stroka):
    """Обратная сторона: чистка не имеет права съесть строку беды.

    Проверка стоит рядом с предыдущей нарочно. Список шума легко расширить «на
    всякий случай», и тогда отчёт снова опустеет — но уже тихо, без единого
    красного теста. Эта пара делает такое расширение видимым.
    """
    assert vyzhimka.ochistit([stroka]) == [stroka]


def test_kod_137_perevoditsya_v_nehvatku_pamyati():
    """`exited with code 137` — это не число, а диагноз, и его надо назвать.

    Живой повод прямой: на сервере с 7937 МБ шлюз тестов поднимает рядом с
    боевым стеком ещё одну MySQL с гигабайтом tmpfs, Redis и сам набор. Если
    ядро уносит контейнер, компоуз печатает 137 и молчит, а человек по этому
    числу решает, чинить ему тест или добавлять памяти. Это противоположные
    действия, и ошибиться здесь стоит часа.
    """
    vyvod = "\n".join([
        " Container opencrm-tests-tests-1  Started",
        " Container opencrm-tests-tests-1  Stopping",
        " Container opencrm-tests-tests-1  Stopped",
        "tests-1 exited with code 137",
    ])
    stroki, sposob = vyzhimka.vyzhat(vyvod, 12)
    vmeste = "\n".join(stroki)
    assert "137" in vmeste
    assert "памяти" in vmeste, f"код не переведён, читателю оставлено число: {vmeste}"
    assert "выхода" in sposob, f"способ не назвал код выхода: {sposob!r}"


def test_kod_vyhoda_idyot_pervoy_strokoy():
    """Итог прогона важнее любой строки вывода и потому стоит наверху."""
    vyvod = "\n".join([
        "tests-1  | FAILED tests/test_a.py::test_b - assert 1 == 2",
        "tests-1 exited with code 137",
    ])
    stroki, _ = vyzhimka.vyzhat(vyvod, 12)
    assert "137" in stroki[0], f"код выхода не первый: {stroki}"
    assert any("FAILED" in s for s in stroki), "падение потерялось ради кода выхода"


def test_nulevoy_kod_vyhoda_ne_shumit():
    """`exited with code 0` — это ход дела, и в вырезке ему места нет."""
    assert vyzhimka.kody_vyhoda(["tests-1 exited with code 0"]) == []
    assert vyzhimka.kody_vyhoda(["[Kdb-test-1 exited with code 0"]) == []


def test_neznakomyy_kod_vsyo_ravno_nazyvaetsya():
    """Кода нет в словаре — но сам факт ненулевого выхода назвать обязаны."""
    nayden = vyzhimka.kody_vyhoda(["tests-1 exited with code 42"])
    assert len(nayden) == 1
    assert "42" in nayden[0]


def test_kazhdyy_priznak_hot_by_raz_srabatyvaet():
    """Признак, который ничего не ловит, — это мёртвая строка, а не сторож.

    Проверка обратная обычной: она требует, чтобы у КАЖДОГО образца из
    `PRIZNAKI` нашёлся пример, на котором он срабатывает. Тот же приём, что у
    `tests/test_db_boundary.py` со списком исключений: без него набор образцов
    год за годом растёт и незаметно наполняется тем, что уже не встречается.
    """
    primery = [
        "=========================== short test summary info ============================",
        "======================= 1 failed, 1833 passed in 512.44s =======================",
        "FAILED tests/test_a.py::test_b",
        "E   AssertionError: 61 <= 44",
        "Traceback (most recent call last):",
        "ERROR: failed to solve: process did not complete successfully",
        "RuntimeError: схема не сошлась",
        "app-1  | ERROR при разборе настроек",
    ]
    for imya, obrazec in vyzhimka.PRIZNAKI:
        assert any(obrazec.search(re.sub(r"^\S+-\d+\s*\|\s?", "", p)) for p in primery), (
            f"признак «{imya}» не ловит ни одного примера — он мёртв"
        )
