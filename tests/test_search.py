"""Общий поиск командной палитры (Ctrl+K).

У поиска нет своего экрана и своих настроек, поэтому его легко считать мелочью.
На деле это единственное место, где одним запросом просматривается **сразу
несколько разделов**, — и потому единственное место, где обход блока или права
не виден никому: раздел закрыт, пункт из меню убран, а название записи из него
по-прежнему приезжает в выдачу.

Отсюда и состав проверок. Что заявки вообще находятся (без них палитра отвечала
на вопрос «где мой заказ» молчанием). Что строка поиска — это строка, а не
шаблон LIKE. Что номер страницы снаружи не приходит. И что каждая группа
пустеет по обеим причинам сразу: выключенный блок и отсутствующее право.

База у тестов общая и переживает файл, поэтому роли и сотрудники убираются за
собой, а свои записи ищутся по приметной приставке, а не по количеству строк.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from core.services import modules_service
from database.repositories import deals as deals_repo
from tests.conftest import API, make_manager

SEARCH = f"{API}/search"
ROLES = f"{API}/roles"
STAFF = f"{API}/staff"

EMPTY = {"items": [], "total": 0}


# --- вспомогательное ---------------------------------------------------------


@pytest.fixture
def role_maker(root_client):
    """Роли с уборкой: тело теста может и не дойти до конца, а имя роли занято."""
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
        people = root_client.get(STAFF).json()["items"]
        user_id = next(u["id"] for u in people if u["email"] == email)
        created.append(user_id)
        assigned = root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role_id})
        assert assigned.status_code == 200, assigned.text
        return client

    yield make

    for user_id in created:
        root_client.delete(f"{STAFF}/{user_id}")


def _client_id(client: TestClient, name: str) -> int:
    response = client.post(f"{API}/clients", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _deal(client: TestClient, **fields) -> dict:
    response = client.post(f"{API}/deals", json=fields)
    assert response.status_code == 201, response.text
    return response.json()


def _titles(found: dict, group: str = "deals") -> list[str]:
    return [item.get("title") or item.get("name") for item in found[group]["items"]]


# --- заявки в выдаче ---------------------------------------------------------


def test_zayavka_nakhoditsya_poiskom(manager_client):
    """Ради чего всё: палитра искала по клиентам и доскам, а заявок не знала.

    Заявка — стержень системы, и «где мой заказ» — первый вопрос, с которым
    жмут Ctrl+K. Ответом было молчание, а карточка при этом лежала в базе.
    """
    client_id = _client_id(manager_client, "Нхдм Заказчик")
    deal = _deal(
        manager_client,
        title="Нхдм Ремонт витрины",
        client_id=client_id,
        description="Нхдмопсн замена стекла",
    )

    by_title = manager_client.get(SEARCH, params={"q": "Нхдм Ремонт"}).json()
    assert [item["id"] for item in by_title["deals"]["items"]] == [deal["id"]]

    card = by_title["deals"]["items"][0]
    assert card["title"] == "Нхдм Ремонт витрины"
    # Имя клиента — подпись строки: две «Доставки» разных заказчиков иначе не
    # различить, и это тот же довод, по которому оно есть у доски.
    assert card["client_name"] == "Нхдм Заказчик"

    by_description = manager_client.get(SEARCH, params={"q": "Нхдмопсн"}).json()
    assert [item["id"] for item in by_description["deals"]["items"]] == [deal["id"]]

    # «Что там по Ромашке» — в жизни спрашивают именем клиента, а не названием
    # работы; репозиторий это умеет, и палитра обязана уметь тоже.
    by_client = manager_client.get(SEARCH, params={"q": "Нхдм Заказчик"}).json()
    assert deal["id"] in {item["id"] for item in by_client["deals"]["items"]}


def test_pustoy_zapros_pokazyvaet_i_nedavnie_zayavki(manager_client):
    """Палитра открывается не пустой — иначе первое нажатие Ctrl+K ничего не даёт."""
    client_id = _client_id(manager_client, "Ндвн Заказчик")
    deal = _deal(manager_client, title="Ндвн Свежая работа", client_id=client_id)

    recent = manager_client.get(SEARCH).json()
    assert recent["clients"]["items"], "недавние клиенты пропали из пустого запроса"
    assert deal["id"] in {item["id"] for item in recent["deals"]["items"]}


def test_forma_otveta_odna_pri_lyubom_naboru_grupp(root_client, manager_client):
    """Группа приходит пустой, а не исчезает.

    Клиенту API иначе пришлось бы знать, какие ключи сегодня бывают, — а
    зависит это от блоков и прав, то есть от чужих настроек.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200

    found = manager_client.get(SEARCH, params={"q": "щщщнеттакого"}).json()
    assert set(found) == {"query", "clients", "deals", "boards"}
    for group in ("clients", "deals", "boards"):
        assert found[group] == EMPTY, group


# --- строка поиска — это строка, а не шаблон ---------------------------------


def test_podchyorkivanie_v_zaprose_ne_zamenyaet_lyuboy_simvol(manager_client):
    """`_` в LIKE — «любой один символ», и в строку поиска он попадает как есть.

    Общий слой это уже чинит (`database/query.contains`), но проверялось это
    только на клиентах и только мимо HTTP. Поиск же ходит в три репозитория
    сразу, и достаточно одному из них собрать условие своими руками, чтобы
    беда вернулась ровно в том месте, где её никто не ищет.
    """
    client_id = _client_id(manager_client, "Пдчрк Заказчик")
    exact = _deal(manager_client, title="Пдчрк_один", client_id=client_id)
    _deal(manager_client, title="ПдчркХодин", client_id=client_id)

    found = manager_client.get(SEARCH, params={"q": "Пдчрк_один"}).json()
    assert [item["id"] for item in found["deals"]["items"]] == [exact["id"]]
    assert found["deals"]["total"] == 1


def test_protsent_v_zaprose_ishchet_protsent_a_ne_vsyo(manager_client):
    """Один знак `%` возвращал всю таблицу — то есть выгрузку всего по нажатию.

    В палитре это заметно ещё меньше, чем в списке: строк на экране всё равно
    шесть, а «найдено 1240» читается как «поиск работает».
    """
    client_id = _client_id(manager_client, "Прцнт Заказчик")
    discount = _deal(manager_client, title="Прцнт Скидка 100% для своих", client_id=client_id)
    _deal(manager_client, title="Прцнт Обычная работа", client_id=client_id)

    everything = manager_client.get(SEARCH).json()
    assert everything["deals"]["total"] >= 2, "заявок в базе мало — проверка ничего не поймает"

    pattern = manager_client.get(SEARCH, params={"q": "%"}).json()
    assert pattern["deals"]["total"] < everything["deals"]["total"], (
        "поиск по одному «%» вернул все заявки"
    )
    assert pattern["clients"]["total"] < everything["clients"]["total"], (
        "поиск по одному «%» вернул всех клиентов"
    )

    literal = manager_client.get(SEARCH, params={"q": "100%"}).json()
    assert [item["id"] for item in literal["deals"]["items"]] == [discount["id"]]


def test_nomer_stranitsy_snaruzhi_ne_prikhodit(manager_client, db):
    """Отрицательного смещения здесь неоткуда взяться — и это проверяется.

    `page = 0` даёт `OFFSET -50`: SQLite молчит и отдаёт первую страницу, MySQL
    отвечает синтаксической ошибкой, то есть пятисоткой. У палитры номера
    страницы нет вовсе — она показывает первые несколько строк, а листают на
    своих экранах. Несуществующий параметр сторожить не надо, но проверить, что
    его действительно нет, надо: стоит однажды его завести «для удобства», и
    сторожить придётся заново.
    """
    from web.api.routes import search as search_route

    assert "page" not in inspect.signature(search_route.global_search).parameters
    assert "per_page" not in inspect.signature(search_route.global_search).parameters

    # Лишний параметр в адресе ничего не меняет: FastAPI его не читает.
    with_page = manager_client.get(SEARCH, params={"q": "Прцнт", "page": 0})
    assert with_page.status_code == 200, with_page.text

    # А репозиторий, в который ходит поиск, нулевую страницу считает первой —
    # общим слоем `page_of`, а не своей арифметикой.
    rows, total = deals_repo.search(db, q="Прцнт", page=0, per_page=1)
    assert total >= 1 and len(rows) == 1


# --- выдача уважает права ----------------------------------------------------


def test_bez_prava_gruppa_ostayotsya_pustoy(role_maker, staff_maker, manager_client):
    """Поиск иначе становится обходом доступов.

    Через него видно название и клиента заявки из раздела, который сотруднику
    закрыт, — то есть ровно то, что раздел и закрывает.
    """
    client_id = _client_id(manager_client, "Бзпрв Заказчик")
    _deal(manager_client, title="Бзпрв Секретная работа", client_id=client_id)

    role = role_maker("Только задачи", ["tasks.view"])
    blind = staff_maker("nodeals@test.local", role["id"])

    found = blind.get(SEARCH, params={"q": "Бзпрв"})
    assert found.status_code == 200, found.text
    assert found.json()["deals"] == EMPTY
    assert found.json()["clients"] == EMPTY


def test_bez_prava_na_chuzhie_nakhodyatsya_tolko_svoi(role_maker, staff_maker, manager_client):
    """«Вижу только свои заявки» обязано держаться и в поиске.

    Это самое удобное место обойти ограничение: список показывает три карточки,
    а Ctrl+K по тому же слову находил бы все тридцать — вместе с именами чужих
    клиентов. Сужение приходит из прав, а не из запроса, поэтому снять его
    нечем.
    """
    client_id = _client_id(manager_client, "Сржм Заказчик")
    foreign = _deal(manager_client, title="Сржм Чужая работа", client_id=client_id)

    role = role_maker("Только свои заявки", ["deals.view", "deals.create"])
    narrow = staff_maker("ownonly@test.local", role["id"])
    mine = _deal(narrow, title="Сржм Своя работа", client_id=client_id)

    found = narrow.get(SEARCH, params={"q": "Сржм"}).json()
    ids = {item["id"] for item in found["deals"]["items"]}
    assert mine["id"] in ids, "сотрудник не находит собственную заявку"
    assert foreign["id"] not in ids, "поиск показал чужую заявку в обход deals.view_others"
    assert found["deals"]["total"] == 1, "счётчик выдал число, которого не видно в списке"

    # У того, чьё право шире, поиск работает как работал.
    seen = manager_client.get(SEARCH, params={"q": "Сржм"}).json()
    assert {foreign["id"], mine["id"]} <= {item["id"] for item in seen["deals"]["items"]}


def test_summa_ne_priezzhaet_bez_prava_na_summy(role_maker, staff_maker, manager_client):
    """Иначе цену работы можно прочесть из выдачи, не открывая заявку.

    Ключи при этом на месте и приходят пустыми: форма ответа не зависит от
    того, кто спрашивает, — то же правило, что у карточки заявки.
    """
    client_id = _client_id(manager_client, "Смма Заказчик")
    _deal(manager_client, title="Смма Дорогая работа", client_id=client_id, amount=500000)

    role = role_maker("Заявки без денег", ["deals.view", "deals.view_others"])
    poor = staff_maker("noamounts@test.local", role["id"])

    card = poor.get(SEARCH, params={"q": "Смма Дорогая"}).json()["deals"]["items"][0]
    assert card["title"] == "Смма Дорогая работа"
    assert card["amount"] is None, "сумма видна тому, у кого нет deals.view_amounts"

    rich = manager_client.get(SEARCH, params={"q": "Смма Дорогая"}).json()["deals"]["items"][0]
    assert rich["amount"] == 500000, "поиск перестал показывать сумму тому, кому положено"


# --- выключенный блок исчезает целиком ---------------------------------------


def test_vyklyuchennyy_blok_ukhodit_iz_vydachi(monkeypatch, manager_client):
    """Выключенный блок обязан пропадать и из поиска, а не только из меню.

    Проверяется подменой состояния, а не выключателем: заявки — несущий блок,
    выключить его нельзя (`module_is_core`). Проверять при этом нужно всё
    равно — гейт в поиске общий для всех групп, и заявки ходят через него
    наравне с досками. Сломайся он, у досок это заметил бы `test_modules.py`,
    но сломаться он может и после того, как заявки станут единственной группой,
    которую он пропускает.
    """
    client_id = _client_id(manager_client, "Вклбл Заказчик")
    _deal(manager_client, title="Вклбл Работа", client_id=client_id)
    assert manager_client.get(SEARCH, params={"q": "Вклбл"}).json()["deals"]["items"]

    # Подмену ставим после того, как запись заведена: при выключенном блоке её
    # не завести — заявки закрыты тем же гейтом, что и всё остальное.
    real = modules_service.is_enabled
    monkeypatch.setattr(
        modules_service,
        "is_enabled",
        lambda db, key: False if key == "deals" else real(db, key),
    )

    found = manager_client.get(SEARCH, params={"q": "Вклбл"})
    assert found.status_code == 200, found.text
    assert found.json()["deals"] == EMPTY
    assert found.json()["clients"]["items"], "клиенты — несущий блок, они обязаны находиться"


def test_gruppa_zakryta_blokom_ranshe_chem_pravom(root_client, manager_client):
    """Порядок проверок тот же, что везде: сначала блок, потом право.

    Досками это видно без подмен: блок выключается по-настоящему, а право на
    них у менеджера есть. Выключенный блок обязан забрать группу и у него.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200
    client_id = _client_id(manager_client, "Прдк Заказчик")
    created = manager_client.post(
        f"{API}/boards", json={"title": "Прдк Доска", "client_id": client_id}
    )
    assert created.status_code == 201, created.text
    assert manager_client.get(SEARCH, params={"q": "Прдк"}).json()["boards"]["items"]

    try:
        assert root_client.post(f"{API}/modules/boards", json={"enabled": False}).status_code == 200
        found = manager_client.get(SEARCH, params={"q": "Прдк"}).json()
        assert found["boards"] == EMPTY
        # Соседние группы при этом не задеты: блок выключается без вреда для
        # остальных, и поиск — не исключение.
        assert found["clients"]["items"]
    finally:
        root_client.post(f"{API}/modules/boards", json={"enabled": True})
