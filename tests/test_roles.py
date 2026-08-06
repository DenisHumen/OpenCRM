"""Роли и конструктор доступов.

Проверяется не «сохраняются ли галочки», а то, ради чего конструктор существует:
что право нельзя обойти адресом, что отказ объясняет себя, что сотрудник не
может расширить себя сам и что систему нельзя привести в состояние, из которого
права уже некому раздать.

База у тестов общая и переживает файл, поэтому всё, что здесь создаётся —
роли и сотрудники, — убирается за собой: чужой тест не должен падать из-за
роли, оставшейся от этого файла.
"""

import pytest
from fastapi.testclient import TestClient

from core import modules, permissions
from core.services import modules_service, permissions_service
from tests.conftest import API, login, make_manager, register
from web.main import app

ROLES = f"{API}/roles"
STAFF = f"{API}/staff"


# --- вспомогательное ---


@pytest.fixture
def role_maker(root_client):
    """Создаёт роли и убирает их после теста.

    Уборка в фикстуре, а не в конце теста: тело до конца может и не дойти, а
    роль с тем же названием во втором тесте упрётся в `role_name_taken`.
    """
    created: list[int] = []

    def make(name: str, codes: list[str]) -> dict:
        response = root_client.post(ROLES, json={"name": name, "permissions": codes})
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
        return response.json()

    yield make

    for role_id in created:
        root_client.delete(f"{ROLES}/{role_id}")


@pytest.fixture
def staff_maker(root_client):
    """Сотрудник с назначенной ролью и его залогиненный клиент."""
    created: list[int] = []

    def make(email: str, role_id: int | None) -> TestClient:
        client = make_manager(root_client, email)
        user_id = _user_id(root_client, email)
        created.append(user_id)
        assigned = root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role_id})
        assert assigned.status_code == 200, assigned.text
        return client

    yield make

    for user_id in created:
        root_client.delete(f"{STAFF}/{user_id}")


def _user_id(root_client, email: str) -> int:
    people = root_client.get(STAFF).json()["items"]
    return next(u["id"] for u in people if u["email"] == email)


def _client_and_deal(manager_client) -> tuple[int, int]:
    client = manager_client.post(f"{API}/clients", json={"name": "Заказчик прав"}).json()
    deal = manager_client.post(
        f"{API}/deals",
        json={"title": "Работа с суммой", "client_id": client["id"], "amount": 500000},
    ).json()
    return client["id"], deal["id"]


# --- реестр ---


def test_the_matrix_is_built_from_the_module_registry(manager_client):
    """Появился блок — появилась строка, без правки списка прав руками.

    Список, набранный вручную, разошёлся бы с реестром при первом же новом
    блоке и разошёлся бы молча: раздел без права открыт всем.
    """
    body = manager_client.get(f"{ROLES}/matrix").json()
    rows = {area["key"] for area in body["areas"]}
    assert set(modules.KEYS) <= rows, "блок из реестра не попал в матрицу"

    for area in body["areas"]:
        if area["module"]:
            assert area["module"] in modules.KEYS
            assert permissions.VIEW in area["actions"]


def test_a_new_module_gets_its_row_without_touching_the_permission_list(monkeypatch):
    """Проверяем правило, а не сегодняшний состав реестра.

    Реальный новый блок появится когда-нибудь; правило «строка появляется сама»
    должно быть проверено сейчас, иначе оно сломается молча.
    """
    invented = modules.Module(key="test_invented", ready=True, default=False)
    fake = modules.MODULES + (invented,)
    monkeypatch.setattr(modules, "MODULES", fake)
    monkeypatch.setattr(modules, "BY_KEY", {m.key: m for m in fake})

    areas = permissions._build()
    row = next((a for a in areas if a.key == "test_invented"), None)
    assert row is not None, "новый блок не получил строку в матрице"
    assert row.actions == permissions.BASE_ACTIONS
    assert row.module == "test_invented"


def test_every_preset_grants_only_permissions_that_exist():
    """Пресет с несуществующим правом — молчаливая дыра: право не проверится
    нигде, а роль будет выглядеть выданной."""
    for key, preset in permissions_service.PRESETS.items():
        for code in preset["permissions"]:
            assert permissions.parse(code) is not None, f"{key}: несуществующее право {code}"


# --- право проверяется на сервере ---


def test_permission_is_enforced_by_the_api_not_only_hidden_in_the_ui(
    role_maker, staff_maker, manager_client
):
    """Спрятать кнопку недостаточно — адрес продолжает работать.

    Роль видит заявки и не умеет их заводить. Проверяем не интерфейс, а прямой
    запрос: именно он остаётся у того, кто открывал раздел вчера.
    """
    client_id, deal_id = _client_and_deal(manager_client)
    role = role_maker("Только смотрит заявки", ["deals.view", "deals.view_others", "clients.view"])
    watcher = staff_maker("watcher@test.local", role["id"])

    assert watcher.get(f"{API}/deals").status_code == 200
    assert watcher.get(f"{API}/deals/{deal_id}").status_code == 200

    denied = watcher.post(
        f"{API}/deals", json={"title": "Мимо прав", "client_id": client_id}
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "permission_denied"
    # Отказ называет причину: молчаливый 404 превратил бы настройку доступов
    # в гадание.
    assert "deals.create" in denied.json()["error"]["message"]

    assert watcher.patch(f"{API}/deals/{deal_id}", json={"title": "Тоже мимо"}).status_code == 403
    assert watcher.delete(f"{API}/deals/{deal_id}").status_code == 403
    assert watcher.post(f"{API}/deals/{deal_id}/move", json={"stage": "done"}).status_code == 403


def test_refusal_names_the_missing_permission_everywhere(role_maker, staff_maker):
    """Одинаковый по форме отказ на всех разделах: код плюс имя права.

    Перебор по областям, а не три примера руками: раздел, забытый в списке, —
    ровно тот, который потом и окажется открытым.
    """
    role = role_maker("Ничего не может", ["clients.view"])
    nobody = staff_maker("nobody@test.local", role["id"])

    for path in (f"{API}/tasks", f"{API}/documents", f"{API}/companies"):
        response = nobody.get(path)
        assert response.status_code == 403, (path, response.text)
        error = response.json()["error"]
        assert error["code"] == "permission_denied", path
        assert ".view" in error["message"], path


def test_a_user_without_a_role_gets_nothing_but_a_reason(staff_maker):
    """Роль сняли — доступа нет, но человек понимает, что произошло.

    Пустая CRM без объяснения выглядит как поломка, и первым делом её понесут
    чинить, а не просить права.
    """
    homeless = staff_maker("homeless@test.local", None)
    response = homeless.get(f"{API}/clients")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert "clients.view" in response.json()["error"]["message"]


# --- порядок проверок ---


def test_a_switched_off_module_answers_before_the_permission_does(
    root_client, role_maker, staff_maker
):
    """Порядок: блок включён → есть право. Не наоборот.

    У сотрудника может быть право на склад, который в этом бизнесе выключен.
    Ответ «нет права» отправил бы владельца искать несуществующую ошибку в
    матрице доступов, хотя чинить надо переключатель блока.
    """
    role = role_maker("Кладовщик", ["warehouse.view", "warehouse.create"])
    keeper = staff_maker("keeper@test.local", role["id"])

    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": True}).status_code == 200
    assert keeper.get(f"{API}/warehouse/products").status_code == 200

    try:
        assert root_client.post(
            f"{API}/modules/warehouse", json={"enabled": False}
        ).status_code == 200
        blocked = keeper.get(f"{API}/warehouse/products")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "module_disabled", (
            "выключенный блок обязан отвечать раньше, чем зайдёт речь о правах"
        )
    finally:
        modules_service.invalidate()
        root_client.post(f"{API}/modules/warehouse", json={"enabled": False})


def test_no_permission_and_no_module_still_blames_the_module(
    root_client, role_maker, staff_maker
):
    """Нет ни блока, ни права — причина всё равно блок.

    Иначе сообщение зависело бы от того, какую проверку написали первой, и
    владелец, включив блок, получил бы второй отказ там, где ждал успеха.
    """
    role = role_maker("Без склада", ["clients.view"])
    outsider = staff_maker("outsider@test.local", role["id"])

    assert root_client.post(
        f"{API}/modules/warehouse", json={"enabled": False}
    ).status_code == 200
    response = outsider.get(f"{API}/warehouse/products")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "module_disabled"


# --- деньги отдельно от раздела ---


def test_a_manager_can_run_a_deal_without_seeing_its_money(
    role_maker, staff_maker, manager_client
):
    """Главная просьба, ради которой прав на раздел недостаточно.

    Заявка видна и правится, сумма — нет. Проверяем ответ сервера, а не экран:
    сумма, «спрятанная» только в интерфейсе, лежит в теле ответа и видна из
    консоли браузера.
    """
    _client_id, deal_id = _client_and_deal(manager_client)
    role = role_maker(
        "Без денег",
        ["deals.view", "deals.view_others", "deals.edit", "clients.view"],
    )
    poor = staff_maker("nomoney@test.local", role["id"])

    card = poor.get(f"{API}/deals/{deal_id}")
    assert card.status_code == 200, card.text
    body = card.json()
    # Ключи на месте, значения пустые: форма ответа не зависит от того, кто
    # спрашивает.
    for key in ("amount", "prepaid", "remainder", "is_paid"):
        assert key in body, key
        assert body[key] is None, f"{key} утекло без права на суммы"

    board = poor.get(f"{API}/deals/board").json()
    for column in board["columns"]:
        assert column["amount_total"] is None, "итог колонки выдал сумму"
        for deal in column["deals"]:
            assert deal["amount"] is None

    listed = poor.get(f"{API}/deals").json()
    assert all(item["amount"] is None for item in listed["items"])


def test_who_cannot_see_the_money_cannot_overwrite_it_either(
    role_maker, staff_maker, manager_client
):
    """Иначе право декоративно наполовину.

    Сумму не показали, но её можно перезаписать вслепую — и прежнее значение
    теряется без следа. Заметит это тот, кто придёт сверять с договором.
    """
    _client_id, deal_id = _client_and_deal(manager_client)
    role = role_maker(
        "Правит, но не видит",
        ["deals.view", "deals.view_others", "deals.edit", "clients.view"],
    )
    poor = staff_maker("blindedit@test.local", role["id"])

    denied = poor.patch(f"{API}/deals/{deal_id}", json={"amount": 1})
    assert denied.status_code == 403, denied.text
    assert "deals.view_amounts" in denied.json()["error"]["message"]

    # А не денежные поля правятся как ни в чём не бывало.
    assert poor.patch(f"{API}/deals/{deal_id}", json={"title": "Новое имя"}).status_code == 200
    # И сумма не пострадала.
    assert manager_client.get(f"{API}/deals/{deal_id}").json()["amount"] == 500000


def test_the_same_deal_shows_its_money_to_someone_who_may_see_it(
    role_maker, staff_maker, manager_client
):
    """Обратная сторона: право на суммы действительно их показывает.

    Без этой половины предыдущий тест проходил бы и на сломанной выдаче, где
    суммы не видны никому.
    """
    _client_id, deal_id = _client_and_deal(manager_client)
    role = role_maker(
        "С деньгами",
        ["deals.view", "deals.view_others", "deals.view_amounts", "clients.view"],
    )
    rich = staff_maker("withmoney@test.local", role["id"])

    body = rich.get(f"{API}/deals/{deal_id}").json()
    assert body["amount"] == 500000
    assert body["remainder"] == 500000


def test_money_is_hidden_in_the_client_card_too(role_maker, staff_maker, manager_client):
    """Врезка в карточке клиента — та же заявка, только сбоку.

    Забыть про неё значит оставить открытой дверь рядом с закрытой.
    """
    client_id, _deal_id = _client_and_deal(manager_client)
    role = role_maker(
        "Без денег в карточке",
        ["clients.view", "deals.view", "deals.view_others"],
    )
    poor = staff_maker("nomoney2@test.local", role["id"])

    card = poor.get(f"{API}/clients/{client_id}").json()
    assert card["deals"], "заявок нет — проверять нечего"
    assert all(deal["amount"] is None for deal in card["deals"])


def test_the_dashboard_hides_money_without_the_right(role_maker, staff_maker):
    role = role_maker("Сводка без денег", ["clients.view", "deals.view"])
    poor = staff_maker("nomoney3@test.local", role["id"])

    body = poor.get(f"{API}/dashboard").json()
    assert body["money_in_work"] is None
    assert body["money_won_this_month"] is None
    # А счётчики остаются: сводка сужается, а не отказывает.
    assert isinstance(body["clients_total"], int)


def test_the_revenue_report_is_money_and_closes_whole(role_maker, staff_maker, root_client):
    """Отчёт по выручке без сумм — пустая таблица. Честнее закрыть целиком."""
    assert root_client.post(f"{API}/modules/reports", json={"enabled": True}).status_code == 200
    role = role_maker("Отчёты без денег", ["reports.view"])
    poor = staff_maker("nomoney4@test.local", role["id"])

    denied = poor.get(f"{API}/reports/revenue")
    assert denied.status_code == 403
    assert "reports.view_amounts" in denied.json()["error"]["message"]

    # Воронка — это счёт заявок, а не деньги: она открыта.
    assert poor.get(f"{API}/reports/funnel").status_code == 200

    # Источники сужаются: откуда приходят — видно, почём — нет.
    sources = poor.get(f"{API}/reports/sources")
    assert sources.status_code == 200, sources.text
    assert sources.json()["revenue_total"] is None

    # Выгрузка обязана совпадать с экраном, иначе право обходится кнопкой.
    export = poor.get(f"{API}/reports/sources.csv")
    assert export.status_code == 200
    assert poor.get(f"{API}/reports/revenue.csv").status_code == 403


# --- доступ к данным ---


def test_without_view_others_a_manager_sees_only_their_own_deals(
    role_maker, staff_maker, root_client, manager_client
):
    """«Видит только свои заявки» — фильтр в запросах, а не признак на экране.

    Спрятанная на экране чужая карточка остаётся доступной по адресу, в списке
    и в поиске, то есть не спрятана вовсе.
    """
    client_id, foreign_deal = _client_and_deal(manager_client)
    role = role_maker("Только свои", ["deals.view", "deals.create", "clients.view"])
    owner = staff_maker("owns@test.local", role["id"])
    owner_id = _user_id(root_client, "owns@test.local")

    mine = owner.post(
        f"{API}/deals",
        json={"title": "Моя заявка", "client_id": client_id, "manager_id": owner_id},
    )
    assert mine.status_code == 201, mine.text
    mine_id = mine.json()["id"]

    listed = owner.get(f"{API}/deals").json()
    ids = {item["id"] for item in listed["items"]}
    assert mine_id in ids
    assert foreign_deal not in ids, "чужая заявка попала в список"

    # И по прямому адресу тоже — с названной причиной, а не молчаливым 404.
    direct = owner.get(f"{API}/deals/{foreign_deal}")
    assert direct.status_code == 403, direct.text
    assert direct.json()["error"]["code"] == "permission_denied"
    assert "deals.view_others" in direct.json()["error"]["message"]

    # Канбан и врезка в карточке клиента подчиняются тому же правилу.
    board = owner.get(f"{API}/deals/board").json()
    on_board = {d["id"] for column in board["columns"] for d in column["deals"]}
    assert foreign_deal not in on_board

    card = owner.get(f"{API}/clients/{client_id}").json()
    assert foreign_deal not in {d["id"] for d in card["deals"]}


def test_search_does_not_leak_what_the_role_may_not_see(
    root_client, role_maker, staff_maker, manager_client
):
    """Поиск иначе становится обходом доступов.

    Через него видны имена и названия записей из закрытого раздела — то есть
    ровно то, что раздел и закрывает. Группа при этом остаётся в ответе пустой,
    а не исчезает: форма ответа одна при любом наборе прав.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200
    created = manager_client.post(f"{API}/clients", json={"name": "Секретный Заказчик"})
    assert created.status_code == 201
    manager_client.post(
        f"{API}/boards", json={"title": "Секретная доска", "client_id": created.json()["id"]}
    )

    role = role_maker("Без клиентов и досок", ["deals.view"])
    blind = staff_maker("blindsearch@test.local", role["id"])

    found = blind.get(f"{API}/search", params={"q": "Секрет"})
    assert found.status_code == 200, found.text
    assert found.json()["clients"] == {"items": [], "total": 0}
    assert found.json()["boards"] == {"items": [], "total": 0}

    # А тому, у кого права есть, поиск работает как работал.
    seen = manager_client.get(f"{API}/search", params={"q": "Секрет"}).json()
    assert seen["clients"]["items"], "поиск перестал находить то, что видно по правам"


def test_a_foreign_manager_id_in_the_query_does_not_widen_the_scope(
    role_maker, staff_maker, root_client, manager_client
):
    """Ограничение приходит из прав, а фильтр — из запроса. Слить их нельзя.

    Иначе достаточно прислать чужой `manager_id`, чтобы обойти ограничение, —
    и выглядело бы это как работающая проверка.
    """
    _client_id, foreign_deal = _client_and_deal(manager_client)
    role = role_maker("Только свои-2", ["deals.view", "clients.view"])
    owner = staff_maker("owns2@test.local", role["id"])

    foreign_manager = manager_client.get(f"{API}/deals/{foreign_deal}").json()["manager_id"]
    listed = owner.get(f"{API}/deals", params={"manager_id": foreign_manager}).json()
    assert foreign_deal not in {item["id"] for item in listed["items"]}


# --- сотрудник не может расширить себя сам ---


def test_an_employee_cannot_grant_themselves_a_permission(role_maker, staff_maker, root_client):
    """Право менять роли — само по себе право, и по умолчанию его нет ни у кого."""
    role = role_maker("Обычный", ["clients.view", "deals.view"])
    ordinary = staff_maker("ordinary@test.local", role["id"])

    denied = ordinary.patch(
        f"{ROLES}/{role['id']}", json={"permissions": list(permissions.all_codes())}
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"
    assert "roles.manage" in denied.json()["error"]["message"]

    assert ordinary.post(ROLES, json={"name": "Своя роль", "permissions": []}).status_code == 403
    assert ordinary.get(ROLES).status_code == 403

    # И права на самом деле не изменились.
    codes = set(root_client.get(f"{ROLES}/{role['id']}").json()["permissions"])
    assert codes == {"clients.view", "deals.view"}


def test_someone_who_manages_roles_still_cannot_promote_themselves(
    role_maker, staff_maker, root_client
):
    """Запрет на себя — то единственное, что отделяет «управляет правами» от
    «имеет все права»."""
    role = role_maker("Кадровик", ["roles.view", "roles.manage", "staff.view", "clients.view"])
    hr = staff_maker("hr@test.local", role["id"])
    hr_id = _user_id(root_client, "hr@test.local")

    everything = role_maker("Всё сразу", list(permissions.all_codes()))
    denied = hr.post(f"{ROLES}/assign/{hr_id}", json={"role_id": everything["id"]})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "cannot_change_own_role"

    # Снять с себя ограничение через «убрать роль» тоже нельзя.
    dropped = hr.post(f"{ROLES}/assign/{hr_id}", json={"role_id": None})
    assert dropped.status_code == 403
    assert dropped.json()["error"]["code"] == "cannot_change_own_role"

    # Он по-прежнему не может того, чего не мог: право у него на роли, а не на склад.
    assert hr.get(f"{API}/warehouse/products").status_code == 403


def test_root_never_gets_a_role_and_never_loses_anything(root_client, role_maker):
    """Root неотключаем: нельзя собрать конфигурацию, в которой права раздать
    некому."""
    empty = role_maker("Пустая", [])
    root_id = _user_id(root_client, "root@test.local")

    denied = root_client.post(f"{ROLES}/assign/{root_id}", json={"role_id": empty["id"]})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "cannot_assign_role_to_root"

    me = root_client.get(f"{API}/auth/me").json()
    assert me["role"] == "root"
    assert me["role_id"] is None
    assert set(me["permissions"]) == set(permissions.all_codes())


# --- последний, кто раздаёт права ---


def test_the_last_role_that_manages_permissions_cannot_be_stripped(
    root_client, role_maker, staff_maker
):
    """Снять роль с единственного, кто может управлять правами, нельзя.

    Root при этом остаётся, но опираться на него нельзя: «владелец завёл
    гендиректора и забыл пароль root» — не выдуманный сценарий.
    """
    role = role_maker("Единственный кадровик", ["roles.view", "roles.manage"])
    staff_maker("onlyhr@test.local", role["id"])

    denied = root_client.patch(f"{ROLES}/{role['id']}", json={"permissions": ["roles.view"]})
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "last_roles_manager"

    # Право осталось на месте, а не потерялось по дороге.
    assert "roles.manage" in root_client.get(f"{ROLES}/{role['id']}").json()["permissions"]


def test_the_last_manager_of_permissions_cannot_be_reassigned(
    root_client, role_maker, staff_maker
):
    role = role_maker("Кадровик номер два", ["roles.view", "roles.manage"])
    plain = role_maker("Совсем обычный", ["clients.view"])
    staff_maker("onlyhr2@test.local", role["id"])
    user_id = _user_id(root_client, "onlyhr2@test.local")

    denied = root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": plain["id"]})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "last_roles_manager"


def test_with_two_managers_of_permissions_one_may_go(root_client, role_maker, staff_maker):
    """Запрет держится на «последнем», а не на «любом»: иначе управление
    правами нельзя было бы передать вообще никогда."""
    role = role_maker("Кадровик из двух", ["roles.view", "roles.manage"])
    plain = role_maker("Обычный из двух", ["clients.view"])
    staff_maker("hr_a@test.local", role["id"])
    staff_maker("hr_b@test.local", role["id"])

    first = _user_id(root_client, "hr_a@test.local")
    moved = root_client.post(f"{ROLES}/assign/{first}", json={"role_id": plain["id"]})
    assert moved.status_code == 200, moved.text


# --- права на лету ---


def test_permissions_are_checked_on_every_request_not_read_once_at_login(
    root_client, role_maker, staff_maker
):
    """У человека открыта вкладка со вчерашними правами.

    Сессию не трогаем нарочно: смысл проверки в том, что доступ прекращается
    без перелогина. Если бы права читались при входе, старая cookie продолжала
    бы работать до истечения срока — четырнадцать дней.
    """
    role = role_maker("Пока можно", ["clients.view", "clients.create"])
    worker = staff_maker("onthefly@test.local", role["id"])

    assert worker.get(f"{API}/clients").status_code == 200

    assert root_client.patch(
        f"{ROLES}/{role['id']}", json={"permissions": ["clients.view"]}
    ).status_code == 200

    # Та же сессия, тот же клиент — и уже отказ.
    denied = worker.post(f"{API}/clients", json={"name": "Уже нельзя"})
    assert denied.status_code == 403, denied.text
    assert "clients.create" in denied.json()["error"]["message"]
    assert worker.get(f"{API}/clients").status_code == 200

    # И /auth/me отдаёт новый набор, а не запомненный при входе.
    assert "clients.create" not in worker.get(f"{API}/auth/me").json()["permissions"]


# --- пресеты ---


@pytest.mark.parametrize("preset", sorted(permissions_service.PRESETS))
def test_a_role_from_a_preset_grants_exactly_what_it_promises(preset, root_client):
    """Пресет — готовое начало, а не сюрприз: что обещано, то и выдано."""
    created = root_client.post(
        f"{ROLES}/from-preset", json={"preset": preset, "name": f"Из набора {preset}"}
    )
    assert created.status_code == 201, created.text
    role = created.json()
    try:
        assert set(role["permissions"]) == set(permissions_service.PRESETS[preset]["permissions"])
        assert role["preset"] == preset
    finally:
        root_client.delete(f"{ROLES}/{role['id']}")


def test_a_preset_role_is_editable_afterwards(root_client):
    """«Готовое начало вместо пустого конструктора, но переделать можно всё»."""
    role = root_client.post(
        f"{ROLES}/from-preset", json={"preset": "accountant", "name": "Свой бухгалтер"}
    ).json()
    try:
        changed = root_client.patch(
            f"{ROLES}/{role['id']}",
            json={"name": "Главбух", "permissions": ["documents.view", "documents.issue"]},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["name"] == "Главбух"
        assert set(changed.json()["permissions"]) == {"documents.view", "documents.issue"}
    finally:
        root_client.delete(f"{ROLES}/{role['id']}")


def test_the_accountant_preset_sees_money_and_not_boards(root_client, staff_maker):
    """Пресет проверяется поведением, а не списком: список — это то же самое
    утверждение, переписанное из исходника."""
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200
    role = root_client.post(
        f"{ROLES}/from-preset", json={"preset": "accountant", "name": "Бухгалтер поведения"}
    ).json()
    try:
        accountant = staff_maker("accountant@test.local", role["id"])
        assert accountant.get(f"{API}/documents").status_code == 200
        assert accountant.get(f"{API}/companies").status_code == 200
        assert accountant.get(f"{API}/reports/revenue").status_code == 200

        denied = accountant.get(f"{API}/boards")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "permission_denied"
    finally:
        root_client.delete(f"{ROLES}/{role['id']}")


def test_unknown_permissions_are_refused_not_silently_dropped(root_client):
    """Молча отбросить неизвестное значит показать успех там, где ничего не
    выдано."""
    denied = root_client.post(
        ROLES, json={"name": "С опечаткой", "permissions": ["deals.telepathy"]}
    )
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "unknown_permission"
    assert "deals.telepathy" in denied.json()["error"]["message"]


def test_unknown_preset_is_refused(root_client):
    denied = root_client.post(f"{ROLES}/from-preset", json={"preset": "телепат"})
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "unknown_preset"


# --- справочник ролей ---


def test_a_role_in_use_cannot_be_deleted(root_client, role_maker, staff_maker):
    """SET NULL оставил бы людей без прав молча: сотрудник приходит утром и
    обнаруживает пустую CRM, а причина — вчерашняя уборка в справочнике."""
    role = role_maker("Занятая", ["clients.view"])
    staff_maker("busy@test.local", role["id"])

    denied = root_client.delete(f"{ROLES}/{role['id']}")
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "role_in_use"


def test_the_default_role_cannot_be_deleted(root_client):
    """Без роли по умолчанию зарегистрировавшийся сотрудник входил бы в CRM
    без единого раздела."""
    default = next(r for r in root_client.get(ROLES).json()["items"] if r["is_default"])
    denied = root_client.delete(f"{ROLES}/{default['id']}")
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "role_is_default"


def test_role_names_do_not_repeat(root_client, role_maker):
    role_maker("Единственная в своём роде", ["clients.view"])
    clash = root_client.post(
        ROLES, json={"name": "Единственная в своём роде", "permissions": []}
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "role_name_taken"


# --- совместимость ---


def test_existing_managers_keep_working_exactly_as_before(manager_client):
    """Накат миграции не должен отнять доступ.

    Проверяем поведением, а не списком прав: список в тесте — это тот же список
    из миграции, переписанный второй раз, и совпадать он будет всегда, даже если
    оба неверны. Здесь перечислено то, что менеджер делал до появления ролей.
    """
    allowed = (
        f"{API}/clients",
        f"{API}/deals",
        f"{API}/deals/board",
        f"{API}/boards",
        f"{API}/tasks",
        f"{API}/documents",
        f"{API}/companies",
        f"{API}/pipeline/stages",
        f"{API}/dashboard",
        f"{API}/modules",
    )
    for path in allowed:
        response = manager_client.get(path)
        assert response.status_code == 200, f"{path} закрылся для менеджера: {response.text}"

    created = manager_client.post(f"{API}/clients", json={"name": "Всё как раньше"})
    assert created.status_code == 201, created.text
    deal = manager_client.post(
        f"{API}/deals", json={"title": "Работа как раньше", "client_id": created.json()["id"]}
    )
    assert deal.status_code == 201, deal.text
    assert manager_client.patch(
        f"{API}/deals/{deal.json()['id']}", json={"amount": 12345}
    ).status_code == 200
    # Суммы менеджер видел и продолжает видеть.
    assert manager_client.get(f"{API}/deals/{deal.json()['id']}").json()["amount"] == 12345


def test_existing_managers_still_cannot_do_what_they_never_could(manager_client):
    """Совместимость в обе стороны: доступ не отняли, но и не выдали лишнего.

    Молча расширить права под видом переезда так же плохо, как молча сузить:
    заметят это ещё позже.
    """
    for response in (
        manager_client.get(f"{STAFF}"),
        manager_client.get(f"{API}/settings"),
        manager_client.get(ROLES),
        manager_client.post(f"{API}/modules/documents", json={"enabled": False}),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "permission_denied"


def test_root_stays_root_after_the_migration(root_client):
    me = root_client.get(f"{API}/auth/me").json()
    assert me["role"] == "root"
    assert me["role_id"] is None
    assert root_client.get(ROLES).status_code == 200
    assert root_client.get(f"{API}/settings").status_code == 200


def test_a_new_employee_gets_the_default_role(root_client):
    """Регистрация без роли означала бы вход в пустую CRM."""
    anon = TestClient(app)
    response = register(anon, "Новичок", "fresh@test.local")
    assert response.status_code == 201, response.text
    user_id = response.json()["user"]["id"]
    try:
        assert root_client.post(f"{STAFF}/{user_id}/approve").status_code == 200
        fresh = TestClient(app)
        assert login(fresh, "fresh@test.local", "manager-pass-123").status_code == 200
        assert fresh.get(f"{API}/clients").status_code == 200
        assert fresh.get(f"{API}/auth/me").json()["role_name"]
    finally:
        root_client.delete(f"{STAFF}/{user_id}")


# --- то, что нашлось перебором по живому стенду ---
#
# Каждая проверка ниже написана на дыру, которая работала: сначала запрос,
# который проходил, и только потом правка. Порядок именно этот — тест,
# написанный после починки «по памяти», проверяет починку, а не дыру.


def test_editing_your_own_role_cannot_add_permissions(root_client, role_maker, staff_maker):
    """Своей должности новых прав не выписывают.

    Запрет «нельзя назначить роль себе» обходился одним запросом с другой
    стороны: не менять роль, а дописать права в ту, которая уже своя.
    Сотрудник с одним лишь `roles.manage` отправлял `PATCH /roles/{своя}` со
    всем реестром и получал журнал, настройки, сотрудников и суммы.
    """
    role = role_maker("Только доступы", ["roles.view", "roles.manage"])
    gate = staff_maker("gate@test.local", role["id"])

    assert gate.get(f"{API}/audit").status_code == 403
    assert gate.get(f"{API}/clients").status_code == 403

    denied = gate.patch(
        f"{ROLES}/{role['id']}", json={"permissions": list(permissions.all_codes())}
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "cannot_grant_to_own_role"
    # Отказ называет, чего именно нельзя выдать: иначе настройка доступов
    # превращается в гадание — ровно как у остальных отказов здесь.
    assert "audit.view" in denied.json()["error"]["message"]

    # Права не изменились ни на строку, и закрытые разделы остались закрытыми.
    assert set(root_client.get(f"{ROLES}/{role['id']}").json()["permissions"]) == {
        "roles.view",
        "roles.manage",
    }
    assert gate.get(f"{API}/audit").status_code == 403
    assert gate.get(f"{API}/clients").status_code == 403


def test_your_own_role_may_still_be_renamed_and_narrowed(role_maker, staff_maker):
    """Обратная сторона: запрет на самоповышение не запрещает уборку.

    Без этой половины предыдущий тест проходил бы и на правке, которая просто
    запретила трогать свою роль вовсе, — а тогда отдать лишний доступ стало бы
    нельзя без второго человека.
    """
    role = role_maker(
        "Уборка у себя", ["roles.view", "roles.manage", "clients.view", "clients.create"]
    )
    # Второй управляющий: без него уборка упрётся в «последнего», а проверяем
    # здесь не его.
    spare = role_maker("Запасной управляющий", ["roles.view", "roles.manage"])
    staff_maker("spare@test.local", spare["id"])
    owner = staff_maker("tidy@test.local", role["id"])

    narrowed = owner.patch(
        f"{ROLES}/{role['id']}",
        json={
            "name": "Уборка у себя, новое имя",
            "permissions": ["roles.view", "roles.manage", "clients.view"],
        },
    )
    assert narrowed.status_code == 200, narrowed.text
    assert set(narrowed.json()["permissions"]) == {"roles.view", "roles.manage", "clients.view"}
    # Убранное право перестало работать сразу, той же сессией.
    assert owner.post(f"{API}/clients", json={"name": "Уже нельзя"}).status_code == 403


def test_disabling_the_last_permissions_manager_still_works_and_that_is_the_open_question(
    root_client, role_maker, staff_maker
):
    """Здесь зафиксировано НЕ желаемое поведение, а известное расхождение.

    Инвариант «раздавать права всегда есть кому» стоит на четырёх путях из
    шести: снять `roles.manage` с последней роли нельзя, перевести её
    единственного носителя на другую должность нельзя, — а отключить или
    удалить его самого можно, и система остаётся без управляющего доступами.

    Просто дописать проверку в `auth_service.disable` нельзя: инвариант не
    считает root'а нарочно, и тогда назначение первого «гендиректора» стало бы
    необратимым — снять право уже нельзя, перевести нельзя, уволить нельзя, а
    «только root и никаких управляющих» есть законное состояние, с которого
    начинается любая установка. Развязка — решение о модели прав (считать ли
    root'а при снятии человека, или заводить явную передачу полномочий), и
    принимать его правкой на месте не следует.

    Тест стоит здесь, чтобы это расхождение не закрылось и не разъехалось
    молча: поменяется поведение — придётся прочитать этот текст.
    """
    role = role_maker("Единственный управляющий", ["roles.view", "roles.manage"])
    staff_maker("lastone@test.local", role["id"])
    user_id = _user_id(root_client, "lastone@test.local")

    # Через роль — закрыто, и это работает.
    denied = root_client.patch(f"{ROLES}/{role['id']}", json={"permissions": ["roles.view"]})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "last_roles_manager"

    # Через человека — открыто. Ровно то же итоговое состояние.
    assert root_client.post(f"{STAFF}/{user_id}/disable").status_code == 200
    assert root_client.post(f"{STAFF}/{user_id}/enable").status_code == 200


def test_the_sources_report_hides_revenue_in_every_row_not_just_the_total(
    root_client, role_maker, staff_maker, manager_client
):
    """Выгрузка обязана совпадать с экраном, а экран — с правом.

    Сужение писалось по ключу `rows`, которого в ответе нет: отчёт отдаёт
    `items`. Зануляться успевал только итог — «выручка —» в шапке и настоящие
    деньги в каждой строке и в CSV. Самый незаметный вид отказа: выглядит
    работающим.
    """
    assert root_client.post(f"{API}/modules/reports", json={"enabled": True}).status_code == 200
    client = manager_client.post(
        f"{API}/clients", json={"name": "Источник денег", "source": "ads"}
    ).json()
    deal = manager_client.post(
        f"{API}/deals",
        json={"title": "Оплаченная", "client_id": client["id"], "amount": 1234500},
    ).json()
    stages = manager_client.get(f"{API}/pipeline/stages").json()["items"]
    won = next(s["key"] for s in stages if s["kind"] == "won")
    assert (
        manager_client.post(f"{API}/deals/{deal['id']}/move", json={"stage": won}).status_code
        == 200
    )

    role = role_maker("Источники без денег", ["reports.view"])
    poor = staff_maker("sources@test.local", role["id"])

    body = poor.get(f"{API}/reports/sources").json()
    assert body["revenue_total"] is None
    assert body["items"], "строк нет — проверять нечего"
    assert all(row["revenue"] is None for row in body["items"]), body["items"]
    # Ключа-призрака, по которому писалось сужение, в ответе быть не должно:
    # пустой `rows` рядом с наполненным `items` — след ровно той ошибки.
    assert "rows" not in body

    export = poor.get(f"{API}/reports/sources.csv")
    assert export.status_code == 200
    assert "12345" not in export.text, export.text
    # А у того, кому положено, деньги в выгрузке есть — иначе тест проходил бы
    # и на сломанном отчёте, который не отдаёт их никому.
    assert "12345" in root_client.get(f"{API}/reports/sources.csv").text


def test_the_warehouse_card_hides_prices_in_write_responses_too(
    root_client, role_maker, staff_maker
):
    """Ответ пишущей ручки — такая же карточка товара, как у GET.

    `amounts` в сериализаторе по умолчанию `True`, и PATCH становился обходом:
    кладовщик без `warehouse.view_amounts` не видел закупочную цену ни в
    списке, ни в карточке, но получал её в ответ на переименование товара.
    """
    assert root_client.post(f"{API}/modules/warehouse", json={"enabled": True}).status_code == 200
    product = root_client.post(
        f"{API}/warehouse/products",
        json={"name": "Матрица без цен", "unit": "pcs", "price": 677700, "cost": 250000},
    )
    assert product.status_code == 201, product.text
    product_id = product.json()["id"]

    role = role_maker(
        "Склад без сумм",
        [
            "warehouse.view",
            "warehouse.create",
            "warehouse.edit",
            "warehouse.delete",
            "warehouse.restore",
        ],
    )
    keeper = staff_maker("keeper@test.local", role["id"])
    try:
        # Чтение уже было закрыто — с него и сверяемся.
        assert keeper.get(f"{API}/warehouse/products/{product_id}").json()["cost"] is None

        patched = keeper.patch(
            f"{API}/warehouse/products/{product_id}", json={"note": "только заметка"}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["price"] is None, patched.text
        assert patched.json()["cost"] is None, patched.text

        assert keeper.delete(f"{API}/warehouse/products/{product_id}").status_code == 200
        restored = keeper.post(f"{API}/warehouse/products/{product_id}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["price"] is None
        assert restored.json()["cost"] is None
        # Остаток при этом на месте: закрыты деньги, а не склад.
        assert restored.json()["stock_milli"] is not None
    finally:
        root_client.delete(f"{API}/warehouse/products/{product_id}")


def test_naming_a_sum_needs_the_same_right_as_seeing_one(
    role_maker, staff_maker, manager_client
):
    """Проверка стояла только на правке, и запрет получался половинчатым.

    Сумму нельзя было переписать, но можно было завести заявку сразу с ней:
    число уходило в выручку и в отчёты, а тот, кто его вписал, не видел его
    больше никогда — даже чтобы исправить опечатку.
    """
    client = manager_client.post(f"{API}/clients", json={"name": "Заказчик без сумм"}).json()
    role = role_maker(
        "Заявки без сумм", ["clients.view", "deals.view", "deals.create", "deals.edit"]
    )
    poor = staff_maker("nosum@test.local", role["id"])

    denied = poor.post(
        f"{API}/deals", json={"title": "С суммой", "client_id": client["id"], "amount": 999999}
    )
    assert denied.status_code == 403, denied.text
    assert "deals.view_amounts" in denied.json()["error"]["message"]

    denied_prepaid = poor.post(
        f"{API}/deals", json={"title": "С предоплатой", "client_id": client["id"], "prepaid": 1}
    )
    assert denied_prepaid.status_code == 403, denied_prepaid.text

    # Заявка без денег заводится как заводилась: сузилось поле, а не раздел.
    created = poor.post(f"{API}/deals", json={"title": "Без суммы", "client_id": client["id"]})
    assert created.status_code == 201, created.text
    assert created.json()["amount"] is None


def test_own_deals_only_narrows_the_summary_and_the_reports(
    root_client, role_maker, staff_maker
):
    """`deals.view_others` решает, ЧЬИ заявки считаются, — везде, а не в списке.

    Сужать список карточек и оставлять их сумму — это не половина запрета, а
    его отсутствие: узнать оборот фирмы и было целью. Найдено живым прогоном
    уже ПОСЛЕ того, как роли были признаны готовыми: список и канбан сужались
    верно, а сводка и отчёт отдавали общие числа.
    """
    role = role_maker(
        "Только свои, но с деньгами",
        [
            "clients.view", "deals.view", "deals.create", "deals.edit",
            "deals.move_stage", "deals.view_amounts",
            "reports.view", "reports.view_amounts",
        ],
    )
    staff = staff_maker("own-only@test.local", role["id"])

    client = root_client.post(f"{API}/clients", json={"name": "Общий клиент"}).json()
    # чужая заявка: ответственный — root, и она закрыта выигрышем
    alien = root_client.post(
        f"{API}/deals", json={"title": "Чужая", "client_id": client["id"], "amount": 1_000_00}
    ).json()
    won = next(
        c for c in root_client.get(f"{API}/deals/board").json()["columns"] if c["kind"] == "won"
    )["key"]
    root_client.post(f"{API}/deals/{alien['id']}/move", json={"stage": won})

    staff.post(f"{API}/deals", json={"title": "Моя", "client_id": client["id"], "amount": 100_00})

    board = staff.get(f"{API}/deals/board").json()
    dashboard = staff.get(f"{API}/dashboard").json()
    revenue = staff.get(f"{API}/reports/revenue", params={"tz_offset": 0}).json()

    assert len(staff.get(f"{API}/deals").json()["items"]) == 1, "в списке чужая заявка"
    assert sum(c.get("amount_total") or 0 for c in board["columns"]) == 100_00
    assert (dashboard["money_won_this_month"] or 0) == 0, "сводка отдала чужую выручку"
    assert (revenue["won_amount"] or 0) == 0, "отчёт отдал чужую выручку"
    # своя заявка при этом видна и посчитана
    assert dashboard["money_in_work"] == 100_00
