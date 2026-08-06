"""Механизм связи блоков: события вместо прямых вызовов.

Проверяем не «вызвался ли обработчик», а свойства, ради которых механизм и
заводился. Каждое из них при нарушении портит систему тихо:

- наблюдатель, роняющий основную операцию, — это мелочь в ленте, отменяющая
  работу целиком;
- участник, НЕ роняющий её, — это остаток, разошедшийся со складом молча;
- подписчик выключенного блока — это выключение, которое ничего не выключило;
- порядок, зависящий от импортов, — это поведение, плавающее между запусками.
"""

import pytest

from core import events
from core import exceptions as errors
from core.services import client_service, modules_service
from core.services.deal_service import DEAL_STAGE_CHANGED
from database.models.client import KIND_STAGE
from tests.conftest import API
from tests.test_deals import DEALS, make_client

MODULES = f"{API}/modules"


@pytest.fixture
def subscribe():
    """Подписки на время теста.

    Снимаем в фикстуре, а не в конце теста: тело до конца может и не дойти, а
    забытый подписчик посыплет совершенно посторонние файлы — реестр подписок
    глобальный, как и реестр блоков.
    """
    added = []

    def add(event, handler, *, is_participant=False, module=None, order=events.DEFAULT_ORDER):
        sub = events.subscribe(
            event, handler, is_participant=is_participant, module=module, order=order
        )
        added.append(sub)
        return sub

    yield add
    for sub in added:
        events.unsubscribe(sub)


@pytest.fixture
def silence():
    """Снять всех подписчиков события на время теста и вернуть их обратно."""
    removed = []

    def hush(event_name):
        listening = events.subscribers_of(event_name)
        for sub in listening:
            events.unsubscribe(sub)
        removed.extend(listening)
        return listening

    yield hush
    for sub in removed:
        events.subscribe(
            sub.event,
            sub.handler,
            is_participant=sub.is_participant,
            module=sub.module,
            order=sub.order,
        )


@pytest.fixture
def deal(manager_client):
    client = make_client(manager_client, "Клиент событий")
    return manager_client.post(
        DEALS, json={"title": "Заявка событий", "client_id": client["id"]}
    ).json()


def move(client, deal, stage="in_progress"):
    return client.post(f"{DEALS}/{deal['id']}/move", json={"stage": stage})


def feed(client, deal, kind=None):
    query = f"?kind={kind}" if kind else ""
    return client.get(f"{DEALS}/{deal['id']}/feed{query}").json()["items"]


# --- то, ради чего механизм заведён ---


def test_stage_change_reaches_the_feed(manager_client, deal):
    """Живая связь, а не мёртвый код: сделка сменила этап — это видно в ленте.

    Журнал этапов существовал и раньше, но отдельной панелью: менеджер, читающий
    ленту, видел звонки и письма и не видел, что заявка доехала до следующего
    этапа.
    """
    assert move(manager_client, deal).status_code == 200

    entries = feed(manager_client, deal, kind=KIND_STAGE)
    assert len(entries) == 1, "смена этапа не дошла до ленты"
    assert "В работе" in entries[0]["body"]


def test_the_deal_does_not_know_who_listens(manager_client, deal, subscribe):
    """Объявляющий не знает подписчиков: новый подписчик не требует правки сделок."""
    heard = []
    subscribe(DEAL_STAGE_CHANGED, lambda event: heard.append(event["to_stage"]))

    move(manager_client, deal)
    assert heard == ["in_progress"]


def test_event_without_subscribers_is_normal(manager_client, deal, silence):
    """Событие, которое никто не слушает, — не ошибка, а обычное положение дел.

    Проверяем на настоящем пути: снимаем всех подписчиков и переводим заявку.
    Выдуманное имя события проверило бы только то, что цикл по пустому списку не
    падает, — а важно, что заявка при этом переводится как ни в чём не бывало.
    """
    assert silence(DEAL_STAGE_CHANGED), "тест потерял смысл: слушать было и так некому"

    assert move(manager_client, deal).status_code == 200
    assert manager_client.get(f"{DEALS}/{deal['id']}").json()["stage"] == "in_progress"
    assert feed(manager_client, deal, kind=KIND_STAGE) == []


# --- два вида подписчиков ---


def test_observer_failure_does_not_undo_the_operation(manager_client, deal, subscribe, caplog):
    """Наблюдатель упал — основная операция состоялась, ошибка видна в журнале.

    И его собственные записи откатились: наблюдатель, успевший записать половину
    и упавший, оставил бы в ленте огрызок, которому никто не поверит.
    """
    def half_written_then_broken(event):
        client_service.add_system_note(
            event.db, event["deal"].client_id, event.actor, KIND_STAGE,
            "Половина работы наблюдателя", deal_id=event["deal"].id,
        )
        raise RuntimeError("наблюдателю поплохело")

    subscribe(DEAL_STAGE_CHANGED, half_written_then_broken)

    with caplog.at_level("ERROR", logger="opencrm.events"):
        response = move(manager_client, deal)

    assert response.status_code == 200
    assert response.json()["stage"] == "in_progress", "мелочь в ленте отменила работу"

    bodies = [entry["body"] for entry in feed(manager_client, deal, kind=KIND_STAGE)]
    assert "Половина работы наблюдателя" not in bodies, "огрызок упавшего наблюдателя остался"
    assert any("В работе" in body for body in bodies), "соседний подписчик не отработал"
    assert "half_written_then_broken" in caplog.text, "падение осталось без следа в журнале"


def test_participant_failure_undoes_everything(manager_client, deal, subscribe):
    """Участник упал — не состоялось ничего, включая то, ради чего всё начиналось.

    Так подписан будет склад: не списалось — акт не провёлся. Отказ доменной
    ошибкой, а не падением: менеджеру нужно объяснение, а не «500».
    """
    def refuses(event):
        raise errors.ValidationError("Нечего списывать", code="nothing_to_write_off")

    subscribe(DEAL_STAGE_CHANGED, refuses, is_participant=True)

    response = move(manager_client, deal)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "nothing_to_write_off"

    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert card["stage"] == "new", "этап сменился вопреки отказу участника"
    assert len(card["stage_history"]) == 1, "в журнал попал переход, которого не было"
    assert feed(manager_client, deal, kind=KIND_STAGE) == [], "лента запомнила отменённое"


def test_participants_run_before_observers(manager_client, deal, subscribe):
    """Наблюдатель записывает то, что состоялось, — значит после всех участников.

    Иначе в ленте появлялись бы записи об операции, которую следующий участник
    отменил: строка исчезнет вместе с откатом, но только если сессия доживёт до
    отката, а рассчитывать на это нельзя.
    """
    order = []
    subscribe(DEAL_STAGE_CHANGED, lambda e: order.append("наблюдатель"))
    subscribe(DEAL_STAGE_CHANGED, lambda e: order.append("участник"), is_participant=True)

    move(manager_client, deal)
    assert order == ["участник", "наблюдатель"]


# --- выключенный блок просто не подписан ---


@pytest.fixture
def restore_modules(root_client):
    yield
    modules_service.invalidate()
    root_client.post(f"{MODULES}/documents", json={"enabled": True})


def test_subscriber_of_a_switched_off_module_is_skipped(
    root_client, manager_client, deal, subscribe, restore_modules
):
    """Выключенный блок не слушает, а остальные — слушают, и никто не падает.

    Это и есть замена россыпи `if moduleOn(...)` по коду: проверка одна, стоит в
    диспетчере, и забыть её в одиннадцатом месте невозможно — одиннадцатого
    места нет.
    """
    heard = []
    subscribe(DEAL_STAGE_CHANGED, lambda e: heard.append("бланки"), module="documents")
    subscribe(DEAL_STAGE_CHANGED, lambda e: heard.append("общий"))

    assert root_client.post(f"{MODULES}/documents", json={"enabled": False}).status_code == 200

    assert move(manager_client, deal).status_code == 200
    assert heard == ["общий"], "подписчик выключенного блока всё-таки сработал"

    # включили обратно — подписка на месте, восстанавливать ничего не пришлось
    heard.clear()
    root_client.post(f"{MODULES}/documents", json={"enabled": True})
    assert move(manager_client, deal, "done").status_code == 200
    assert heard == ["общий", "бланки"]


# --- порядок ---


def test_order_does_not_depend_on_the_order_of_registration(manager_client, deal, subscribe):
    """Порядок задан явно, а не тем, кто раньше импортировался.

    Оставь его за импортами — и одно и то же действие даст разный результат на
    рабочем сервере и в тестах, а воспроизвести падение станет нечем.
    """
    order = []

    def late(event):
        order.append("поздний")

    def early(event):
        order.append("ранний")

    # регистрируем задом наперёд
    subscribe(DEAL_STAGE_CHANGED, late, order=200)
    subscribe(DEAL_STAGE_CHANGED, early, order=10)

    move(manager_client, deal)
    assert order == ["ранний", "поздний"]


def test_equal_order_is_still_decided(subscribe):
    """При равном `order` очередь всё равно определена: имя блока, потом функции.

    «Как получится» здесь недопустимо ровно потому, что расхождение проявится не
    сразу и не у всех.
    """
    def zebra(event):
        pass

    def alpha(event):
        pass

    subscribe("test.tie", zebra)
    subscribe("test.tie", alpha)

    names = [sub.handler.__name__ for sub in events.subscribers_of("test.tie")]
    assert names == ["alpha", "zebra"]


# --- исполнитель ---


def test_actor_reaches_every_record_born_of_one_action(manager_client, deal):
    """Одно действие — две записи, и обе принадлежат одному живому человеку.

    Подписчик ставит того же исполнителя, а не «систему»: иначе половина истории
    заявки оказывается ничьей, и на вопрос «кто это сделал» отвечать нечем.
    """
    me = manager_client.get(f"{API}/auth/me").json()
    move(manager_client, deal)

    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert card["stage_history"][-1]["author_name"] == me["name"]

    entry = feed(manager_client, deal, kind=KIND_STAGE)[0]
    assert entry["author_id"] == me["id"], "запись из подписчика осталась ничьей"


def test_reason_travels_with_the_event(manager_client, deal):
    """Причина — часть контракта события, а не забота подписчика.

    Карточку правят осознанно, карточку на доске перетаскивают мимоходом — в
    ленте это разные строки, и различает их не подписчик, а тот, кто событие
    поднял.
    """
    move(manager_client, deal)
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"stage": "done"})

    bodies = [entry["body"] for entry in feed(manager_client, deal, kind=KIND_STAGE)]
    assert any("moved on the board" in body for body in bodies)
    assert any("edited on the deal card" in body for body in bodies)


def test_system_entries_cannot_be_faked_by_hand(manager_client, deal):
    """Вид записи, который ставит код, из формы не прислать.

    Иначе лента перестала бы быть свидетельством: «этап сменили» мог бы набрать
    руками тот, кто его не менял.
    """
    response = manager_client.post(
        f"{DEALS}/{deal['id']}/feed", json={"kind": KIND_STAGE, "body": "Как будто перевели"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_note_kind"
