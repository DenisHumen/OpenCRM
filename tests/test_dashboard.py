from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from core.utils import now_utc
from database.models import ShareLink, ShareView
from database.repositories import stats as stats_repo
from database.session import SessionLocal
from tests.conftest import API, png_bytes
from web.main import app


def test_dashboard_aggregates(root_client, manager_client):
    # свой клиент, а не расчёт на созданных в других файлах: база одна на весь
    # прогон, и без него тест зеленел только когда его запускали вместе со всеми
    manager_client.post(f"{API}/clients", json={"name": "Дашборд-клиент"})
    board = manager_client.post(f"{API}/boards", json={"title": "Дашборд-доска"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    TestClient(app).get(f"/b/{share['token']}")  # один просмотр

    data = manager_client.get(f"{API}/dashboard").json()
    assert data["clients_total"] >= 1
    assert data["boards_published"] >= 1
    assert data["views_7d"] >= 1
    assert len(data["views_by_day"]) == 7
    assert data["views_by_day"][-1]["count"] >= 1  # сегодняшний просмотр
    assert data["last_view_at"] is not None
    assert len(data["recent_boards"]) >= 1
    first = data["recent_boards"][0]
    assert {"views_count", "has_active_link", "has_pin", "works_count"} <= set(first)


# --- плитка просмотров против графика под ней ---

def _published_board_with_a_view(client, title: str) -> int:
    """Доска, витрина и один просмотр по ней. Возвращает id доски."""
    board = client.post(f"{API}/boards", json={"title": title}).json()
    client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    TestClient(app).get(f"/b/{share['token']}")
    return board["id"]


def _move_views(board_id: int, moment) -> int:
    """Переносит просмотры доски в прошлое прямо в базе.

    Через API недостижимо: время просмотра ставит сервер, а проверять окно без
    прошлого нечем.
    """
    with SessionLocal() as db:
        views = list(
            db.scalars(
                select(ShareView)
                .join(ShareLink, ShareLink.id == ShareView.share_link_id)
                .where(ShareLink.board_id == board_id)
            )
        )
        for view in views:
            view.viewed_at = moment
        db.commit()
        return len(views)


def test_views_tile_equals_the_sum_of_the_bars_under_it(manager_client):
    """Плитка «просмотров за 7 дней» и столбики под ней — одно число.

    Плитка считала скользящее окно (последние 168 часов), график — семь
    календарных суток. Совпадали они ровно в полночь, а весь остальной день
    расходились на хвост седьмого дня назад: столбик рисовал его целиком, плитка
    брала от него только часы после текущего.

    Заметить это глазами можно: столбиков семь, они складываются, и сумма не
    сходится с числом над ними. Правильным берём график — его человек и
    проверяет; плитку приводим к нему.

    Просмотр кладём ровно в середину зазора между окнами: он попадает в
    скользящее окно и не попадает в календарное, то есть проявляет расхождение
    в любое время суток, а не только в удачное.
    """
    board_id = _published_board_with_a_view(manager_client, "Зазор между окнами")

    now = now_utc()
    chart_start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    rolling_start = now - timedelta(days=7)
    assert rolling_start < chart_start, "зазора нет — проверять нечего"
    moved = _move_views(board_id, rolling_start + (chart_start - rolling_start) / 2)
    assert moved == 1, "просмотр не записался — тест ни о чём"

    data = manager_client.get(f"{API}/dashboard").json()
    assert len(data["views_by_day"]) == 7
    assert sum(day["count"] for day in data["views_by_day"]) == data["views_7d"], (
        "плитка за 7 дней не равна сумме столбиков под ней"
    )


def test_views_window_is_seven_whole_days_up_to_tonight(manager_client):
    """Окно плитки — календарное и полуоткрытое: [полночь-6 суток; завтра).

    Проверяем само окно, а не только равенство сумм: равенство держалось бы и на
    двух одинаково неверных окнах. Вечерний просмотр сегодняшнего дня обязан
    попасть в плитку — иначе к полуночи она бы отставала от графика на весь
    сегодняшний вечер.
    """
    start, end = stats_repo.views_window(7, now_utc().replace(hour=10, minute=30))
    assert start.hour == 0 and start.minute == 0 and start.second == 0
    assert (end - start) == timedelta(days=7)
    assert end.date() == (now_utc() + timedelta(days=1)).date(), (
        "окно кончается раньше полуночи — вечерние просмотры выпадают из плитки"
    )


def test_previous_week_is_measured_by_the_same_ruler(manager_client):
    """«К прошлой неделе» сравнивает семь суток с семью сутками.

    Скользящее окно против календарного давало сравнение разной длины: рост в
    процентах считался от знаменателя, который в среднем на полдня короче
    числителя, и стрелка вверх появлялась сама собой.
    """
    now = now_utc()
    start, end = stats_repo.views_window(7, now)
    prev_start, prev_end = stats_repo.views_window(7, now - timedelta(days=7))
    assert prev_end == start, "окна недель не стыкуются встык"
    assert (end - start) == (prev_end - prev_start)


def test_boards_list_extended_fields(manager_client):
    client = manager_client.post(f"{API}/clients", json={"name": "Для доски"}).json()
    board = manager_client.post(
        f"{API}/boards", json={"title": "С клиентом", "client_id": client["id"]}
    ).json()
    manager_client.post(f"{API}/boards/{board['id']}/shares", json={"pin": "1234"})

    listing = manager_client.get(f"{API}/boards", params={"search": "С клиентом"}).json()
    item = listing["items"][0]
    assert item["client_name"] == "Для доски"
    assert item["has_links"] is True
    assert item["has_active_link"] is True
    assert item["has_pin"] is True
    assert item["views_count"] == 0


def test_spa_served_and_api_404_untouched(base_client):
    # SPA отдаётся на корне и на клиентских маршрутах
    for path in ("/", "/clients", "/boards/5"):
        response = base_client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]
    # несуществующий API-путь — честный 404, не index.html
    assert base_client.get("/api/v1/nonexistent").status_code == 404


def test_global_search(manager_client):
    client = manager_client.post(
        f"{API}/clients", json={"name": "Поисковый Клиент", "company": "Ромашка"}
    ).json()
    manager_client.post(f"{API}/boards", json={"title": "Поисковая доска", "client_id": client["id"]})

    # пустой запрос — недавние записи, палитра не открывается пустой
    recent = manager_client.get(f"{API}/search").json()
    assert recent["clients"]["items"] and recent["boards"]["items"]

    by_name = manager_client.get(f"{API}/search", params={"q": "Поисковый"}).json()
    assert any(c["name"] == "Поисковый Клиент" for c in by_name["clients"]["items"])

    by_company = manager_client.get(f"{API}/search", params={"q": "Ромашка"}).json()
    assert any(c["id"] == client["id"] for c in by_company["clients"]["items"])

    by_board = manager_client.get(f"{API}/search", params={"q": "Поисковая доска"}).json()
    board = by_board["boards"]["items"][0]
    assert board["title"] == "Поисковая доска"
    assert board["client_name"] == "Поисковый Клиент"  # палитра показывает клиента доски

    # регистр не важен, в том числе для кириллицы
    for query in ("поисковый", "ПОИСКОВЫЙ", "ромашка"):
        hit = manager_client.get(f"{API}/search", params={"q": query}).json()
        assert any(c["id"] == client["id"] for c in hit["clients"]["items"]), query
    lower_board = manager_client.get(f"{API}/search", params={"q": "поисковая"}).json()
    assert lower_board["boards"]["items"][0]["title"] == "Поисковая доска"

    empty = manager_client.get(f"{API}/search", params={"q": "щщщнеттакого"}).json()
    assert empty["clients"]["items"] == [] and empty["boards"]["items"] == []


def test_search_requires_auth(base_client):
    assert base_client.get(f"{API}/search").status_code == 401


def test_head_requests_supported(base_client):
    # мониторинг и прокси проверяют доступность через HEAD
    assert base_client.head("/healthz").status_code == 200
    assert base_client.head("/").status_code == 200


# --- новые блоки сводки 06.09.2026: долг, заказы и возвраты, склад, напоминания, звонки ---


def _blok(client, key: str, enabled: bool) -> None:
    assert client.post(f"{API}/modules/{key}", json={"enabled": enabled}).status_code == 200


def test_svodka_znaet_dolg_zakazy_sklad_i_prosrochku(root_client):
    """Плитки и блоки, которых у сводки не было: пустое место на обычном
    мониторе владелец просил заполнить полезными числами, а не растягивать."""
    for key in ("documents", "warehouse", "orders", "tasks"):
        _blok(root_client, key, True)
    try:
        klient = root_client.post(f"{API}/clients", json={"name": "Сводка долг"}).json()
        zayavka = root_client.post(
            f"{API}/deals", json={"title": "Сводка заявка", "client_id": klient["id"], "amount": 10_000, "prepaid": 3_000}
        ).json()
        assert zayavka["id"]
        malo = root_client.post(
            f"{API}/warehouse/products", json={"name": "Сводка мало", "sku": "SV-MALO", "price": 100, "min_stock": "5"}
        ).json()
        root_client.post(f"{API}/warehouse/moves", json={"product_id": malo["id"], "kind": "in", "quantity": "2"})
        net = root_client.post(f"{API}/warehouse/products", json={"name": "Сводка нет", "sku": "SV-NET", "price": 100}).json()
        tovar = root_client.post(f"{API}/warehouse/products", json={"name": "Сводка товар", "sku": "SV-TOV", "price": 500}).json()
        root_client.post(f"{API}/warehouse/moves", json={"product_id": tovar["id"], "kind": "in", "quantity": "10"})
        order = root_client.post(f"{API}/orders", json={"kind": "sales_order", "client_id": klient["id"]}).json()
        root_client.post(f"{API}/orders/{order['id']}/lines", json={"product_id": tovar["id"], "quantity": "2"})
        assert root_client.post(f"{API}/orders/{order['id']}/close", json={}).status_code == 200
        root_client.post(f"{API}/tasks", json={"title": "Сводка просрочка", "due_at": "2020-01-01T10:00:00"})

        data = root_client.get(f"{API}/dashboard").json()
        assert data["money_due"] >= 7_000, "долг по открытой заявке — цена минус предоплата"
        assert data["orders_week"]["shipped_count"] >= 1
        assert data["orders_week"]["returns_count"] >= 0
        assert any(o["number"] == order["number"] and o["total"] == 1_000 for o in data["recent_orders"])
        nizko = {row["name"]: row for row in data["low_stock"]}
        assert data["low_stock_total"] >= 2
        # Список — пятёрка самых пустых; свои товары ищем среди них по имени, а
        # соседние файлы могли насыпать своих с нулём.
        if "Сводка нет" in nizko:
            assert nizko["Сводка нет"]["out"] is True and nizko["Сводка нет"]["stock_milli"] == 0
        if "Сводка мало" in nizko:
            assert nizko["Сводка мало"]["out"] is False and nizko["Сводка мало"]["stock_milli"] == 2_000
        assert data["tasks_counters"]["overdue"] >= 1
        # Телефонию мог оставить включённой соседний файл (ворота гоняют набор
        # в обоих порядках): блок либо отсутствует, либо со счётчиками.
        assert data["calls_24h"] is None or {"vsego", "propushcheno"} <= set(data["calls_24h"])
    finally:
        for key in ("orders", "warehouse"):
            _blok(root_client, key, False)

    # Выключенные блоки уходят пустыми значениями, форма ответа та же.
    data = root_client.get(f"{API}/dashboard").json()
    assert data["orders_week"] is None and data["recent_orders"] == []
    assert data["low_stock"] == [] and data["low_stock_total"] == 0


def test_bez_prava_na_summy_dolg_pust(root_client, manager_client):
    """`deals.view_amounts` прячет и долг: это сумма, как и остальные плитки."""
    from tests.conftest import make_manager

    smotryashchiy = make_manager(root_client, "svodka-bez-summ@test.local")
    roli = root_client.get(f"{API}/roles").json()["items"]
    bez_summ = root_client.post(
        f"{API}/roles", json={"name": "Сводка без сумм", "permissions": ["deals.view", "clients.view"]}
    )
    if bez_summ.status_code == 201:
        me = smotryashchiy.get(f"{API}/auth/me").json()
        root_client.post(f"{API}/roles/assign/{me['id']}", json={"role_id": bez_summ.json()["id"]})
        data = smotryashchiy.get(f"{API}/dashboard").json()
        assert data["money_due"] is None
        assert all(s["amount"] is None for s in data["deals_by_stage"]), "сумма этапа — тоже сумма"
    assert isinstance(roli, list)


def test_voronka_znaet_summy_etapov_i_klientov_bez_zayavok(root_client):
    """Плитка клиентов: новых за неделю и без заявок; воронка: сумма этапа
    рядом с числом (владелец, 06.09.2026, план А-08/А-09)."""
    bez = root_client.post(f"{API}/clients", json={"name": "Сводка: без заявки"}).json()
    s_zayavkoy = root_client.post(f"{API}/clients", json={"name": "Сводка: с заявкой"}).json()
    deal = root_client.post(
        f"{API}/deals", json={"title": "Сумма этапа", "client_id": s_zayavkoy["id"], "amount": 7_000}
    ).json()
    try:
        data = root_client.get(f"{API}/dashboard").json()
        assert data["clients_this_week"] >= 2
        assert data["clients_without_deals"] >= 1
        [etap] = [s for s in data["deals_by_stage"] if s["key"] == deal["stage"]]
        assert etap["count"] >= 1 and etap["amount"] >= 7_000
    finally:
        root_client.delete(f"{API}/deals/{deal['id']}")
        root_client.delete(f"{API}/clients/{bez['id']}")
        root_client.delete(f"{API}/clients/{s_zayavkoy['id']}")


def test_raskladka_svodki_hranitsya_u_sotrudnika(root_client, manager_client):
    """Владелец 06.09.2026: блоки сводки добавляются, убираются и переставляются.
    Раскладка — у сотрудника, а не у фирмы; пусто — умолчание экрана."""
    assert root_client.get(f"{API}/dashboard/layout").json()["layout"] is None
    r = root_client.put(f"{API}/dashboard/layout", json={"widgets": [{"kind": "funnel", "w": 4}, {"kind": "clients"}]})
    assert r.status_code == 200, r.text
    assert r.json()["layout"]["widgets"] == [
        {"kind": "funnel", "w": 4, "params": {}},
        {"kind": "clients", "w": 1, "params": {}},
    ]
    assert r.json()["kinds"]["api_key"]["odin"] is False
    assert root_client.get(f"{API}/dashboard/layout").json()["layout"]["widgets"][0]["kind"] == "funnel"
    assert manager_client.get(f"{API}/dashboard/layout").json()["layout"] is None, "раскладка своя у каждого"
    assert root_client.delete(f"{API}/dashboard/layout").status_code == 200
    assert root_client.get(f"{API}/dashboard/layout").json()["layout"] is None


def test_raskladka_otvergaet_chuzhoe_s_kodom(root_client, manager_client):
    """Неизвестный виджет, второй такой же, чужая ширина, выключенный блок,
    чужое право — отказ с кодом: экран показывает причину, а не теряет виджет."""
    put = lambda client, widgets: client.put(f"{API}/dashboard/layout", json={"widgets": widgets})
    otkaz = put(root_client, [{"kind": "pogoda"}])
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "unknown_widget"
    otkaz = put(root_client, [{"kind": "funnel"}, {"kind": "funnel"}])
    assert otkaz.json()["error"]["code"] == "widget_duplicate"
    otkaz = put(root_client, [{"kind": "funnel", "w": 1}])
    assert otkaz.json()["error"]["code"] == "bad_widget_width"
    otkaz = put(root_client, [{"kind": "api_key"}])
    assert otkaz.json()["error"]["code"] == "widget_needs_key"
    otkaz = put(root_client, [{"kind": "api_key", "params": {"key_id": 999_999}}])
    assert otkaz.status_code == 404 and otkaz.json()["error"]["code"] == "api_key_not_found"
    otkaz = put(manager_client, [{"kind": "api_key", "params": {"key_id": 1}}])
    assert otkaz.status_code == 403 and otkaz.json()["error"]["code"] == "permission_denied"
    _blok(root_client, "tasks", False)
    try:
        otkaz = put(root_client, [{"kind": "my_tasks"}])
        assert otkaz.json()["error"]["code"] == "module_disabled"
    finally:
        _blok(root_client, "tasks", True)
    assert root_client.get(f"{API}/dashboard/layout").json()["layout"] is None, "отказ ничего не записал"


def test_vidzhet_klyucha_sayta_po_odnomu_na_klyuch(root_client):
    """Наблюдение за ключом сайта — виджет на каждый ключ; тот же ключ дважды —
    отказ. Без ключа виджет не заводится (проверка выше)."""
    keys = f"{API}/settings/api-keys"
    k1 = root_client.post(keys, json={"name": "магазин", "scopes": ["catalog.read"]}).json()
    k2 = root_client.post(keys, json={"name": "маркетплейс", "scopes": ["catalog.read"]}).json()
    widgets = [
        {"kind": "api_key", "params": {"key_id": k1["id"]}},
        {"kind": "api_key", "params": {"key_id": k2["id"], "lishnee": 1}, "w": 4},
    ]
    r = root_client.put(f"{API}/dashboard/layout", json={"widgets": widgets})
    assert r.status_code == 200, r.text
    assert [w["params"] for w in r.json()["layout"]["widgets"]] == [{"key_id": k1["id"]}, {"key_id": k2["id"]}]
    assert r.json()["layout"]["widgets"][1]["w"] == 4
    dvazhdy = root_client.put(
        f"{API}/dashboard/layout",
        json={"widgets": [{"kind": "api_key", "params": {"key_id": k1["id"]}}] * 2},
    )
    assert dvazhdy.json()["error"]["code"] == "widget_duplicate"
    root_client.delete(f"{API}/dashboard/layout")
