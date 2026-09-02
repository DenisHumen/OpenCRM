"""Журнал действий: проверяем свойства, без которых он бесполезен.

Журнал, который нельзя прочитать как доказательство, хуже отсутствующего: на
него ссылаются, а он врёт. Поэтому здесь почти нет тестов «запись появилась» —
есть тесты на то, из-за чего запись перестаёт что-либо доказывать:

- исполнитель, потерявшийся по дороге между блоками («кто списал» — «система»);
- источник, стёршийся до «руки» («Иванов списал» вместо «списалось по акту»);
- пустой исполнитель как удобная заглушка там, где протащить человека лень;
- запись, которую можно поправить;
- запись, появившаяся отдельно от изменения, о котором она написана;
- «сумма изменена» без «было 5 000, стало 500».
"""

import pytest
from sqlalchemy import delete, select, update

from core import events
from core import exceptions as errors
from core.services import audit_service, client_service, deal_service
from core.services.deal_service import DEAL_STAGE_CHANGED
from database.models import AuditEvent, ClientNote, User
from database.models.audit import (
    FACELESS_SOURCES,
    SOURCE_MAIL_SYNC,
    SOURCE_MANUAL,
    SOURCE_TELEPHONY_WEBHOOK,
)
from database.models.client import KIND_STAGE
from database.session import SessionLocal
from tests.conftest import API, RODIVSHIESYA_ZAPISI, make_manager
from tests.test_deals import DEALS, make_client

AUDIT = f"{API}/audit"
ROLES = f"{API}/roles"


@pytest.fixture
def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def subscribe():
    """Подписки на время теста — снимаем в фикстуре: реестр глобальный."""
    added = []

    def add(event, handler, **kwargs):
        sub = events.subscribe(event, handler, **kwargs)
        added.append(sub)
        return sub

    yield add
    for sub in added:
        events.unsubscribe(sub)


@pytest.fixture
def deal(manager_client):
    client = make_client(manager_client, "Клиент журнала")
    return manager_client.post(
        DEALS, json={"title": "Заявка журнала", "client_id": client["id"]}
    ).json()


def entries(root_client, **params) -> list[dict]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = root_client.get(f"{AUDIT}?{query}" if query else AUDIT)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def about(root_client, entity_type: str, entity_id: int) -> list[dict]:
    return entries(root_client, entity_type=entity_type, entity_id=entity_id)


# --- исполнитель протаскивается по всей цепочке ---


def test_one_action_leaves_one_name_in_every_record(root_client, manager_client, deal):
    """Одно действие человека — три записи в трёх местах, и все они его.

    Смена этапа порождает запись в журнале этапов (блок сделок), строку в ленте
    клиента (блок клиентов) и запись в журнале действий. Пусть хоть одна из них
    достанется «системе» — и на вопрос «кто это сделал» отвечать будет нечем,
    хотя формально «всё записано».
    """
    me = manager_client.get(f"{API}/auth/me").json()
    moved = manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    assert moved.status_code == 200, moved.text

    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert card["stage_history"][-1]["author_name"] == me["name"]

    feed = manager_client.get(f"{DEALS}/{deal['id']}/feed?kind={KIND_STAGE}").json()["items"]
    assert feed and feed[0]["author_id"] == me["id"], "запись в ленте осталась ничьей"

    logged = about(root_client, "deal", deal["id"])
    assert logged, "смена этапа не дошла до журнала"
    assert logged[0]["actor_id"] == me["id"]
    assert logged[0]["actor_name"] == me["name"]


def test_actor_name_survives_renaming_and_dismissal(root_client, session):
    """Имя в журнале — снимок, а не ссылка на справочник.

    Внешний ключ обнуляется при удалении сотрудника, а имя правится в профиле.
    И то и другое случается именно к тому моменту, когда в журнал приходят: за
    прошлый год и про уволившегося.
    """
    from tests.conftest import make_manager

    temp = make_manager(root_client, "leaving@test.local")
    who = temp.get(f"{API}/auth/me").json()
    client = temp.post(f"{API}/clients", json={"name": "Клиент уволенного"}).json()
    assert temp.delete(f"{API}/clients/{client['id']}").status_code == 200

    assert root_client.delete(f"{API}/staff/{who['id']}").status_code == 200

    logged = about(root_client, "client", client["id"])
    assert logged, "удаление клиента не дошло до журнала"
    assert logged[0]["actor_id"] is None, "внешний ключ должен обнулиться вместе с аккаунтом"
    assert logged[0]["actor_name"] == who["name"], "журнал забыл, кто это сделал"


def test_source_travels_down_the_chain_untouched(manager_client, deal, subscribe, session):
    """Источник уезжает подписчикам тем же, каким начал цепочку.

    Подменить его на «руку» в подписчике — значит стереть разницу между «Иванов
    списал руками» и «списалось, потому что Иванов провёл акт». Разбираться
    будут именно в этой разнице.
    """
    heard = []
    subscribe(
        DEAL_STAGE_CHANGED,
        lambda event: heard.append((event.source, event.source_ref)),
        is_participant=False,
    )

    actor = session.scalar(select(User).where(User.email == "manager@test.local"))
    deal_service.move_stage(
        session,
        deal["id"],
        "done",
        actor,
        source=SOURCE_MAIL_SYNC,
        source_ref="msg-42",
    )

    assert heard == [(SOURCE_MAIL_SYNC, "msg-42")], "источник не доехал до подписчика"


# --- source различает руку и автоматику ---


def test_manual_work_is_marked_as_manual(root_client, manager_client, deal):
    """Действие через интерфейс — это рука, и в журнале оно так и записано."""
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})

    logged = about(root_client, "deal", deal["id"])[0]
    assert logged["source"] == SOURCE_MANUAL
    assert logged["source_ref"] == "", "у ручного действия ссылаться не на что"


def test_source_is_required_and_checked(session, root_client):
    """`source` — обязательный аргумент без умолчания, и выдумать его нельзя.

    Умолчание здесь было бы «рука», и любое автоматическое действие, о котором
    забыли, выглядело бы в журнале как сделанное вручную — то есть журнал врал
    бы ровно в том случае, ради которого его читают.
    """
    actor = session.scalar(select(User).where(User.email == "root@test.local"))

    with pytest.raises(TypeError):
        audit_service.record(
            session, action="test.thing", actor=actor, entity_type="deal", entity_id=1
        )

    with pytest.raises(ValueError, match="Unknown audit source"):
        audit_service.record(
            session,
            action="test.thing",
            actor=actor,
            source="looks_plausible",
            entity_type="deal",
            entity_id=1,
        )


# --- пустой исполнитель бывает только у двух источников ---


def test_only_the_pbx_and_mail_may_have_no_actor():
    """Список источников без человека — закрытый и ровно из двух.

    Тест сторожит именно список: третий источник «без человека» появится не
    потому, что там правда нет человека, а потому, что протащить его оказалось
    неудобно.
    """
    assert set(FACELESS_SOURCES) == {SOURCE_TELEPHONY_WEBHOOK, SOURCE_MAIL_SYNC}


def test_empty_actor_is_refused_everywhere_else(session):
    """Пусто без причины не проходит ни в журнал, ни в ленту.

    Проверяем оба входа, а не только журнал: автор есть и у записи в ленте, и
    обезличить её так же легко.
    """
    with pytest.raises(ValueError, match="has no actor"):
        audit_service.record(
            session,
            action="test.thing",
            actor=None,
            source=SOURCE_MANUAL,
            entity_type="deal",
            entity_id=1,
        )

    with pytest.raises(ValueError, match="has no actor"):
        client_service.add_system_note(
            session, 1, None, KIND_STAGE, "Ничья запись", source=SOURCE_MANUAL
        )

    # А у вебхука и почты — проходит: там человека действительно нет.
    for source in FACELESS_SOURCES:
        audit_service.assert_actor(None, source, "Проверка")


def test_a_call_from_the_pbx_is_the_authorless_case(root_client, session):
    """Живой пример пустого исполнителя: событие прислала станция.

    Одновременно это проверка, что «пусто» не расползлось: у записи в ленте,
    порождённой действием человека, автор есть всегда (см. тест выше про одно
    действие и три записи).
    """
    from tests.test_telephony import send_event

    switched = root_client.post(f"{API}/modules/telephony", json={"enabled": True})
    assert switched.status_code == 200, switched.text
    secret = root_client.post(f"{API}/telephony/settings/secret").json()["secret"]
    try:
        client = root_client.post(
            f"{API}/clients", json={"name": "Звонивший", "phone": "+380671110000"}
        ).json()
        answer = send_event(
            secret,
            {
                "call_id": "audit-call-1",
                "direction": "in",
                "from": "+380671110000",
                "to": "+380440000000",
                "outcome": "answered",
                "duration": 30,
            },
        )
        assert answer.status_code == 200, answer.text

        notes = root_client.get(f"{API}/clients/{client['id']}/notes?kind=call").json()["items"]
        assert notes, "звонок не дошёл до ленты — проверять нечего"
        assert notes[0]["author_id"] is None, "у звонка от станции завёлся автор"
    finally:
        root_client.post(f"{API}/modules/telephony", json={"enabled": False})


# --- журнал только дописывается ---


def test_nobody_can_edit_or_delete_an_entry(root_client, manager_client, deal, session):
    """Ни root, ни автор, ни соседний модуль: запрет стоит на мэппере.

    Проверка в сервисе закрыла бы один путь из трёх. Обойдут её не
    злонамеренно, а по невнимательности — при разборе как раз того случая, ради
    которого журнал вёлся.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    logged = about(root_client, "deal", deal["id"])[0]

    entry = session.get(AuditEvent, logged["id"])
    entry.value_after = "переписали"
    with pytest.raises(errors.ForbiddenError) as edited:
        session.flush()
    assert edited.value.code == "audit_append_only"
    session.rollback()

    entry = session.get(AuditEvent, logged["id"])
    session.delete(entry)
    with pytest.raises(errors.ForbiddenError) as removed:
        session.flush()
    assert removed.value.code == "audit_append_only"
    session.rollback()

    # и запись на месте
    assert session.get(AuditEvent, logged["id"]) is not None


def test_a_mass_update_or_delete_cannot_rewrite_the_log(
    root_client, manager_client, deal, session
):
    """Запрет держится и против одного запроса на всю таблицу.

    Проверка выше правит **объект** — её ловит мэппер. Но `update()` и
    `delete()` уезжают в базу одним запросом, объектов не трогают вовсе и оба
    мэпперных запрета проходят насквозь: журнал переписывался молча, одной
    строкой из соседнего сервиса. Журнал, который можно тихо переписать,
    отвечает на вопрос «кто это сделал» ровно до первого человека, которому
    ответ невыгоден.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    logged = about(root_client, "deal", deal["id"])[0]

    with pytest.raises(errors.ForbiddenError) as rewritten:
        session.execute(
            update(AuditEvent).where(AuditEvent.id == logged["id"]).values(actor_name="Никто")
        )
    assert rewritten.value.code == "audit_append_only"
    session.rollback()

    with pytest.raises(errors.ForbiddenError) as wiped:
        session.execute(delete(AuditEvent).where(AuditEvent.id == logged["id"]))
    assert wiped.value.code == "audit_append_only"
    session.rollback()

    # И «почистить журнал целиком» — тоже нет.
    with pytest.raises(errors.ForbiddenError):
        session.execute(delete(AuditEvent))
    session.rollback()

    # Обход по самой таблице, мимо модели, закрыт тем же запретом: смотрим на
    # таблицу, а не на то, каким словом её назвали в запросе.
    with pytest.raises(errors.ForbiddenError):
        session.execute(delete(AuditEvent.__table__))
    session.rollback()

    entry = session.get(AuditEvent, logged["id"])
    assert entry is not None, "запись журнала всё-таки удалилась"
    assert entry.actor_name != "Никто", "запись журнала всё-таки переписали"


def test_the_log_is_still_readable(root_client, manager_client, deal, session):
    """Запрет закрывает правку, а не чтение.

    Пара к тесту выше: сторож на уровне сессии видит все запросы подряд, и
    заодно закрытый `SELECT` превратил бы журнал в таблицу, которую нельзя
    прочитать, — то есть в отсутствующую.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    logged = about(root_client, "deal", deal["id"])[0]

    assert session.scalars(select(AuditEvent).where(AuditEvent.id == logged["id"])).all()


def test_the_log_has_no_write_endpoints(root_client, manager_client, deal):
    """Ручки «добавить» и «удалить» нет и не будет.

    Ручка добавления позволила бы дописать в журнал что угодно и когда угодно —
    то есть отменила бы весь смысл затеи не хуже, чем ручка удаления.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    logged = about(root_client, "deal", deal["id"])[0]

    assert root_client.post(AUDIT, json={"action": "made.up"}).status_code == 405
    assert root_client.delete(f"{AUDIT}/{logged['id']}").status_code == 405
    assert root_client.patch(f"{AUDIT}/{logged['id']}", json={}).status_code == 405


# --- запись и изменение появляются вместе или не появляются вовсе ---


def test_a_refused_operation_leaves_no_trace(root_client, manager_client, deal, subscribe):
    """Участник отменил операцию — журнал не помнит перехода, которого не было.

    Записывай журнал постфактум, отдельной транзакцией, — и он расходился бы с
    действительностью ровно в тех случаях, когда операция упала на середине, то
    есть когда журнал и нужен.
    """
    def refuses(event):
        raise errors.ValidationError("Нечего списывать", code="nothing_to_write_off")

    subscribe(DEAL_STAGE_CHANGED, refuses, is_participant=True)

    response = manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    assert response.status_code == 422

    assert manager_client.get(f"{DEALS}/{deal['id']}").json()["stage"] == "new"
    assert about(root_client, "deal", deal["id"]) == [], "журнал запомнил отменённое"


def test_the_change_and_its_record_arrive_together(root_client, manager_client, deal):
    """Обратная половина: операция состоялась — запись есть.

    Порознь эти два теста доказывают только по половине; вместе — что журнал и
    изменение живут в одной транзакции.
    """
    moved = manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    assert moved.status_code == 200, moved.text

    assert manager_client.get(f"{DEALS}/{deal['id']}").json()["stage"] == "in_progress"
    assert len(about(root_client, "deal", deal["id"])) == 1


# --- старое значение рядом с новым ---


def test_money_change_keeps_the_old_number(root_client, manager_client, deal):
    """«Сумма изменена» без «было 5 000, стало 500» не отвечает ни на что."""
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"amount": 500000})
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"amount": 50000})

    changes = [
        e for e in about(root_client, "deal", deal["id"]) if e["action"] == "deal.amount_changed"
    ]
    assert len(changes) == 2

    latest, first = changes
    assert (first["value_before"], first["value_after"]) == (None, "500000"), (
        "первая сумма: «было» — не ноль и не пусто, суммы просто ещё не называли"
    )
    assert (latest["value_before"], latest["value_after"]) == ("500000", "50000")


def test_setting_the_same_number_twice_is_not_a_change(root_client, manager_client, deal):
    """Форма присылает сумму при каждом сохранении — записываем только правки.

    Иначе настоящее изменение утонет среди «изменил сумму с 5000 на 5000», а
    искать его будут именно здесь.
    """
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"amount": 700000})
    manager_client.patch(f"{DEALS}/{deal['id']}", json={"amount": 700000, "title": "Другое имя"})

    changes = [
        e for e in about(root_client, "deal", deal["id"]) if e["action"] == "deal.amount_changed"
    ]
    assert len(changes) == 1


def test_deletion_remembers_what_was_deleted(root_client, manager_client):
    """«client 5 удалён» не отвечает на вопрос «кого» — карточки уже нет."""
    client = make_client(manager_client, "Уходящий клиент")
    assert manager_client.delete(f"{API}/clients/{client['id']}").status_code == 200

    logged = about(root_client, "client", client["id"])
    assert logged, "удаление не дошло до журнала"
    assert logged[0]["action"] == "client.deleted"
    assert logged[0]["entity_label"] == "Уходящий клиент"
    # Величины у удаления нет: менялся не показатель, исчез сам объект.
    assert (logged[0]["value_before"], logged[0]["value_after"]) == (None, None)


def test_switching_a_module_records_both_states(root_client):
    """Вопрос «куда делся раздел» задают на следующий день после выключения.

    Смотрим записи про **этот** блок, а не общий счётчик по всем: страница
    журнала отдаёт полсотни строк, и как только соседние тесты нащёлкали столько
    же переключений, «стало на две больше» перестаёт наступать вовсе. Тест
    падал не от поломки, а от собственного соседства — проверено обратным
    порядком файлов.
    """
    # У блока нет числового идентификатора — его имя лежит в снимке названия.
    #
    # Отбор по имени идёт НА СЕРВЕРЕ (`search`), а не только здесь, и это не
    # ускорение. Страница журнала отдаёт полсотни строк; отбирая на своей
    # стороне, мы фильтруем то, что уже отрезано пагинацией, — и как только
    # соседние файлы нащёлкают полсотни своих переключений, записи про `tasks`
    # просто не доедут до этой строки. Так и случилось, когда в наборе появился
    # ещё один файл, включающий блоки (`tests/test_waybills.py`).
    #
    # Проверка на `entity_label` оставлена рядом нарочно: серверный поиск ищет и
    # по имени исполнителя тоже, и «tasks» теоретически может совпасть с чем-то
    # ещё. Сервер сужает, эта строка отбирает точно.
    about_tasks = lambda: [
        e
        for e in entries(root_client, action="module.switched", search="tasks")
        if e["entity_label"] == "tasks"
    ]
    # Считаем НОВЫЕ записи по номеру, а не общее количество.
    #
    # Количество здесь не работает в принципе, и это не мелочь настройки.
    # Страница журнала отдаёт полсотни строк; как только соседние файлы
    # нащёлкают полсотни переключений `tasks`, счёт упирается в потолок
    # страницы и «стало на две больше» перестаёт наступать НАВСЕГДА. Замерено
    # ровно так: `assert 50 == 50 + 2`.
    #
    # Номер последней записи потолком не ограничен: две новые всегда окажутся
    # первыми на первой странице, потому что журнал отдаётся от свежих к старым.
    bylo = about_tasks()
    posledniy = bylo[0]["id"] if bylo else 0

    assert root_client.post(f"{API}/modules/tasks", json={"enabled": False}).status_code == 200
    assert root_client.post(f"{API}/modules/tasks", json={"enabled": True}).status_code == 200

    logged = [e for e in about_tasks() if e["id"] > posledniy]
    assert len(logged) == 2, f"переключений записано {len(logged)}, а их было два"
    assert (logged[1]["value_before"], logged[1]["value_after"]) == ("on", "off")
    assert (logged[0]["value_before"], logged[0]["value_after"]) == ("off", "on")


def test_granting_and_revoking_access_is_recorded(root_client):
    """Доступ — единственное, что сотрудник не может вернуть себе сам."""
    from tests.conftest import make_manager

    temp = make_manager(root_client, "access@test.local")
    who = temp.get(f"{API}/auth/me").json()

    assert root_client.post(f"{API}/staff/{who['id']}/disable").status_code == 200
    assert root_client.post(f"{API}/staff/{who['id']}/enable").status_code == 200

    # Идентификаторы удалённых аккаунтов база переиспользует, поэтому по
    # `entity_id` находятся и записи про тёзку по номеру. Отбираем свои по
    # снимку имени — ровно то, ради чего снимок и хранится рядом с ключом.
    logged = [e for e in about(root_client, "user", who["id"]) if e["entity_label"] == who["name"]]
    assert [e["action"] for e in logged] == [
        "staff.enabled",
        "staff.disabled",
        "staff.approved",
    ]
    assert (logged[1]["value_before"], logged[1]["value_after"]) == ("active", "disabled")


def test_stock_move_answers_who_wrote_off_what(root_client):
    """«Кто списал две матрицы» — тот вопрос, ради которого журнал заведён."""
    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": True}).status_code == 200
    try:
        product = root_client.post(
            f"{API}/warehouse/products", json={"name": "Матрица 15.6", "unit": "pcs"}
        ).json()
        root_client.post(
            f"{API}/warehouse/moves",
            json={"product_id": product["id"], "kind": "in", "quantity": 5},
        )
        root_client.post(
            f"{API}/warehouse/moves",
            json={"product_id": product["id"], "kind": "writeoff", "quantity": 2},
        )

        logged = about(root_client, "product", product["id"])
        assert [e["action"] for e in logged] == ["stock.move_added", "stock.move_added"]
        assert logged[0]["actor_name"] == "Root"
        assert logged[0]["source"] == SOURCE_MANUAL
        # Остаток до и после — та самая старая величина рядом с новой.
        assert (logged[0]["value_before"], logged[0]["value_after"]) == ("5", "3")
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": False})


# --- что намеренно не пишется ---


def test_reading_screens_is_not_recorded(root_client, manager_client, deal):
    """Открытие экранов и чтение — другой объём и другая задача.

    Записывай их — и журнал вырастет на порядки, а ответ на вопрос «кто поменял
    сумму» утонет среди строк «открыл список клиентов».
    """
    before = len(entries(root_client))

    manager_client.get(f"{API}/clients")
    manager_client.get(f"{DEALS}/board")
    manager_client.get(f"{DEALS}/{deal['id']}")
    manager_client.get(f"{API}/dashboard")

    assert len(entries(root_client)) == before


# --- кто видит журнал ---


def test_reading_the_log_needs_its_own_permission(manager_client):
    """Журнал закрыт своим правом, а не должностью.

    Раньше здесь стояло «только root»: ролей не было, и другого способа
    закрыть раздел тоже. Теперь право `audit.view` выдаётся отдельно от всего
    остального — и его нет ни у менеджера, ни у бухгалтера, ни у проджекта.
    Пускать туда всех, кто видит список сотрудников, незачем: за журналом
    приходят не работать, а разбираться, когда что-то не сошлось.
    """
    denied = manager_client.get(AUDIT)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


def test_the_log_can_be_read_by_person(root_client, manager_client, deal):
    """«Что делал этот сотрудник» — второй вопрос, ради которого сюда заходят."""
    me = manager_client.get(f"{API}/auth/me").json()
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})

    mine = entries(root_client, actor_id=me["id"])
    assert mine, "по исполнителю не находится ничего"
    assert all(e["actor_id"] == me["id"] for e in mine)


def test_system_feed_entries_are_still_undeletable(root_client, manager_client, deal):
    """Журнал не отменил старого запрета: след действия в ленте тоже не стереть.

    Две записи об одном действии живут в разных местах и обе неприкосновенны —
    иначе «поправить» историю можно было бы через ту, о которой забыли.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})
    feed = manager_client.get(f"{DEALS}/{deal['id']}/feed?kind={KIND_STAGE}").json()["items"]
    assert feed, "запись в ленте не появилась — проверять нечего"

    client_id = manager_client.get(f"{DEALS}/{deal['id']}").json()["client_id"]
    denied = root_client.delete(f"{API}/clients/{client_id}/notes/{feed[0]['id']}")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "system_note_immutable"


def test_deleting_a_note_is_itself_recorded(root_client, manager_client):
    """Удаление рукописной заметки — тоже удаление, и оно попадает в журнал."""
    client = make_client(manager_client, "Клиент заметки")
    note = manager_client.post(
        f"{API}/clients/{client['id']}/notes", json={"kind": "note", "body": "Позвонить в среду"}
    ).json()

    assert manager_client.delete(f"{API}/clients/{client['id']}/notes/{note['id']}").status_code == 200

    logged = about(root_client, "note", note["id"])
    assert logged and logged[0]["action"] == "note.deleted"
    assert logged[0]["entity_label"] == "Позвонить в среду"


def test_the_feed_entry_and_the_log_entry_are_different_things(root_client, manager_client, deal):
    """Журнал дополняет ленту, а не заменяет её, и наоборот.

    Лента — рабочий поток менеджера: звонки, письма, встречи вперемешку и по
    одному клиенту. Журнал — один вид записи на все блоки, и в нём есть то,
    чего в ленте нет и быть не должно: источник и прежняя величина.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "in_progress"})

    feed = manager_client.get(f"{DEALS}/{deal['id']}/feed?kind={KIND_STAGE}").json()["items"]
    logged = about(root_client, "deal", deal["id"])[0]

    assert len(feed) == 1 and logged["action"] == "deal.stage_changed"
    assert (logged["value_before"], logged["value_after"]) == ("new", "in_progress")
    assert "source" not in feed[0], "источник живёт в журнале, а не в ленте"


def test_notes_of_a_deleted_client_keep_their_log(root_client, manager_client, session):
    """Журнал переживает удаление того, о чём он написан.

    Поэтому `entity_id` — не внешний ключ: CASCADE снёс бы запись об удалении
    вместе с удалённым, RESTRICT запретил бы удалять вовсе.
    """
    client = make_client(manager_client, "Совсем уходящий")
    manager_client.delete(f"{API}/clients/{client['id']}")

    # Мягкое удаление карточку из базы не убирает — убираем её насовсем.
    from database.models import Client

    session.delete(session.get(Client, client["id"]))
    session.commit()

    logged = about(root_client, "client", client["id"])
    assert logged, "запись об удалении исчезла вместе с удалённым"
    assert logged[0]["entity_label"] == "Совсем уходящий"


def test_no_stray_notes_without_an_author(root_client, manager_client, deal, session):
    """Сводная проверка: ничьих записей в ленте не появилось на ровном месте.

    Автор пуст ровно у писем, звонков и входящих из телеграма — там человека с
    НАШЕЙ стороны действительно нет: письмо прислал клиент, звонок принесла
    станция, сообщение написал клиент боту. У всех прочих видов записи автор
    обязан быть: пусто у них означало бы, что где-то исполнителя не протащили,
    и найти это место потом будет нечем.

    Исходящие в телеграме автора имеют — там отвечает живой менеджер, и его имя
    в ленте обязано стоять.

    **Смотрим на МИГ РОЖДЕНИЯ записи, а не на таблицу.** Прежде проверка
    сканировала `client_notes` целиком — и не могла отличить «автора не
    протащили» от «автора уволили». У `client_notes.author_id` стоит
    `ondelete="SET NULL"` (иначе удалить сотрудника было бы нельзя вовсе, а
    висячий номер в колонке хуже пустоты), поэтому запись, честно подписанная
    живым человеком, становится ничьей ровно в тот миг, когда человека
    увольняют. Само это поведение объявлено нормой в
    `test_actor_name_survives_renaming_and_dismissal` выше.

    База у набора ОБЩАЯ, а сотрудников заводят и убирают фикстурами в доброй
    половине файлов. Достаточно соседу завести сборщика, дать ему аннулировать
    заказ и убрать сборщика за собой — и сводная проверка краснела на полностью
    исправном коде. Так и вышло: сцепку поймал прогон в обратном порядке файлов,
    тот самый, что CI гоняет вторым заходом.

    Ширина при этом НЕ потеряна, и это важнее удобства: слушатель
    `_zapominat_avtorov_zapisey` (tests/conftest.py) запоминает вид и автора при
    каждой вставке за весь прогон. Сузить проверку до записей одного теста
    значило бы выменять ложную тревогу на слепоту, а стеречь надо как раз то,
    чего никто не ждёт.
    """
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": "done"})

    nichi = sorted({
        vid for vid, avtor in RODIVSHIESYA_ZAPISI
        if avtor is None and vid not in ("email", "call", "telegram")
    })
    assert nichi == [], "ничья запись в ленте у вида, где человек есть: " + ", ".join(nichi)

    # И сам список не пуст: иначе проверка зеленела бы оттого, что слушатель
    # отвалился, а выглядела бы рабочей.
    assert len(RODIVSHIESYA_ZAPISI) > 5, (
        f"слушатель записей не сработал ни разу ({len(RODIVSHIESYA_ZAPISI)}) — "
        "проверка ничего не стережёт"
    )


def test_uvolennyy_sotrudnik_ne_unosit_zapis_iz_lenty(root_client, session):
    """Запись остаётся, автор пустеет. Это НЕ то же, что «автора не протащили».

    Разбор стоит рядом с проверкой выше: она из-за этой самой пары и перешла на
    слушателя. Здесь показано, что пара действительно такова, — иначе переход
    выглядел бы поблажкой самому себе.

    Терять запись при увольнении нельзя: лента отвечает на вопрос «что было с
    клиентом», а не «кто у нас сейчас работает». Уволенный менеджер — причина
    открыть архив, а не стереть год переписки.

    Сотрудник здесь СВОЙ, заведённый на один тест: общий `manager_client` унёс
    бы с собой все следующие проверки файла, а база у набора одна.
    """
    uhodyashchiy = make_manager(root_client, "uhodit-iz-lenty@test.local")
    klient = uhodyashchiy.post(f"{API}/clients", json={"name": "Клиент уходящего"}).json()
    zapis = uhodyashchiy.post(
        f"{API}/clients/{klient['id']}/notes", json={"body": "Договорились на вторник"}
    )
    assert zapis.status_code == 201, zapis.text
    zapis_id = zapis.json()["id"]

    avtor_id = next(
        u["id"] for u in root_client.get(f"{API}/staff").json()["items"]
        if u["email"] == "uhodit-iz-lenty@test.local"
    )

    # Увольняем ручкой, а не запросом мимо кода.
    assert root_client.delete(f"{API}/staff/{avtor_id}").status_code == 200
    session.expire_all()

    ostalas = session.get(ClientNote, zapis_id)
    assert ostalas is not None, "запись исчезла вместе с сотрудником — потерян год ленты"
    assert ostalas.author_id is None, (
        "автор уцелел висячим номером — на такого пользователя уже никто не ответит"
    )
    assert ostalas.body == "Договорились на вторник", "текст записи пострадал при увольнении"
    # Пользователя и правда не стало: иначе проверка выше ничего не значит.
    assert session.get(User, avtor_id) is None


def test_bringing_a_client_back_is_recorded_too(root_client, manager_client):
    """Возврат из корзины виден в журнале наравне с удалением.

    Иначе журнал врёт умолчанием: «удалил клиента» в нём есть, «вернул через
    час» — нет, и читающий через месяц уверен, что карточки не стало. Спорить
    при этом не с чем: записи о возврате просто не существует.
    """
    client = manager_client.post(f"{API}/clients", json={"name": "Ушёл и вернулся"}).json()
    assert manager_client.delete(f"{API}/clients/{client['id']}").status_code == 200
    assert root_client.post(f"{API}/clients/{client['id']}/restore").status_code == 200

    trail = [e["action"] for e in about(root_client, "client", client["id"])]
    assert trail[:2] == ["client.restored", "client.deleted"], trail

    restored = about(root_client, "client", client["id"])[0]
    assert restored["entity_label"] == "Ушёл и вернулся"
    assert restored["actor_name"], "возврат записан без исполнителя"


def test_restoring_what_was_not_deleted_does_not_invent_a_record(root_client, manager_client):
    """Возврат живой записи — не событие.

    Кнопка «вернуть» доступна там, где карточка уже на месте (открыли две
    вкладки, в одной вернули). Молча отвечать «готово» можно, а вот писать в
    журнал возврат, которого не было, — нельзя: журнал перестанет быть
    свидетельством.
    """
    client = manager_client.post(f"{API}/clients", json={"name": "Никуда не уходил"}).json()
    before = len(about(root_client, "client", client["id"]))

    assert root_client.post(f"{API}/clients/{client['id']}/restore").status_code == 200

    assert len(about(root_client, "client", client["id"])) == before, "записан возврат без удаления"


def test_the_permission_constructor_leaves_a_trail(root_client):
    """Кто собрал должность, кто переписал права, кому её отдали.

    Вопрос «кто дал бухгалтеру доступ к отчётам» задают ровно тогда, когда
    отвечать уже некому. До этих записей конструктор доступов не оставлял следа
    вообще: в журнале была только смена «root ↔ менеджер» — одна дверь из двух
    десятков, и не главная.
    """
    made = root_client.post(
        f"{API}/roles", json={"name": "Бухгалтер для журнала", "permissions": ["reports.view"]}
    )
    assert made.status_code == 201, made.text
    role = made.json()

    try:
        born = entries(root_client, action="role.created")[0]
        assert born["entity_label"] == "Бухгалтер для журнала"
        assert born["value_after"] == "reports.view"
        assert born["actor_name"], "роль заведена без исполнителя"

        # Правка прав: видно, что было и что стало.
        changed = root_client.patch(
            f"{ROLES}/{role['id']}",
            json={"permissions": ["reports.view", "reports.view_amounts"]},
        )
        assert changed.status_code == 200, changed.text

        edit = entries(root_client, action="role.permissions_changed")[0]
        assert edit["value_before"] == "reports.view"
        assert edit["value_after"] == "reports.view, reports.view_amounts"

        # Сохранение без изменений в журнал не идёт: экран присылает набор
        # целиком при каждом нажатии, и шум утопил бы настоящие правки.
        quiet = len(entries(root_client, action="role.permissions_changed"))
        root_client.patch(
            f"{ROLES}/{role['id']}",
            json={"permissions": ["reports.view_amounts", "reports.view"]},
        )
        assert len(entries(root_client, action="role.permissions_changed")) == quiet

        # Назначение — запись на СОТРУДНИКА: спрашивают про человека.
        from tests.test_roles import _user_id

        make_manager(root_client, "trail@test.local")
        user_id = _user_id(root_client, "trail@test.local")
        assert root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role["id"]}).status_code == 200

        given = about(root_client, "user", user_id)[0]
        assert given["action"] == "role.assigned"
        assert given["value_after"] == "Бухгалтер для журнала"
    finally:
        from tests.test_roles import _user_id as _uid

        root_client.post(f"{ROLES}/assign/{_uid(root_client, 'trail@test.local')}", json={"role_id": None})
        root_client.delete(f"{ROLES}/{role['id']}")


def test_deleting_a_role_is_recorded_too(root_client):
    """Исчезнувшая должность — тоже событие: права пропали у всех, кто её носил."""
    role = root_client.post(
        f"{API}/roles", json={"name": "Временная должность", "permissions": ["clients.view"]}
    ).json()
    assert root_client.delete(f"{ROLES}/{role['id']}").status_code == 200

    # Спрашиваем про ЭТУ роль, а не «самое свежее событие про роли вообще».
    # Второе привязывало проверку к соседям и к часам: любое чужое событие о
    # ролях, оказавшееся новее, вставало первым, и проверка краснела на
    # исправном коде. Поймано обратным проходом ворот.
    gone = about(root_client, "role", role["id"])[0]
    assert gone["action"] == "role.deleted"
    assert gone["entity_label"] == "Временная должность"


def test_everything_that_disappears_leaves_a_trace(root_client, manager_client):
    """Удаление любого несущего объекта видно в журнале.

    Правило простое: если исчезновение объекта однажды заставит спросить «а куда
    оно делось» — запись обязана быть. Клиент, заявка, товар и роль это умели, а
    фирма, доска, ссылка на неё и почтовый ящик исчезали молча.

    Про каждый из них спрашивают, и вопросы разные: фирма — это реквизиты на
    бумаге у клиента; доска — витрина и файлы, снесённые с диска навсегда;
    ссылка — «почему у клиента перестало открываться»; ящик — «почему письма
    перестали ходить».
    """
    for block in ("companies", "boards", "mail"):
        root_client.post(f"{API}/modules/{block}", json={"enabled": True})

    company = root_client.post(f"{API}/companies", json={"name": "Юрлицо на снос"}).json()
    board = manager_client.post(f"{API}/boards", json={"title": "Доска на снос"}).json()
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    mailbox = root_client.post(
        f"{API}/mail/accounts", json={"address": "gone@example.com", "title": "Ящик на снос"}
    ).json()

    assert manager_client.delete(f"{API}/shares/{share['id']}").status_code == 200
    assert manager_client.delete(f"{API}/boards/{board['id']}").status_code == 200
    assert root_client.delete(f"{API}/companies/{company['id']}").status_code == 200
    assert root_client.delete(f"{API}/mail/accounts/{mailbox['id']}").status_code == 200

    for kind, label in (
        ("company", "Юрлицо на снос"),
        ("board", "Доска на снос"),
        ("share", "Доска на снос"),
        ("mailbox", "gone@example.com"),
    ):
        recorded = entries(root_client, entity_type=kind)
        assert recorded, f"удаление {kind} прошло молча"
        assert recorded[0]["action"] == f"{kind}.deleted"
        assert recorded[0]["entity_label"] == label, kind
        assert recorded[0]["actor_name"], f"{kind}: удаление записано без исполнителя"


# --- длина полей: журнал не роняет то, о чём он пишет ---


def test_long_values_do_not_break_the_operation_being_recorded(session, root_client):
    """Длинная строка обрезается, а не роняет саму операцию.

    Журнал пишется в ТОЙ ЖЕ транзакции, что и изменение. Строка длиннее колонки
    — это `Data too long for column`, то есть отказ основной операции: сотрудник
    не смог бы провести акт из-за длинного имени действия. `actor_name`,
    `source_ref` и `entity_label` обрезались, а `action` и `entity_type` — нет.

    Пределы берём у колонок, а не переписываем сюда числами: сузят колонку в
    миграции — тест поедет за ней.
    """
    actor = session.scalar(select(User).where(User.email == "root@test.local"))
    columns = AuditEvent.__table__.c

    entry = audit_service.record(
        session,
        action="deal." + "x" * 200,
        actor=actor,
        source=SOURCE_MANUAL,
        entity_type="y" * 200,
        entity_id=1,
        entity_label="z" * 500,
        source_ref="r" * 200,
    )

    for name in ("action", "entity_type", "entity_label", "source_ref", "actor_name"):
        limit = columns[name].type.length
        assert len(getattr(entry, name)) <= limit, f"{name} длиннее колонки ({limit})"


# --- возврат из корзины помнит, кого вернули ---


def test_a_restore_without_a_name_is_refused_just_like_a_deletion(session):
    """У возврата то же требование, что и у удаления, и по тому же доводу.

    «client 5 возвращён» не отвечает на вопрос «кого»: спрашивают об этом
    позже, а карточку к тому времени могли удалить снова — уже начисто.
    Требование стояло только на половине пары, и в журнале получалось
    «удалил Иванова» рядом с безымянным возвратом.
    """
    actor = session.scalar(select(User).where(User.email == "root@test.local"))

    with pytest.raises(ValueError, match="without a label"):
        audit_service.record_restore(
            session, actor=actor, entity_type="client", entity_id=5, entity_label=""
        )

    # Вторая половина пары — как и была.
    with pytest.raises(ValueError, match="without a label"):
        audit_service.record_deletion(
            session, actor=actor, entity_type="client", entity_id=5, entity_label=""
        )


# --- поиск по журналу: закрепляем то, что уже цело ---


def _log_lines(session, actor, action: str, labels) -> None:
    for label in labels:
        audit_service.record(
            session,
            action=action,
            actor=actor,
            source=SOURCE_MANUAL,
            entity_type="deal",
            entity_id=None,
            entity_label=label,
        )


def test_wildcards_in_the_search_string_are_letters_not_a_pattern(session):
    """`%` и `_` в строке поиска — то, что человек набрал, а не шаблон LIKE.

    Держится это общим слоем (`database/query.contains` экранирует сам), но
    проверяется здесь: журнал — единственное место, куда приходят разбираться,
    и поиск по одному знаку `%`, отдающий всю таблицу, был бы выгрузкой всего
    журнала по случайному нажатию.
    """
    from database.repositories import audit as audit_repo

    actor = session.scalar(select(User).where(User.email == "root@test.local"))
    _log_lines(session, actor, "test.wildcard", ("тест_один", "тестХодин"))

    found, _ = audit_repo.search(session, action="test.wildcard", q="тест_один")
    assert [e.entity_label for e in found] == ["тест_один"], "`_` сработал как «любой символ»"

    _, total = audit_repo.search(session, action="test.wildcard", q="%")
    assert total == 0, "поиск по одному проценту отдал всё подряд"


def test_page_zero_is_the_first_page_and_not_a_negative_offset(session):
    """`page=0` не превращается в `OFFSET -50`.

    Снаружи не пройдёт — маршрут объявляет `ge=1`, — но защита в двадцати
    четырёх местах держится на памяти автора следующего маршрута. Ниже стоит
    `offset_for`, и забыть её нельзя; тест закрепляет, что журнал ходит именно
    через неё. Отрицательное смещение для MySQL — синтаксическая ошибка, то
    есть пятисотка на живом сервере.
    """
    from database.repositories import audit as audit_repo

    actor = session.scalar(select(User).where(User.email == "root@test.local"))
    _log_lines(session, actor, "test.paging", ("строка 1", "строка 2", "строка 3"))

    first, total = audit_repo.search(session, action="test.paging", page=1, per_page=2)
    assert total == 3
    zero, _ = audit_repo.search(session, action="test.paging", page=0, per_page=2)
    assert [e.id for e in zero] == [e.id for e in first]


def test_the_share_token_never_reaches_the_journal(root_client, manager_client):
    """Токен ссылки в журнал не пишется: он и есть ключ к витрине.

    Журнал читают шире, чем настройки доски, и строка с токеном в нём была бы
    работающей ссылкой, разложенной по чужим экранам.
    """
    root_client.post(f"{API}/modules/boards", json={"enabled": True})
    board = manager_client.post(f"{API}/boards", json={"title": "Доска с токеном"}).json()
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    token = share["token"]
    assert manager_client.delete(f"{API}/shares/{share['id']}").status_code == 200

    everything = " ".join(
        str(entry) for entry in entries(root_client, entity_type="share")
    )
    assert token not in everything, "токен ссылки утёк в журнал"
