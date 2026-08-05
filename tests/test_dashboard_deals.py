"""Дашборд отвечает на вопросы бизнеса, а не портфолио.

Просмотры витрин — метрика сайта работ, и владелец сверяет по ним не выручку.
Поэтому проверяем то, из-за чего дашборду перестают верить: что средний чек не
занижается сделками без цены, что пустой этап воронки видно, и что «мои задачи»
действительно мои.
"""

from tests.conftest import API
from tests.test_deals import DEALS, make_client

DASH = f"{API}/dashboard"
TASKS = f"{API}/tasks"


def stages(client):
    board = client.get(f"{DEALS}/board").json()
    return (
        next(c for c in board["columns"] if c["kind"] == "open"),
        next(c for c in board["columns"] if c["kind"] == "won"),
    )


def win(client, client_id, amount=None):
    """Заводит сделку и закрывает её выигрышем."""
    body = {"title": "Закрытая", "client_id": client_id}
    if amount is not None:
        body["amount"] = amount
    deal = client.post(DEALS, json=body).json()
    _open, won = stages(client)
    client.post(f"{DEALS}/{deal['id']}/move", json={"stage": won["key"]})
    return deal


def test_deals_without_a_price_do_not_drag_the_average_down(manager_client):
    """«Сумму ещё не назвали» — не то же самое, что «работа бесплатная».

    Возьми в знаменатель все выигранные сделки — и каждая без цены будет тихо
    занижать средний чек. Число при этом останется правдоподобным, и заметить
    подмену можно будет только пересчитав вручную.
    """
    person = make_client(manager_client, "Клиент среднего чека")
    before = manager_client.get(DASH).json()

    win(manager_client, person["id"], amount=100000)
    win(manager_client, person["id"], amount=200000)
    after_priced = manager_client.get(DASH).json()

    added_sum = after_priced["money_won_this_month"] - before["money_won_this_month"]
    added_count = after_priced["won_count_this_month"] - before["won_count_this_month"]
    assert added_sum == 300000
    assert added_count == 2

    # сделка без названной цены не должна менять ни знаменатель, ни средний чек
    win(manager_client, person["id"])
    after_unpriced = manager_client.get(DASH).json()

    assert after_unpriced["won_count_this_month"] == after_priced["won_count_this_month"]
    assert after_unpriced["avg_check"] == after_priced["avg_check"]


def test_average_check_without_a_single_priced_deal_is_not_zero(manager_client):
    """Ноль прочитают как «работаем даром». Верный ответ — «не о чем говорить».

    Окно берём заведомо пустое — в будущем, — чтобы не зависеть от сделок,
    которые оставили после себя другие тесты.
    """
    from datetime import timedelta

    from core.utils import now_utc
    from database.repositories import deals as deals_repo
    from database.session import SessionLocal

    with SessionLocal() as db:
        empty = deals_repo.money_summary(db, now_utc() + timedelta(days=365))

    assert empty["won_count_priced"] == 0, "окно должно быть пустым, иначе тест ни о чём"
    assert empty["won_since"] == 0
    assert empty["avg_check"] is None, "средний чек без сделок с ценой — не ноль"


def test_average_check_divides_by_priced_deals_only(manager_client):
    """Проверка самого деления, а не только знаменателя."""
    from core.utils import now_utc
    from database.repositories import deals as deals_repo
    from database.session import SessionLocal

    person = make_client(manager_client, "Клиент деления")
    # База одна на весь прогон, и выигранные сделки в ней уже есть: считаем
    # прирост, а не абсолютные числа.
    month_start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def summary():
        with SessionLocal() as db:
            return deals_repo.money_summary(db, month_start)

    before = summary()
    win(manager_client, person["id"], amount=100000)
    win(manager_client, person["id"], amount=300000)
    win(manager_client, person["id"])  # цену не назвали
    after = summary()

    assert after["won_since"] - before["won_since"] == 400000
    assert after["won_count_priced"] - before["won_count_priced"] == 2, (
        "сделка без цены попала в знаменатель"
    )
    # среднее делится именно на число сделок с ценой
    assert after["avg_check"] == round(after["won_since"] / after["won_count_priced"])


def test_empty_stage_stays_visible_in_the_funnel(manager_client):
    """«В согласовании ноль» — тоже ответ, и чаще всего именно он и нужен.

    Показывай только непустые этапы — и провал в середине воронки станет
    невидимым ровно тогда, когда на него надо смотреть.
    """
    all_stages = manager_client.get(f"{API}/pipeline/stages").json()["items"]
    funnel = manager_client.get(DASH).json()["deals_by_stage"]

    assert [s["key"] for s in funnel] == [s["key"] for s in all_stages]
    assert all("count" in s and "name" in s and "kind" in s for s in funnel)
    assert any(s["count"] == 0 for s in funnel), "пустых этапов не осталось — тест бесполезен"


def test_funnel_counts_follow_the_deals(manager_client):
    person = make_client(manager_client, "Клиент воронки")
    open_stage, _won = stages(manager_client)

    def count_of(key):
        funnel = manager_client.get(DASH).json()["deals_by_stage"]
        return next(s["count"] for s in funnel if s["key"] == key)

    before = count_of(open_stage["key"])
    manager_client.post(
        DEALS,
        json={"title": "Ещё одна", "client_id": person["id"], "stage": open_stage["key"]},
    )
    assert count_of(open_stage["key"]) == before + 1


def test_dashboard_shows_my_tasks_not_everyones(manager_client, root_client):
    """«С чего начать» — вопрос про свои задачи. Общий список фирмы на него
    не отвечает."""
    mine = manager_client.post(
        TASKS, json={"title": "Позвонить своему", "due_at": "2026-08-06T09:00:00Z"}
    ).json()
    stranger = root_client.post(
        TASKS, json={"title": "Чужая забота", "due_at": "2026-08-06T09:00:00Z"}
    ).json()

    titles = [t["id"] for t in manager_client.get(DASH).json()["my_tasks"]]
    assert mine["id"] in titles
    assert stranger["id"] not in titles, "на дашборде показались чужие задачи"


def test_undated_task_does_not_crowd_out_todays(manager_client):
    """На дашборде мало места, и занимать его бессрочным «когда-нибудь»
    нельзя: список «на сегодня» перестаёт быть списком на сегодня."""
    dated = manager_client.post(
        TASKS, json={"title": "Сегодня до обеда", "due_at": "2026-08-06T11:00:00Z"}
    ).json()
    undated = manager_client.post(TASKS, json={"title": "Когда-нибудь потом"}).json()

    shown = manager_client.get(DASH).json()["my_tasks"]
    ids = [task["id"] for task in shown]
    assert dated["id"] in ids, "задача со сроком не показалась — проверять нечего"
    assert undated["id"] not in ids
    assert all(task["due_at"] for task in shown)


def test_tasks_block_disappears_with_its_module(manager_client, root_client):
    """Выключенный блок пропадает целиком, а не оставляет пустой список."""
    root_client.post(f"{API}/modules/tasks", json={"enabled": False})
    try:
        assert manager_client.get(DASH).json()["my_tasks"] == []
    finally:
        root_client.post(f"{API}/modules/tasks", json={"enabled": True})
