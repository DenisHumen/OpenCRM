"""Напоминания: срок, исполнитель, счётчики.

CRM, которая не напоминает перезвонить, — записная книжка.

Главное здесь не «сохраняется ли строка», а часовой пояс: «сегодня до 18:00»
означает местное время, а в базе всё лежит в UTC. Перепутай их — и напоминание
сработает не тогда, причём тихо.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import API
from tests.test_deals import DEALS, make_client

TASKS = f"{API}/tasks"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def test_deadline_with_an_offset_is_stored_as_utc(manager_client):
    """18:00 в Киеве и 18:00 в Варшаве — разные мгновения.

    Клиент присылает абсолютный момент со смещением; сервер обязан привести его
    к UTC, иначе сравнение «просрочено ли» врёт на величину смещения.
    """
    kyiv = timezone(timedelta(hours=3))
    local = datetime(2026, 8, 10, 18, 0, tzinfo=kyiv)   # 18:00 по Киеву
    task = manager_client.post(TASKS, json={"title": "Позвонить", "due_at": iso(local)}).json()

    stored = datetime.fromisoformat(task["due_at"])
    # 18:00+03:00 — это 15:00 UTC. Ни минутой позже.
    assert stored.hour == 15 and stored.minute == 0, task["due_at"]


def test_a_deadline_without_an_offset_is_taken_as_utc(manager_client):
    """Гадать о зоне отправителя хуже, чем принять соглашение и держать его."""
    task = manager_client.post(
        # Дата от «сейчас», а не зашитая: проверяется РАЗБОР строки без смещения,
        # и день тут безразличен. А вот оставленная в прошлом задача становится
        # просроченной и меняет порядок в общем для всех тестов списке — этим
        # уже уронило соседний файл, см. `skoro` в test_dashboard_deals.py.
        TASKS,
        json={
            "title": "Без зоны",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
                "%Y-%m-%dT09:30:00"
            ),
        },
    ).json()
    stored = datetime.fromisoformat(task["due_at"])
    assert (stored.hour, stored.minute) == (9, 30)


def test_task_without_an_assignee_goes_to_its_author(manager_client):
    """«Ничья» задача не делается никем: каждый думает, что возьмёт другой."""
    task = manager_client.post(TASKS, json={"title": "Кто-нибудь сделает"}).json()
    assert task["assignee_id"] is not None
    assert task["assignee_name"]


def test_a_task_needs_neither_client_nor_deal(manager_client):
    """«Отвезти документы в банк» — тоже задача."""
    task = manager_client.post(TASKS, json={"title": "Отвезти документы"})
    assert task.status_code == 201, task.text
    assert task.json()["client_id"] is None
    assert task.json()["deal_id"] is None


def test_overdue_and_today_are_separate_lists(manager_client):
    now = datetime.now(timezone.utc)
    late = manager_client.post(
        TASKS, json={"title": "Просрочено", "due_at": iso(now - timedelta(hours=2))}
    ).json()
    soon = manager_client.post(
        TASKS, json={"title": "Сегодня", "due_at": iso(now + timedelta(hours=3))}
    ).json()
    later = manager_client.post(
        TASKS, json={"title": "Через месяц", "due_at": iso(now + timedelta(days=30))}
    ).json()

    overdue = [t["id"] for t in manager_client.get(f"{TASKS}?scope=overdue").json()["items"]]
    today = [t["id"] for t in manager_client.get(f"{TASKS}?scope=today").json()["items"]]

    assert late["id"] in overdue
    assert soon["id"] not in overdue, "будущий срок попал в просроченные"
    assert soon["id"] in today
    assert later["id"] not in today


def test_done_tasks_leave_the_open_lists(manager_client):
    now = datetime.now(timezone.utc)
    task = manager_client.post(
        TASKS, json={"title": "Сделаю и закрою", "due_at": iso(now - timedelta(hours=1))}
    ).json()
    assert task["id"] in [t["id"] for t in manager_client.get(f"{TASKS}?scope=overdue").json()["items"]]

    done = manager_client.patch(f"{TASKS}/{task['id']}", json={"is_done": True}).json()
    assert done["is_done"] is True
    assert done["done_at"], "не записано, когда закрыли"

    assert task["id"] not in [t["id"] for t in manager_client.get(f"{TASKS}?scope=overdue").json()["items"]]
    assert task["id"] in [t["id"] for t in manager_client.get(f"{TASKS}?scope=done").json()["items"]]

    # и обратно: отметили по ошибке
    back = manager_client.patch(f"{TASKS}/{task['id']}", json={"is_done": False}).json()
    assert back["is_done"] is False and back["done_at"] is None


def test_undated_tasks_do_not_push_overdue_ones_down(manager_client):
    """Задача без срока не должна оттеснять просроченную наверх списка.

    Сравниваются позиции СВОИХ двух задач, а не место в общем списке. База у
    тестов одна на всю сессию, и «моя запись первая» — это утверждение не про
    сортировку, а про то, что соседи не успели ничего завести. Такая проверка
    держится ровно до первого нового теста с просроченной задачей и падает
    потом в чужом файле, где искать её никто не станет.
    """
    now = datetime.now(timezone.utc)
    bez_sroka = manager_client.post(TASKS, json={"title": "Когда-нибудь"}).json()
    dated = manager_client.post(
        TASKS, json={"title": "Срочное", "due_at": iso(now - timedelta(days=1))}
    ).json()

    poryadok = [t["id"] for t in manager_client.get(f"{TASKS}?scope=open").json()["items"]]
    assert dated["id"] in poryadok and bez_sroka["id"] in poryadok, "задачи не попали в список"
    assert poryadok.index(dated["id"]) < poryadok.index(bez_sroka["id"]), (
        "бессрочная задача оказалась выше просроченной"
    )


def test_summary_counts_what_the_navigation_shows(manager_client):
    """Без счётчика в задачи заходят «на всякий случай»."""
    before = manager_client.get(f"{TASKS}/summary").json()
    now = datetime.now(timezone.utc)
    manager_client.post(TASKS, json={"title": "Ещё просрочка", "due_at": iso(now - timedelta(minutes=5))})

    after = manager_client.get(f"{TASKS}/summary").json()
    assert after["overdue"] == before["overdue"] + 1
    assert after["open"] == before["open"] + 1


def test_summary_counts_past_the_length_of_the_list(manager_client):
    """Счётчик считает все напоминания, а не первую страницу.

    Список отдаётся с потолком в 200 строк, и счётчик легко сделать его
    заложником — тогда на большой базе в меню навсегда застынет «200». Проверка
    заводит больше задач, чем помещается в список, и сверяет число с тем, что
    насчитал сервер до и после.
    """
    from core.services.task_service import LIST_LIMIT

    before = manager_client.get(f"{TASKS}/summary").json()["open"]
    added = LIST_LIMIT + 5
    for i in range(added):
        assert manager_client.post(TASKS, json={"title": f"Напоминание {i}"}).status_code == 201

    listed = manager_client.get(f"{TASKS}?scope=open").json()["items"]
    counted = manager_client.get(f"{TASKS}/summary").json()["open"]

    assert len(listed) == LIST_LIMIT, "список отдал больше своего потолка"
    assert counted == before + added, "счётчик остановился на длине списка"


def test_task_is_linked_to_a_deal_and_found_by_it(manager_client):
    client = make_client(manager_client, "Клиент задачи")
    deal = manager_client.post(DEALS, json={"title": "Заказ", "client_id": client["id"]}).json()
    task = manager_client.post(
        TASKS, json={"title": "Заказать деталь", "deal_id": deal["id"]}
    ).json()

    found = manager_client.get(f"{TASKS}?deal_id={deal['id']}").json()["items"]
    assert [t["id"] for t in found] == [task["id"]]


def test_tasks_disappear_with_their_module(root_client, manager_client):
    root_client.post(f"{API}/modules/tasks", json={"enabled": False})
    try:
        closed = manager_client.get(TASKS)
        assert closed.status_code == 403
        assert closed.json()["error"]["code"] == "module_disabled"
        # соседние разделы не задеты
        assert manager_client.get(f"{API}/clients").status_code == 200
    finally:
        root_client.post(f"{API}/modules/tasks", json={"enabled": True})
