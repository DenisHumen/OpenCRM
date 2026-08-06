"""Сделки: воронка, этапы, журнал перемещений.

Сделка — стержень, на который дальше вешаются деньги, письма и отчёты. Поэтому
проверяем не «создаётся ли», а то, от чего зависят все остальные разделы: что
журнал этапов заполняется всегда, что закрытие фиксируется, а возврат в работу
его снимает.
"""

from tests.conftest import API

DEALS = f"{API}/deals"


def make_client(manager_client, name="Клиент сделки"):
    return manager_client.post(f"{API}/clients", json={"name": name}).json()


def test_deal_needs_a_real_client(manager_client):
    """Сделка без клиента бессмысленна: некому выставлять счёт."""
    no_client = manager_client.post(DEALS, json={"title": "Ничья", "client_id": 999999})
    assert no_client.status_code == 404
    assert no_client.json()["error"]["code"] == "client_not_found"


def test_deal_is_created_on_the_first_stage(manager_client):
    client = make_client(manager_client)
    response = manager_client.post(DEALS, json={"title": "Лендинг", "client_id": client["id"]})
    assert response.status_code == 201, response.text
    deal = response.json()
    assert deal["stage"] == "new"   # первый открытый этап универсального набора
    assert deal["client_name"] == "Клиент сделки"   # имя приходит сразу, без второго запроса
    assert deal["closed_at"] is None


def test_owner_defaults_to_the_author_but_can_be_left_empty(manager_client):
    """«Поле не прислали» и «прислали пустым» — разные намерения.

    Без ответственного по умолчанию доска зарастает ничейными карточками. Но
    если пусто выбрали явно — значит нужна общая очередь, и подставлять автора
    втихую нельзя: интерфейс обещал одно, а сделал бы другое.
    """
    client = make_client(manager_client, "Ответственный")

    mine = manager_client.post(DEALS, json={"title": "По умолчанию", "client_id": client["id"]}).json()
    assert mine["manager_id"], "сделка осталась ничьей без явного на то указания"
    assert mine["manager_name"], "имя ответственного должно приходить сразу"

    shared = manager_client.post(
        DEALS, json={"title": "В общую очередь", "client_id": client["id"], "manager_id": None}
    ).json()
    assert shared["manager_id"] is None
    assert shared["manager_name"] is None


def test_creation_is_written_to_the_stage_journal(manager_client):
    """Первая запись — с пустым «откуда»: до создания этапа не было."""
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "Журнал", "client_id": client["id"]}).json()

    history = manager_client.get(f"{DEALS}/{deal['id']}").json()["stage_history"]
    assert len(history) == 1
    assert history[0]["from_stage"] == ""
    assert history[0]["to_stage"] == "new"
    assert history[0]["author_name"]


def test_moving_through_the_funnel_is_recorded(manager_client):
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "Путь", "client_id": client["id"]}).json()

    for stage in ("in_progress", "ready", "done"):
        moved = manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": stage})
        assert moved.status_code == 200, moved.text
        assert moved.json()["stage"] == stage

    history = manager_client.get(f"{DEALS}/{deal['id']}").json()["stage_history"]
    assert [h["to_stage"] for h in history] == ["new", "in_progress", "ready", "done"]


def test_stage_change_through_patch_is_recorded_too(manager_client):
    """Единственная точка смены этапа: иначе журнал дырявый там, где меняли иначе."""
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "Через patch", "client_id": client["id"]}).json()

    manager_client.patch(f"{DEALS}/{deal['id']}", json={"stage": "in_progress"})
    history = manager_client.get(f"{DEALS}/{deal['id']}").json()["stage_history"]
    assert [h["to_stage"] for h in history] == ["new", "in_progress"]


def test_reordering_inside_a_column_is_not_a_stage_change(manager_client):
    """Перетащили карточку выше в той же колонке — в журнал это писать нельзя,
    иначе отчёт «сколько стоит в этапе» засорится пустыми переходами."""
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "Порядок", "client_id": client["id"]}).json()

    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "new", "sort_order": 5})
    data = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert data["sort_order"] == 5
    assert len(data["stage_history"]) == 1


def test_closing_records_the_date_and_reopening_clears_it(manager_client):
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "Отказ", "client_id": client["id"]}).json()

    lost = manager_client.post(
        f"{DEALS}/{deal['id']}/move", json={"stage": "cancelled", "lost_reason": "дорого"}
    ).json()
    assert lost["closed_at"] is not None
    assert lost["lost_reason"] == "дорого"

    # вернули в работу — дата закрытия и причина больше не верны
    back = manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"}).json()
    assert back["closed_at"] is None
    assert back["lost_reason"] == ""


def test_unknown_stage_is_rejected(manager_client):
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "Чужой этап", "client_id": client["id"]}).json()
    bad = manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "выдуманный"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "unknown_stage"


def test_kanban_shows_exactly_the_configured_pipeline(manager_client):
    """Доска рисуется по воронке этого бизнеса, а не по зашитому списку.

    Пустые колонки тоже приходят: иначе доска «схлопывается» и перетащить сделку
    в пустой этап становится некуда.
    """
    stages = manager_client.get(f"{API}/pipeline/stages").json()["items"]
    board = manager_client.get(f"{DEALS}/board").json()
    assert [c["key"] for c in board["columns"]] == [s["key"] for s in stages]
    # Название приходит вместе с колонкой — фронт не может знать слова этого
    # бизнеса заранее и не должен их подставлять.
    assert all(c["name"] for c in board["columns"])
    assert {c["kind"] for c in board["columns"]} <= {"open", "won", "lost"}


def test_list_filters_by_stage_and_hides_closed_on_demand(manager_client):
    client = make_client(manager_client, "Фильтры")
    open_deal = manager_client.post(DEALS, json={"title": "Живая", "client_id": client["id"]}).json()
    closed = manager_client.post(DEALS, json={"title": "Закрытая", "client_id": client["id"]}).json()
    manager_client.post(f"{DEALS}/{closed['id']}/move", json={"stage": "done"})

    only_open = manager_client.get(DEALS, params={"client_id": client["id"], "include_closed": False}).json()
    ids = [d["id"] for d in only_open["items"]]
    assert open_deal["id"] in ids
    assert closed["id"] not in ids

    by_stage = manager_client.get(DEALS, params={"stage": "done", "client_id": client["id"]}).json()
    assert [d["id"] for d in by_stage["items"]] == [closed["id"]]


def test_search_finds_a_deal_by_client_name(manager_client):
    """В жизни спрашивают «что там по Ромашке», а не «как называлась сделка»."""
    client = make_client(manager_client, "Ромашка")
    manager_client.post(DEALS, json={"title": "Безымянная работа", "client_id": client["id"]})
    found = manager_client.get(DEALS, params={"search": "Ромашка"}).json()
    assert any(d["client_id"] == client["id"] for d in found["items"])


def test_deleted_deal_disappears_from_lists(manager_client):
    client = make_client(manager_client)
    deal = manager_client.post(DEALS, json={"title": "На удаление", "client_id": client["id"]}).json()

    assert manager_client.delete(f"{DEALS}/{deal['id']}").status_code == 200
    assert manager_client.get(f"{DEALS}/{deal['id']}").status_code == 404
    listing = manager_client.get(DEALS, params={"client_id": client["id"]}).json()
    assert deal["id"] not in [d["id"] for d in listing["items"]]


def test_every_single_deal_response_has_the_same_shape(manager_client):
    """Ответы по одной сделке обязаны быть одинаковыми.

    Карточка кладёт ответ на изменение прямо в состояние экрана. Пока PATCH и
    /move отвечали без `stage_history`, экран уходил в белое сразу после смены
    этапа: следующая отрисовка спотыкалась о undefined.
    """
    client = make_client(manager_client, "Форма ответа")
    created = manager_client.post(DEALS, json={"title": "Форма", "client_id": client["id"]}).json()

    fetched = manager_client.get(f"{DEALS}/{created['id']}").json()
    patched = manager_client.patch(f"{DEALS}/{created['id']}", json={"description": "правка"}).json()
    moved = manager_client.post(f"{DEALS}/{created['id']}/move", json={"stage": "in_progress"}).json()

    for name, payload in (("создание", created), ("чтение", fetched),
                          ("правка", patched), ("перемещение", moved)):
        assert "stage_history" in payload, f"{name}: ответ без истории этапов"
        assert isinstance(payload["stage_history"], list), name
        assert "manager_name" in payload, f"{name}: ответ без имени ответственного"
        assert "client_name" in payload, f"{name}: ответ без имени клиента"

    # история читаема человеком: голый ключ этапа ему ничего не говорит
    assert moved["stage_history"][-1]["to_name"], "в истории нет названия этапа"


def test_deals_require_login(base_client):
    assert base_client.get(DEALS).status_code == 401
    assert base_client.get(f"{DEALS}/board").status_code == 401


def test_two_people_moving_one_deal_do_not_tear_the_journal(manager_client):
    """Второй, кто двигает ту же заявку, получает отказ, а не молчаливый перехват.

    Гонка, ради которой это написано: двое открыли доску, оба видят заявку в
    «Новой», оба тянут её — один в «В работе», другой в «Готово». Раньше обоим
    отвечали «готово», и в журнале оставались две записи из одного этапа:

        ('', 'new') → ('new', 'in_progress') → ('new', 'done')

    Перехода `in_progress → done` не было ни разу. Данные при этом целы — тем
    неприятнее: заметить нечего, пока не сверишь журнал глазами, а отчёт
    «сколько заявка простояла в этапе» уже считает по разорванной цепочке.

    Второго участника изображаем отдельной сессией: она меняет этап в базе
    ровно между чтением и записью первого. Настоящую параллельность здесь
    воспроизвести нечем — `TestClient` работает в одном процессе и запросы
    выстраивает в очередь, — а порядок событий тот же самый.
    """
    from sqlalchemy import update

    from core import exceptions as errors
    from core.services import deal_service
    from database.models import Deal, User
    from database.session import SessionLocal

    client = make_client(manager_client, "Клиент гонки")
    deal = manager_client.post(DEALS, json={"title": "Заявка", "client_id": client["id"]}).json()

    with SessionLocal() as first:
        author = first.query(User).first()
        # Первый прочитал заявку и держит её в руках — в «Новой».
        held = deal_service.get_deal(first, deal["id"])
        assert held.stage == "new"

        # Второй тем временем успел передвинуть её и зафиксировать.
        with SessionLocal() as second:
            second.execute(update(Deal).where(Deal.id == deal["id"]).values(stage="done"))
            second.commit()

        try:
            deal_service.move_stage(first, deal["id"], "in_progress", author)
            raise AssertionError("перехват прошёл молча — журнал снова рвётся")
        except errors.ConflictError as refused:
            assert refused.code == "stage_moved_meanwhile"
        first.rollback()

    # В журнале — только состоявшиеся переходы, цепочка не разорвана.
    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    chain = [(row["from_stage"], row["to_stage"]) for row in card["stage_history"]]
    assert ("new", "in_progress") not in chain, "запись о непроизошедшем переходе всё же появилась"
    assert card["stage"] == "done"


def test_a_deal_cannot_be_handed_to_someone_who_does_not_exist(manager_client):
    """Несуществующий ответственный — отказ с объяснением, а не 500.

    Клиента заявка проверяет, а ответственного проверять забывала: число
    доезжало до вставки и падало нарушением внешнего ключа. Для человека это
    пятисотка на обычную опечатку в запросе.
    """
    client = make_client(manager_client, "Клиент без ответственного")

    born = manager_client.post(
        DEALS, json={"title": "Ничья", "client_id": client["id"], "manager_id": 999_999}
    )
    assert born.status_code == 404, born.text
    assert born.json()["error"]["code"] == "manager_not_found"

    deal = manager_client.post(DEALS, json={"title": "Живая", "client_id": client["id"]}).json()
    moved = manager_client.patch(f"{DEALS}/{deal['id']}", json={"manager_id": 999_999})
    assert moved.status_code == 404, moved.text
    assert moved.json()["error"]["code"] == "manager_not_found"

    # Пустой ответственный по-прежнему законен: общая очередь, разберут потом.
    freed = manager_client.patch(f"{DEALS}/{deal['id']}", json={"manager_id": None})
    assert freed.status_code == 200, freed.text
    assert freed.json()["manager_id"] is None


def test_a_deadline_with_a_zone_keeps_its_moment(manager_client):
    """«18:00 по Киеву» — это 15:00 UTC, а не 18:00 UTC.

    Смещение молча отбрасывалось: в базу ложилось присланное время как есть, и
    срок наступал на величину смещения позже, чем человек назначил. В ответе
    при этом стояло присланное значение — то есть API отвечал не тем, что
    записал, и проверить расхождение было нечем.
    """
    from database.models import Deal
    from database.session import SessionLocal

    client = make_client(manager_client, "Клиент со сроком")
    deal = manager_client.post(
        DEALS,
        json={
            "title": "Срочная",
            "client_id": client["id"],
            "due_at": "2026-09-01T18:00:00+03:00",
        },
    ).json()

    assert deal["due_at"] == "2026-09-01T15:00:00", deal["due_at"]

    with SessionLocal() as db:
        stored = db.get(Deal, deal["id"])
        assert stored.due_at.tzinfo is None, "в базе оказалось время с зоной"
        assert stored.due_at.hour == 15, "смещение зоны потеряно при записи"

    # То же и при правке, и то же с зоной западнее UTC.
    edited = manager_client.patch(
        f"{DEALS}/{deal['id']}", json={"due_at": "2026-09-01T10:00:00-05:00"}
    ).json()
    assert edited["due_at"] == "2026-09-01T15:00:00", edited["due_at"]
