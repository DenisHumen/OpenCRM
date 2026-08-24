"""Соединений к базе не меньше, чем одновременных обработчиков.

**Это сторож на аварию, которая уже случилась на боевом сервере 24 августа
2026.** Ручки у нас синхронные (`def`, не `async def`), и Starlette уводит такую
в поток. Предел потоков anyio по умолчанию сорок, а соединений у процесса было
десять — четырёхкратный перебор. Сорок обработчиков разом просят соединение из
пула на десять; кто не дождался за `pool_timeout`, получает не медленный ответ, а
`sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 5 reached` и
пятисотую.

Сработало это на витрине доски: три десятка плиток — пачка запросов
`/media/<uid>/thumb.webp` разом, каждая плитка стоит трёх вопросов к базе
(`/media/` проксируется в приложение, а не раздаётся nginx). Ответ сайта вырос до
восьми секунд — это те, кто соединение всё же дождался, — часть плиток отдала
пятисотую, и всё «само прошло», когда пачка кончилась.

**Почему этого не поймал набор.** Тесты ходят по одному запросу за раз: окна,
в котором сорок обработчиков делят десять соединений, там не бывает вовсе.
Нагрузочного прогона у проекта нет и не планируется — значит правило обязано
держаться числами, а не наблюдением.
"""

import anyio
import pytest

from tests.conftest import API

from database.session import (
    BYUDZHET_SOEDINENIY,
    MINIMUM_ODNOVREMENNYH,
    predel_odnovremennyh,
    razmer_pula,
)

#: Числа воркеров, на которых правило проверяется. Один — боевое умолчание.
VORKERY = (1, 2, 3, 4, 8, 12, 16)


def test_u_zhivogo_dvizhka_soedineniy_ne_menshe_chem_odnovremennyh():
    """Главное правило, и сверяем его с ЖИВЫМ движком, а не с формулой.

    Сверять формулу с формулой бессмысленно: `predel_odnovremennyh` сам считается
    из `razmer_pula`, и равенство между ними держится по построению. Такая
    проверка зеленела бы и на возвращённых пяти с пятью — поймано подлогом сразу
    после написания.

    Значение имеет другое: сколько соединений у движка, который приложение
    ДЕЙСТВИТЕЛЬНО завело. Пропиши кто-нибудь числа мимо расчёта — вернётся ровно
    та авария, а формула останется стройной.
    """
    from config.settings import get_settings
    from database.session import engine

    pul = engine.pool
    if not hasattr(pul, "size"):
        pytest.skip("у этого пула нет размера — база не MySQL")

    zhivyh = pul.size() + pul._max_overflow  # noqa: SLF001 — иначе число не спросить
    nuzhno = predel_odnovremennyh(get_settings().workers)
    assert zhivyh >= nuzhno, (
        f"у движка {zhivyh} соединений, а обработчиков разрешено {nuzhno} — "
        "лишние встанут в очередь за соединением и отдадут пятисотую"
    )


@pytest.mark.parametrize("workers", VORKERY)
def test_pul_ne_prevyshaet_potolok_bazy(workers):
    """Все воркеры вместе укладываются в бюджет.

    Упирается в `max_connections` не приложение, а тот, кто пришёл чинить:
    «Too many connections» ровно в момент разбора аварии. Поэтому бюджет ниже
    потолка mysql:8.0 (151), а не равен ему.
    """
    vsego = predel_odnovremennyh(workers) * workers
    # На большом числе воркеров побеждает `MINIMUM_ODNOVREMENNYH` — приложение
    # обязано оставаться пригодным к работе, даже если это стоит запаса. Ставим
    # предел там же, где стоит потолок MySQL за вычетом разовых заходов.
    predel = max(BYUDZHET_SOEDINENIY, MINIMUM_ODNOVREMENNYH * workers)
    assert vsego <= predel, (
        f"{workers} воркеров просят {vsego} соединений — база отдаст "
        "«Too many connections» тому, кто придёт разбираться"
    )


def test_odin_vorker_poluchaet_ves_byudzhet():
    """Боевое умолчание — один воркер, и делить бюджет ему не с кем.

    Пять и пять при одном воркере — это девяносто неиспользованных соединений
    и пятисотые на пачке картинок. Разбор — в шапке файла.
    """
    assert predel_odnovremennyh(1) == BYUDZHET_SOEDINENIY


@pytest.mark.parametrize("workers", (0, None, -3))
def test_bessmyslennoe_chislo_vorkerov_ne_valit_start(workers):
    """Ноль, пусто и отрицательное считаем одним воркером.

    Приложение обязано подняться и с испорченной настройкой: отказ на старте
    из-за опечатки в `OPENCRM_WORKERS` означал бы лежащий сайт там, где хватило
    бы разумного умолчания.
    """
    assert predel_odnovremennyh(workers) == BYUDZHET_SOEDINENIY


def test_predel_potokov_vystavlen_pri_starte():
    """Приложение ВЫСТАВЛЯЕТ предел, а не полагается на умолчание anyio.

    Проверяем вызов, а не наблюдение со стороны: предел живёт в цикле событий, и
    `anyio.run` из теста заводит свой цикл со своим пределом — выставленного в
    работающем приложении он не видит вовсе. Поэтому зовём ту же функцию, что
    зовёт `lifespan`, и читаем результат в ТОМ ЖЕ цикле.
    """
    from config.settings import get_settings
    from web.main import nastroit_predel_potokov

    workers = get_settings().workers

    async def vystavit_i_prochitat():
        vystavleno = nastroit_predel_potokov(workers)
        zhivoy = anyio.to_thread.current_default_thread_limiter().total_tokens
        return vystavleno, zhivoy

    vystavleno, zhivoy = anyio.run(vystavit_i_prochitat)
    assert zhivoy == vystavleno == predel_odnovremennyh(workers), (
        f"предел потоков {zhivoy}, а соединений {predel_odnovremennyh(workers)} — "
        "обработчики снова будут ждать соединение до отказа пула"
    )


def test_lifespan_zovyot_nastroyku_predela():
    """И `lifespan` эту настройку правда зовёт.

    Без этого проверка выше стерегла бы функцию, которую никто не вызывает, —
    правило объявлено и не применено, а именно так авария и выглядела.
    """
    import ast
    import inspect

    from web import main as web_main

    derevo = ast.parse(inspect.getsource(web_main.lifespan))
    zovyot = {
        u.func.id
        for u in ast.walk(derevo)
        if isinstance(u, ast.Call) and isinstance(u.func, ast.Name)
    }
    assert "nastroit_predel_potokov" in zovyot, (
        "`lifespan` больше не выставляет предел потоков — приложение вернётся к "
        "сорока обработчикам на десять соединений"
    )


def test_predel_ne_vzyat_iz_umolchaniya_anyio():
    """И это значение не должно совпадать с умолчанием сорок случайно.

    Иначе проверка выше зеленела бы и на неприменённом правиле. Сорок — это
    ровно то число, при котором авария и случилась, поэтому если расчёт когда-то
    его даст, здесь нужен новый довод, а не молчание.
    """
    assert predel_odnovremennyh(1) != 40, (
        "расчёт совпал с умолчанием anyio — проверка «предел выставлен» "
        "перестала что-либо доказывать"
    )


# --- сколько соединений стоит ОДИН запрос -------------------------------------


def test_zapros_zanimaet_odno_soedinenie(root_client):
    """Один запрос — одно соединение к базе. Не два.

    **Это сторож на ту самую аварию, и он единственный смотрит на неё прямо.**
    Посредник режима обслуживания (`web/middleware.MaintenanceMode`) открывал
    сессию и звал приложение ВНУТРИ неё: на обычном пути (режим выключен, то
    есть всегда) соединение держалось весь запрос целиком. Значит каждый запрос
    стоил двух соединений — своего у обработчика и чужого у посредника.

    Это не «в полтора раза теснее», это взаимная блокировка: пачка из десяти
    запросов при пуле на десять занимает по первому соединению каждым и ждёт
    второго — до `pool_timeout`, тридцать секунд, после чего отдаёт
    `QueuePool limit ... reached` и пятисотую.

    Набор такого не ловит по устройству: он ходит по одному запросу за раз, и
    второму соединению не с кем соперничать. Поэтому считаем не время и не
    отказ, а САМО ЧИСЛО занятых соединений в тот миг, когда обработчик работает.

    **Кэш посредника сбрасываем нарочно.** `maintenance_mode.state` помнит ответ
    две секунды, и на горячем кэше посредник в базу не ходит вовсе — сессия
    берёт соединение лениво, при первом запросе. Проверка без сброса оставалась
    зелёной и на сломанном посреднике: поймано подлогом сразу после написания.
    Живой сервер кэш остужает сам, раз в две секунды, и тогда посредник платит
    настоящим соединением — а держал он его весь запрос.
    """
    from fastapi import Depends
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import Session

    from core.services import maintenance_mode

    from database.session import engine
    from web.api.deps import get_db
    from web.main import app

    pul = engine.pool
    if not hasattr(pul, "checkedout"):
        pytest.skip("у этого пула нет счётчика занятых — база не MySQL")

    zamer: dict[str, int] = {}

    def _skolko_zanyato(db: Session = Depends(get_db)):
        # Сходить в базу обязательно: сессия берёт соединение ЛЕНИВО, при первом
        # запросе. Без этого счётчик показал бы ноль и на сломанном посреднике.
        db.execute(sa_text("SELECT 1"))
        # Считаем ИЗНУТРИ запроса: снаружи занятых уже нет.
        zamer["vnutri"] = pul.checkedout()
        return {"ok": True}

    # В НАЧАЛО списка, а не через `@app.get`: перехватчик SPA зарегистрирован
    # последним и ловит всё, что до него не разобрали, — маршрут, добавленный
    # после него, отвечал бы 404.
    from fastapi.routing import APIRoute

    app.router.routes.insert(
        0,
        APIRoute("/api/v1/__proverka_soedineniy", endpoint=_skolko_zanyato, methods=["GET"]),
    )
    try:
        # Остужаем кэш посредника: на горячем он до базы не доходит.
        maintenance_mode._cache = None  # noqa: SLF001 — иначе окна не воспроизвести
        maintenance_mode._cached_at = 0.0  # noqa: SLF001
        do = pul.checkedout()
        otvet = root_client.get(f"{API}/__proverka_soedineniy")
        assert otvet.status_code == 200, otvet.text
    finally:
        # Убираем временный маршрут: реестр у приложения общий на весь прогон.
        app.router.routes[:] = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/api/v1/__proverka_soedineniy"
        ]
        app.openapi_schema = None

    zanyato_zaprosom = zamer["vnutri"] - do
    assert zanyato_zaprosom == 1, (
        f"один запрос занял {zanyato_zaprosom} соединения вместо одного.\n"
        "Два означают взаимную блокировку под нагрузкой: пачка запросов займёт "
        "по первому соединению каждым и будет ждать второго до отказа пула."
    )
