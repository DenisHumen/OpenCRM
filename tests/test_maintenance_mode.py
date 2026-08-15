"""Ручной режим обслуживания: root закрывает сайт, себе доступ оставляет.

Это заслон перед всем приложением, и ошибка здесь стоит дороже обычной: пустить
лишнего — значит не закрыть сайт, а закрыть лишнего — значит запереть root'а
снаружи, и снять режим будет уже неоткуда. Проверяем обе стороны.
"""

import pytest
from fastapi.testclient import TestClient

from core.services import maintenance_mode
from tests.conftest import API, ROOT_EMAIL, ROOT_PASSWORD, login
from web.main import app

MAINT = f"{API}/settings/maintenance"


@pytest.fixture
def closed(root_client):
    """Сайт закрыт на работы; после теста открываем обратно."""
    root_client.post(MAINT, json={"enabled": True, "note": "Переносим базу, вернёмся к 14:00"})
    yield
    root_client.post(MAINT, json={"enabled": False})
    maintenance_mode.invalidate()


def test_only_root_can_close_the_site(manager_client):
    assert manager_client.post(MAINT, json={"enabled": True}).status_code == 403


def test_root_keeps_working_while_others_do_not(root_client, manager_client, closed):
    # root ходит везде как обычно — иначе снять режим было бы неоткуда
    assert root_client.get(f"{API}/clients").status_code == 200
    assert root_client.get(f"{API}/dashboard").status_code == 200

    # менеджер упирается в заглушку
    blocked = manager_client.get(f"{API}/clients")
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "maintenance_mode"


def test_visitors_see_the_page_with_the_note(closed):
    page = TestClient(app).get("/")
    assert page.status_code == 503
    assert page.headers.get("Retry-After")
    assert "Переносим базу, вернёмся к 14:00" in page.text


def test_showcase_is_closed_too(root_client, manager_client):
    """Витрина — та же система: клиент не должен видеть её во время работ."""
    board = manager_client.post(f"{API}/boards", json={"title": "Закрытая"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    anon = TestClient(app)
    assert anon.get(f"/b/{share['token']}").status_code == 200

    root_client.post(MAINT, json={"enabled": True})
    try:
        assert anon.get(f"/b/{share['token']}").status_code == 503
    finally:
        root_client.post(MAINT, json={"enabled": False})
        maintenance_mode.invalidate()


def test_root_does_not_pass_through_to_client_facing_pages(root_client, manager_client):
    """Пропуск для root нужен ради одного: войти в CRM и снять режим.

    К витрине это не относится. Раньше относилось — и root, проверяя свою же
    ссылку, видел работающую страницу и заключал, что режим не сработал. Он
    срабатывал; просто проверяющий был единственным, для кого сайт оставался
    открыт. «Закрыто» на клиентской стороне значит закрыто для всех.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Витрина root'а"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    # закрываем только после подготовки: при закрытом сайте менеджер уже не
    # смог бы создать доску
    root_client.post(MAINT, json={"enabled": True})
    try:
        assert root_client.get(f"/b/{share['token']}").status_code == 503
        assert root_client.get("/d/2000-000001").status_code == 503
        # а в CRM root по-прежнему проходит, иначе режим было бы не снять
        assert root_client.get(f"{API}/clients").status_code == 200
    finally:
        root_client.post(MAINT, json={"enabled": False})
        maintenance_mode.invalidate()


def test_client_facing_pages_are_never_cached(manager_client):
    """Ссылку отзывают, срок истекает, сайт закрывают — страница обязана
    перестать открываться. Из кэша браузера она открывалась бы и дальше."""
    board = manager_client.post(f"{API}/boards", json={"title": "Некэшируемая"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "no-store" in page.headers.get("Cache-Control", ""), (
        "витрина кэшируется: отозванная ссылка продолжит открываться у того, "
        "кто успел её загрузить"
    )


def test_root_can_still_sign_in_while_closed(root_client, closed):
    """Самая дорогая ошибка: закрыть вход и остаться снаружи навсегда."""
    fresh = TestClient(app)
    assert fresh.get("/login").status_code == 200          # страница входа открыта
    assert login(fresh, ROOT_EMAIL, ROOT_PASSWORD).status_code == 200
    assert fresh.get(f"{API}/clients").status_code == 200  # и внутри всё работает


def test_healthz_stays_green_while_closed(closed):
    """Иначе docker сочтёт контейнер больным и начнёт его перезапускать."""
    assert TestClient(app).get("/healthz").status_code == 200


def test_healthz_govorit_chto_sayt_zakryt(closed):
    """Закрытый сайт не имеет права выглядеть здоровым.

    Режим включает не только человек: его закрывает и открывает переезд базы
    (`migrate_maintenance` в opencrm.sh). Оборванный переезд оставлял сайт
    закрытым НАВСЕГДА, и не видел этого никто — `/healthz` отвечал
    `status ok, schema ok, redis ok`, докер, автообновление и мониторинг
    молчали. Люди при этом видели разное: сотрудник — 503 даже на чтение
    списка клиентов, публичная главная — 503, а владелец проходит по
    устройству режима и ВИДИТ РАБОЧИЙ САЙТ. То есть единственный, кто может
    открыть, беды и не замечал.

    Код ответа поле не меняет — иначе docker пустил бы контейнер по кругу
    перезапусков поверх объявленных работ, а откат обновления сносил бы
    исправный деплой.
    """
    otvet = TestClient(app).get("/healthz")
    assert otvet.status_code == 200
    assert otvet.json()["maintenance"] == "on", "закрытый сайт выглядит полностью здоровым"


def test_healthz_govorit_chto_sayt_otkryt(root_client):
    """Обратная сторона: поле обязано и молчать, когда молчать положено."""
    root_client.post(MAINT, json={"enabled": False})
    maintenance_mode.invalidate()
    assert TestClient(app).get("/healthz").json()["maintenance"] == "off"


def test_note_is_dropped_when_the_site_reopens(root_client):
    root_client.post(MAINT, json={"enabled": True, "note": "вернёмся к 14:00"})
    root_client.post(MAINT, json={"enabled": False})
    maintenance_mode.invalidate()
    state = root_client.get(MAINT).json()
    assert state["enabled"] is False
    assert state["note"] == "", "прошлое пояснение ввело бы в заблуждение в следующий раз"


def test_who_and_when_are_recorded(root_client, closed):
    """Забытый режим должен быть объясним: видно, кто закрыл и когда."""
    state = root_client.get(MAINT).json()
    assert state["enabled"] is True
    assert state["by"], "не записано, кто закрыл сайт"
    assert state["since"], "не записано, когда закрыли"


def test_cleanup_never_removes_money(root_client, manager_client):
    """Уборка места убирает мусор, но не деньги.

    Удаление клиента уходит каскадом в заявки, и «Очистить место» задним
    числом стирало прошлогоднюю выручку из отчётов — молча: заявок не было ни
    в ответе, ни в журнале. Человек освобождал диск и не знал, что стёр.
    """
    from tests.test_deals import DEALS

    paying = manager_client.post(f"{API}/clients", json={"name": "Заплатил и ушёл"}).json()
    deal = manager_client.post(
        DEALS, json={"title": "Оплаченная", "client_id": paying["id"], "amount": 300_00}
    ).json()
    won = next(
        c for c in manager_client.get(f"{DEALS}/board").json()["columns"] if c["kind"] == "won"
    )["key"]
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": won})

    empty = manager_client.post(f"{API}/clients", json={"name": "Ничего не купил"}).json()

    manager_client.delete(f"{API}/clients/{paying['id']}")
    manager_client.delete(f"{API}/clients/{empty['id']}")

    result = root_client.post(f"{API}/system/storage/purge").json()

    assert result["clients_kept_with_revenue"] >= 1, "клиент с выручкой всё-таки удалён"
    # заявка с деньгами на месте, и отчёт её по-прежнему видит
    assert manager_client.get(f"{DEALS}/{deal['id']}").status_code == 200
    revenue = manager_client.get(f"{API}/reports/revenue", params={"tz_offset": 0}).json()
    assert (revenue["won_amount"] or 0) >= 300_00, "выручка исчезла после уборки"


def test_cleanup_says_how_many_deals_it_took(root_client, manager_client):
    """«Удалено 3 клиента» без числа заявок — половина правды."""
    from tests.test_deals import DEALS

    person = manager_client.post(f"{API}/clients", json={"name": "С незакрытой заявкой"}).json()
    manager_client.post(DEALS, json={"title": "В работе", "client_id": person["id"]})
    manager_client.delete(f"{API}/clients/{person['id']}")

    result = root_client.post(f"{API}/system/storage/purge").json()
    assert "deals" in result, "в ответе нет числа удалённых заявок"
    assert result["deals"] >= 1


def test_cleanup_is_written_into_the_journal(root_client):
    """Единственное место, где данные исчезают навсегда, обязано быть в журнале."""
    # Ищем по действию, а не по приросту числа записей: журнал отдаётся
    # страницей, и в полном прогоне соседние тесты успевают её заполнить —
    # проверка на «стало больше» тогда врёт про свою же причину.
    root_client.post(f"{API}/system/storage/purge")
    entries = root_client.get(
        f"{API}/audit", params={"action": "storage.purged"}
    ).json()["items"]
    if not entries:  # фильтра по действию может не быть — смотрим страницу целиком
        entries = [
            e
            for e in root_client.get(f"{API}/audit").json()["items"]
            if e["action"] == "storage.purged"
        ]
    assert entries, "уборка не попала в журнал"


def test_puti_otkrytye_pri_zakrytom_sayte_vedut_kuda_to(closed):
    """Каждый пропуск обязан вести к чему-то, что приложение вправду отдаёт.

    `MAINTENANCE_OPEN_*` — это список «не применять правило здесь», и мёртвая
    запись в нём дороже прочих: пропуска заведены ради одного — чтобы владелец
    мог войти и СНЯТЬ режим. Переименовали путь, а строка осталась — и
    настоящий путь закрыт заглушкой вместе со всеми.

    Хуже всех `/assets/`: без него страница входа откроется ПУСТОЙ. Разметка
    есть, сборка не загрузилась, кнопки нет, и снять режим неоткуда. Заметить
    это можно только глазами и только в тот единственный раз, когда режим
    включён по-настоящему.

    Проверка структурная: путь обязан быть либо подключённым каталогом
    (`app.mount`), либо приставкой настоящего маршрута. Поведением тут не
    проверить `/assets/` — файлы сборки в наборе могут и не лежать.
    """
    from starlette.routing import Mount

    from web.main import app
    from web.middleware import MAINTENANCE_OPEN_PATHS, MAINTENANCE_OPEN_PREFIXES

    podklyucheno = {r.path.rstrip("/") + "/" for r in app.routes if isinstance(r, Mount)}
    marshruty = _vse_puti(app)

    bezdomnye = []
    for pristavka in MAINTENANCE_OPEN_PREFIXES:
        norma = pristavka.rstrip("/") + "/"
        est_katalog = norma in podklyucheno
        est_marshrut = any(p == pristavka.rstrip("/") or p.startswith(norma) for p in marshruty)
        if not (est_katalog or est_marshrut):
            bezdomnye.append(pristavka)

    for put in MAINTENANCE_OPEN_PATHS:
        # Страницы вроде `/login` рисует SPA — их отдаёт общий перехват, поэтому
        # для них проверка поведением, а не по списку маршрутов.
        if TestClient(app).get(put).status_code == 503:
            bezdomnye.append(put)

    assert bezdomnye == [], (
        "при закрытом сайте эти пути объявлены открытыми, но вести им некуда:\n  "
        + "\n  ".join(bezdomnye)
        + "\nБез них владелец не войдёт и не снимет режим."
    )


def _vse_puti(app) -> set[str]:
    """Пути всех маршрутов, включая подключённые роутеры.

    Плоский перебор `app.routes` показывает единицы вместо полутора сотен: те
    же грабли, что в `tests/test_route_guards.py:api_routes`.
    """
    puti: set[str] = set()

    def sobrat(routes, pristavka=""):
        for r in routes:
            vlozhennyy = getattr(r, "original_router", None)
            if vlozhennyy is not None:
                ctx = getattr(r, "include_context", None)
                sobrat(vlozhennyy.routes, pristavka + (getattr(ctx, "prefix", "") or ""))
                continue
            put = getattr(r, "path", None)
            if put:
                puti.add(pristavka + put)

    sobrat(app.routes)
    return puti
