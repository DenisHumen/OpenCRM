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


def stage_name(client, key: str) -> str:
    """Как этап называется В ЭТОЙ базе.

    Не константой: название этапа — засеянные данные, а они переводятся и
    переименовываются. Проверка «смена этапа видна в ленте» про название знать
    не должна вовсе — ей важно, что в ленту попало имя ТОГО этапа, куда сделка
    уехала. Спросить его у воронки честнее, чем повторить своей копией.
    """
    items = client.get(f"{API}/pipeline/stages").json()["items"]
    return next(s["name"] for s in items if s["key"] == key)


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
    assert stage_name(manager_client, "in_progress") in entries[0]["body"]


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
    v_rabote = stage_name(manager_client, "in_progress")
    assert any(v_rabote in body for body in bodies), "соседний подписчик не отработал"
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


def test_an_observer_does_not_swallow_a_nested_participants_refusal(
    manager_client, deal, subscribe
):
    """Отказ участника обязан отменять операцию и со второго уровня тоже.

    Наблюдатель вправе поднять внутри себя второе событие — так и делают, когда
    записанное само по себе является поводом для чего-то ещё. Но у второго
    события свои участники, а участник существует ровно затем, чтобы сказать
    «нет»: не списалось — акт не провёлся.

    `_run_observer` ловил всё подряд, и это «нет» проглатывалось вместе с
    падениями самого наблюдателя: операция состоялась вопреки прямому отказу и
    не оставила ни строки о том, что кто-то возражал. Обещание «участник может
    отменить операцию» держалось только на первом уровне.
    """
    nested = "test.raised_from_an_observer"

    def refuses(event):
        raise errors.ValidationError("Нечего списывать", code="nothing_to_write_off")

    def raises_a_second_event(event):
        events.emit(nested, db=event.db, actor=event.actor, reason=event.reason)

    subscribe(nested, refuses, is_participant=True)
    subscribe(DEAL_STAGE_CHANGED, raises_a_second_event)

    response = move(manager_client, deal)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "nothing_to_write_off"

    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert card["stage"] == "new", "этап сменился вопреки отказу участника второго уровня"
    assert feed(manager_client, deal, kind=KIND_STAGE) == [], "лента запомнила отменённое"


def test_an_observers_own_failure_is_still_swallowed(manager_client, deal, subscribe, caplog):
    """Обратная половина: своё падение наблюдатель по-прежнему проглатывает.

    Без этой пары починку выше легко «доделать» до того, что любая ошибка в
    ленте начнёт отменять работу, — то есть ровно до беды, от которой точка
    отката и заводилась.
    """
    def emits_then_breaks(event):
        events.emit(
            "test.nobody_listens", db=event.db, actor=event.actor, reason=event.reason
        )
        raise RuntimeError("наблюдателю поплохело после второго события")

    subscribe(DEAL_STAGE_CHANGED, emits_then_breaks)

    with caplog.at_level("ERROR", logger="opencrm.events"):
        response = move(manager_client, deal)

    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "in_progress"
    assert "emits_then_breaks" in caplog.text, "падение осталось без следа в журнале"


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


def test_a_typo_in_the_module_key_is_refused_at_subscription(subscribe):
    """Опечатка в ключе блока падает при загрузке, а не молчит годами.

    `emit` спрашивает `modules_service.is_enabled(db, sub.module)`, и незнакомый
    ключ для него — «блок выключен». Отличить `warehous` от честно выключенного
    склада нечем: подписчик просто никогда не зовётся, ошибки при этом нет
    никакой. Списание по акту тихо перестало бы происходить, а искать причину
    пришлось бы по остаткам склада — то есть неделю спустя и не в том месте.

    Проверять надо в момент подписки: подписки загружаются один раз при первом
    событии, значит и отказ будет один и громкий.
    """
    def handler(event):
        pass

    with pytest.raises(ValueError, match="Unknown module"):
        subscribe("test.typo", handler, module="warehous")

    # И тем входом, которым пользуются в рабочем коде, — декоратором.
    with pytest.raises(ValueError, match="Unknown module"):
        events.observer("test.typo", module="warehous")(handler)
    with pytest.raises(ValueError, match="Unknown module"):
        events.participant("test.typo", module="warehous")(handler)

    # Опечатка не оставила после себя половину подписки.
    assert events.subscribers_of("test.typo") == ()

    # А настоящий ключ проходит — проверка ловит опечатку, а не запрещает блоки.
    subscribe("test.typo", handler, module="warehouse")
    assert len(events.subscribers_of("test.typo")) == 1


def test_every_subscription_in_the_wild_names_a_module_that_exists():
    """Сводная: ни одна подписка рабочего кода не указывает в пустоту.

    Проверка выше сторожит вход, эта — то, что сегодня в реестре подписок.
    Расходятся они в тот день, когда блок переименуют, а подписку забудут.
    """
    from core import modules

    events._load_subscriptions()
    unknown = sorted(
        {sub.module for sub in events._subscribers if sub.module and not modules.get(sub.module)}
    )
    assert unknown == [], f"подписка на несуществующий блок: {unknown}"


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


def test_system_entry_cannot_be_deleted_by_anyone(manager_client, root_client):
    """След собственного действия не должен стираться тем, кто его оставил.

    Автором записи о смене этапа стоит тот, кто двигал заявку, — и общее
    правило «автор может удалить своё» отдало бы ему право убрать отметку о
    том, что он сделал. Рукописную заметку удаляют, потому что ошиблись при
    вводе; смена этапа либо была, либо нет.
    """
    from tests.conftest import API
    from tests.test_deals import DEALS, make_client

    client = make_client(manager_client, "Клиент неудаляемой записи")
    deal = manager_client.post(DEALS, json={"title": "Поедет по этапам", "client_id": client["id"]}).json()
    board = manager_client.get(f"{DEALS}/board").json()
    won = next(c for c in board["columns"] if c["kind"] == "won")["key"]
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": won})

    notes = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    system = [n for n in notes if n["kind"] == "stage"]
    assert system, "смена этапа не попала в ленту — проверять нечего"

    note_id = system[0]["id"]
    # автор записи — тот, кто двигал заявку
    denied = manager_client.delete(f"{API}/clients/{client['id']}/notes/{note_id}")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "system_note_immutable"

    # и root тоже: журнал, который можно поправить, ничего не доказывает
    assert root_client.delete(f"{API}/clients/{client['id']}/notes/{note_id}").status_code == 403

    still = manager_client.get(f"{API}/clients/{client['id']}/notes").json()["items"]
    assert any(n["id"] == note_id for n in still), "запись всё-таки удалилась"


def test_the_two_decorators_subscribe_the_two_different_ways(manager_client, deal):
    """`@participant` и `@observer` — не синонимы, и разница проверяется здесь.

    Подписки в рабочем коде расставлены декораторами, а тесты до сих пор звали
    `subscribe(...)` напрямую — то есть проверяли механизм, но не тот вход,
    которым им пользуются. Разойдись декоратор с механизмом (перепутан флаг,
    потерян порядок), ни один тест этого бы не заметил, а склад молча перестал
    бы отменять акт, на который нечего списать.

    Снимаем подписки руками: фикстура `subscribe` умеет только прямой вызов, а
    декоратор подписывает сам, в момент объявления.
    """
    order = []

    @events.observer(DEAL_STAGE_CHANGED)
    def watches(event):
        order.append("наблюдатель")

    @events.participant(DEAL_STAGE_CHANGED)
    def takes_part(event):
        order.append("участник")

    try:
        assert move(manager_client, deal).status_code == 200
        # Участник идёт первым — как и при прямой подписке.
        assert order == ["участник", "наблюдатель"]

        # И право отменить у него настоящее: наблюдатель того же события
        # отменить ничего не может — это и есть разница между двумя словами.
        order.clear()

        @events.participant(DEAL_STAGE_CHANGED)
        def refuses(event):
            raise errors.ValidationError("Нечего списывать", code="nothing_to_write_off")

        try:
            second = manager_client.post(
                f"{DEALS}/{deal['id']}/move", json={"stage": "ready"}
            )
            assert second.status_code == 422, second.text
            assert second.json()["error"]["code"] == "nothing_to_write_off"
        finally:
            for sub in list(events._subscribers):
                if sub.handler is refuses:
                    events.unsubscribe(sub)
    finally:
        for sub in list(events._subscribers):
            if sub.handler in (watches, takes_part):
                events.unsubscribe(sub)


def test_an_unknown_source_is_refused_at_the_door(manager_client, deal):
    """Незнакомый источник — отказ сразу, а не потерянная запись потом.

    Неизвестное значение доходило до `assert_actor` внутри наблюдателя, там
    превращалось в ошибку, наблюдатель падал под точкой отката — запись в ленте
    исчезала молча, а основная операция состоялась.
    """
    from database.session import SessionLocal

    with SessionLocal() as db:
        with pytest.raises(ValueError, match="Unknown event source"):
            events.emit(
                DEAL_STAGE_CHANGED,
                db=db,
                actor=None,
                reason="проба",
                source="cron",
                deal=None,
                from_stage="new",
                to_stage="ready",
            )


def test_every_subscription_passes_the_source_down():
    """Источник протаскивается вниз без изменений — во всех подписках.

    Обещание записано в докстроке `Event`: «все трое протаскиваются вниз без
    изменений». Держалось оно только у смены этапа; бланк и списание источник
    теряли, и у безликого источника (вебхук АТС, забор почты) запись в ленте
    исчезала молча — исполнителя там нет по определению, `assert_actor`
    отказывал, наблюдатель падал под точкой отката.

    Проверка по исходнику: подписок мало, они рядом, а поймать потерю поведением
    можно только подняв событие от вебхука — то есть написав ту самую связку,
    которой сегодня ещё нет.
    """
    import pathlib
    import re

    source = pathlib.Path("core/subscriptions.py").read_text(encoding="utf-8")
    calls = re.findall(r"add_system_note\((.*?)^    \)", source, flags=re.S | re.M)
    assert len(calls) >= 3, f"подписок найдено {len(calls)} — проверка смотрит не туда"
    for call in calls:
        assert "source=event.source" in call, "подписка пишет в ленту, не передав источник"


def test_lenta_zayavki_listaetsya_do_kontsa(manager_client, deal):
    """Лента заявки, которую ведут год, не обрывается на своей странице.

    Прежде ручка брала двести событий, `total` выбрасывала и отвечала одним
    куском. У долгой заявки ранние разговоры просто переставали существовать —
    без счётчика, без строки «показано столько-то», без единого признака.

    Проверяется обходом: пройдя страницами, доходим ровно до всех записей и ни
    одной не встречаем дважды. На это опирается экран — он собирает ленту
    страницами, пока показанных меньше, чем сказано в `total`.
    """
    skolko = 7
    for nomer in range(skolko):
        otvet = manager_client.post(
            f"{DEALS}/{deal['id']}/feed",
            json={"kind": "note", "body": f"событие {nomer}"},
        )
        assert otvet.status_code == 201, otvet.text

    sobrano = []
    vsego = None
    stranitsa = 1
    while True:
        otvet = manager_client.get(
            f"{DEALS}/{deal['id']}/feed?page={stranitsa}&per_page=3"
        ).json()
        assert otvet["page"] == stranitsa, f"отдана не та страница: {otvet['page']}"
        if vsego is None:
            vsego = otvet["total"]
        else:
            assert otvet["total"] == vsego, "«всего» пляшет между страницами"
        sobrano += [z["id"] for z in otvet["items"]]
        if not otvet["items"] or len(sobrano) >= vsego:
            break
        stranitsa += 1
        assert stranitsa < 50, "листание не заканчивается"

    assert vsego >= skolko, f"в ленте {vsego} записей, а заведено было {skolko}"
    assert len(sobrano) == len(set(sobrano)), "страницы пересекаются — запись встретилась дважды"
    assert len(sobrano) == vsego, f"пройдено {len(sobrano)} из {vsego} — в листании дыра"
