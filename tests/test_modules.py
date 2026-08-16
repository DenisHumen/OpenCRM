"""Блоки системы: включение, выключение и главное — независимость друг от друга.

Смысл модульности в том, что дизайн-студия выключает склад, а мастерская —
доски, и обе продолжают работать. Поэтому проверяем не «переключается ли
флажок», а то, ради чего он существует: что выключенный блок исчезает целиком
(включая прямые адреса и публичные ссылки), что при этом остальные разделы
работают, и что данные выключенного блока никуда не деваются.
"""

import pytest
from fastapi.testclient import TestClient

from core import modules
from core.services import mail_service, modules_service
from core.utils import now_utc
from tests.conftest import API
from web.main import app

MODULES = f"{API}/modules"

SWITCHABLE = [m.key for m in modules.MODULES if not m.core and m.ready]


@pytest.fixture(autouse=True)
def restore_modules(root_client):
    """Состояние блоков глобальное и переживает тест, а база у тестов общая.

    Без восстановления один упавший тест оставил бы бланки выключенными, и
    посыпались бы совершенно посторонние файлы. Восстанавливаем в фикстуре, а не
    в конце теста: тело до конца может и не дойти.
    """
    yield
    # Кэш мог остаться от подменённого реестра — сбрасываем до восстановления.
    modules_service.invalidate()
    for module in modules.MODULES:
        if not module.core and module.ready:
            root_client.post(f"{MODULES}/{module.key}", json={"enabled": module.default})


def switch(client, key: str, enabled: bool):
    return client.post(f"{MODULES}/{key}", json={"enabled": enabled})


def states(client) -> dict[str, bool]:
    return {m["key"]: m["enabled"] for m in client.get(MODULES).json()["items"]}


def test_every_module_from_the_registry_is_reported(manager_client):
    """Список блоков задаёт код, а не база: иначе новый блок не появился бы у
    того, кто обновился, а снесённый остался бы висеть."""
    listed = states(manager_client)
    assert set(listed) == set(modules.KEYS)


def test_core_modules_cannot_be_switched_off(root_client):
    """Клиенты и заявки — то, на чём держится всё остальное."""
    for key in (m.key for m in modules.MODULES if m.core):
        response = switch(root_client, key, False)
        assert response.status_code == 422, key
        assert response.json()["error"]["code"] == "module_is_core"
        assert states(root_client)[key] is True


def test_unbuilt_modules_cannot_be_switched_on(root_client, monkeypatch):
    """Переключатель для того, чего нет, — обещание, а не функция.

    Раньше тест брал ненаписанные блоки из самого реестра — и перестал что-либо
    проверять в тот день, когда написали последний из них: список оказался
    пустым, а правило осталось непроверенным. Поэтому подкладываем свой блок:
    проверяем правило, а не сегодняшний состав реестра. Правило понадобится
    снова при первом же новом запланированном блоке.
    """
    planned = modules.Module(key="test_planned", ready=False, default=False)
    fake = modules.MODULES + (planned,)
    monkeypatch.setattr(modules, "MODULES", fake)
    monkeypatch.setattr(modules, "BY_KEY", {m.key: m for m in fake})
    modules_service.invalidate()

    response = switch(root_client, "test_planned", True)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "module_not_ready"
    assert states(root_client)["test_planned"] is False

    # и на всякий случай: настоящие ненаписанные блоки, если они появятся,
    # подчиняются тому же правилу
    for key in (m.key for m in modules.MODULES if not m.ready and m.key != "test_planned"):
        assert switch(root_client, key, True).status_code == 422, key


def test_unknown_module_is_rejected(root_client):
    response = switch(root_client, "телепатия", True)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_module"


def test_switching_off_documents_does_not_touch_anything_else(root_client, manager_client):
    """Главное свойство модульности.

    Выключили бланки — работа не должна встать: клиенты, заявки, доски и сводка
    обязаны отвечать как ни в чём не бывало. Ради этого всё и затевалось.
    """
    assert switch(root_client, "documents", False).status_code == 200

    closed = manager_client.get(f"{API}/documents")
    assert closed.status_code == 403
    assert closed.json()["error"]["code"] == "module_disabled"

    for path in ("/clients", "/deals", "/boards", "/dashboard", "/pipeline/stages"):
        alive = manager_client.get(f"{API}{path}")
        assert alive.status_code == 200, f"{path} слёг из-за выключенных бланков: {alive.text}"


def test_a_switched_off_module_is_closed_on_direct_links_too(root_client, manager_client):
    """Спрятать пункт меню мало: адрес остаётся в закладках и в старых письмах."""
    client = manager_client.post(f"{API}/clients", json={"name": "Владелец бланка"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Пылесос"}
    ).json()

    assert switch(root_client, "documents", False).status_code == 200

    for path in (
        f"/documents/{doc['id']}",
        f"/documents/by-number/{doc['number']}",
        f"/documents/{doc['id']}/print",
    ):
        blocked = manager_client.get(f"{API}{path}")
        assert blocked.status_code == 403, path
        assert blocked.json()["error"]["code"] == "module_disabled", path

    moved = manager_client.post(f"{API}/documents/{doc['id']}/status", json={"status": "ready"})
    assert moved.status_code == 403


def test_public_qr_link_closes_with_the_module(root_client, manager_client):
    """QR напечатан на бумаге и живёт своей жизнью. Выключили блок — ссылка
    обязана закрыться, иначе выключение косметическое."""
    client = manager_client.post(f"{API}/clients", json={"name": "С квитанцией"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Часы"}
    ).json()

    anon = TestClient(app)
    assert anon.get(f"/d/{doc['number']}").status_code == 200

    assert switch(root_client, "documents", False).status_code == 200
    assert anon.get(f"/d/{doc['number']}").status_code == 404


def test_data_survives_switching_a_module_off_and_on(root_client, manager_client):
    """Выключение — «убрать с глаз», а не «стереть». Передумали — всё на месте."""
    client = manager_client.post(f"{API}/clients", json={"name": "Не потеряться"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Швейная машинка"}
    ).json()

    switch(root_client, "documents", False)
    switch(root_client, "documents", True)

    back = manager_client.get(f"{API}/documents/{doc['id']}")
    assert back.status_code == 200
    assert back.json()["payload"]["fields"]["item"] == "Швейная машинка"
    assert back.json()["number"] == doc["number"]


def test_boards_switch_off_takes_their_share_links_with_them(root_client, manager_client):
    """Витрины — часть досок: выключили доски, делиться нечем."""
    assert switch(root_client, "boards", False).status_code == 200
    assert manager_client.get(f"{API}/boards").status_code == 403
    assert manager_client.patch(f"{API}/shares/1", json={"is_active": False}).status_code == 403
    # а бланки и клиенты продолжают работать
    assert manager_client.get(f"{API}/documents").status_code == 200
    assert manager_client.get(f"{API}/clients").status_code == 200


def test_dependencies_pull_each_other_in_both_directions(root_client, monkeypatch):
    """Зависимость между двумя необязательными блоками — это последовательность.

    Готовых пар «выключаемый на выключаемом» в реестре может не быть, а правило
    исполняться обязано, поэтому пара заводится прямо здесь: проверяем правило, а
    не сегодняшний состав реестра.

    **Выключаешь основание — гаснет и надстройка; включаешь надстройку —
    поднимается основание.** Прежде система в обоих случаях отказывала и
    называла причину. Это было честно, но перекладывало на человека работу,
    которую может сделать сама: он читал «блок ещё нужен вот этим», шёл
    выключать их по одному и возвращался. При цепочке в три звена порядок
    приходилось угадывать.
    """
    base = modules.Module(key="test_base", ready=True, default=False)
    on_top = modules.Module(key="test_on_top", ready=True, default=False, requires=("test_base",))
    fake = modules.MODULES + (base, on_top)
    monkeypatch.setattr(modules, "MODULES", fake)
    monkeypatch.setattr(modules, "BY_KEY", {m.key: m for m in fake})
    modules_service.invalidate()

    # Включили надстройку — основание поднялось следом, отдельного нажатия не
    # потребовалось.
    turned_on = switch(root_client, "test_on_top", True)
    assert turned_on.status_code == 200, turned_on.text
    state = {item["key"]: item["enabled"] for item in turned_on.json()["items"]}
    assert state["test_on_top"] is True
    assert state["test_base"] is True, "основание не поднялось вместе с надстройкой"

    # Выбили основание — надстройка погасла вместе с ним, а не осталась висеть
    # разделом, которому не на что опереться.
    turned_off = switch(root_client, "test_base", False)
    assert turned_off.status_code == 200, turned_off.text
    state = {item["key"]: item["enabled"] for item in turned_off.json()["items"]}
    assert state["test_base"] is False
    assert state["test_on_top"] is False, "надстройка осталась включённой без основания"


def test_the_screen_is_told_what_a_switch_will_take_with_it(root_client, monkeypatch):
    """Экран обязан предупредить ДО нажатия, а знание о связях считает сервер.

    Молча уводить за собой соседние разделы нельзя: человек выключает склад, а
    из меню пропадают ещё и наклейки, и связать одно с другим ему нечем.
    Считает это сервер, а не экран: обход дерева на фронтенде был бы тем же
    правилом во втором экземпляре, а два экземпляра расходятся молча.
    """
    base = modules.Module(key="test_base", ready=True, default=False)
    on_top = modules.Module(key="test_on_top", ready=True, default=False, requires=("test_base",))
    fake = modules.MODULES + (base, on_top)
    monkeypatch.setattr(modules, "MODULES", fake)
    monkeypatch.setattr(modules, "BY_KEY", {m.key: m for m in fake})
    modules_service.invalidate()

    assert switch(root_client, "test_on_top", True).status_code == 200

    listed = root_client.get(f"{API}/modules").json()["items"]
    by_key = {item["key"]: item for item in listed}

    assert by_key["test_base"]["off_takes"] == ["test_on_top"], (
        "экран не узнает, что выключение основания погасит надстройку"
    )
    assert by_key["test_base"]["required_by"] == ["test_on_top"]
    assert by_key["test_on_top"]["requires"] == ["test_base"]
    # Всё уже включено — поднимать нечего, и экрану нечего показывать.
    assert by_key["test_on_top"]["on_needs"] == []

    switch(root_client, "test_base", False)
    listed = root_client.get(f"{API}/modules").json()["items"]
    by_key = {item["key"]: item for item in listed}
    assert by_key["test_on_top"]["on_needs"] == ["test_base"], (
        "экран не узнает, что включение надстройки поднимет основание"
    )

    # выключили надстройку — основание снова свободно
    assert switch(root_client, "test_on_top", False).status_code == 200
    assert switch(root_client, "test_base", False).status_code == 200


def test_only_root_can_switch_modules(manager_client):
    """Это решение уровня «каким бизнесом мы занимаемся», а не личная настройка."""
    denied = switch(manager_client, "documents", False)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"
    assert "settings.manage" in denied.json()["error"]["message"]
    # но видеть список менеджер обязан: иначе интерфейс не знает, что показывать
    assert manager_client.get(MODULES).status_code == 200


def test_the_switch_records_who_did_it(root_client):
    """Раздел пропал из меню — спрашивают не только «когда», но и «кто»."""
    switch(root_client, "documents", False)
    entry = next(m for m in root_client.get(MODULES).json()["items"] if m["key"] == "documents")
    assert entry["enabled"] is False
    assert entry["updated_by_name"], "не видно, кто выключил блок"
    assert entry["updated_at"]


def test_switchable_modules_round_trip(root_client):
    """Каждый необязательный блок выключается и включается обратно."""
    assert SWITCHABLE, "нет ни одного переключаемого блока"
    for key in SWITCHABLE:
        assert switch(root_client, key, False).status_code == 200, key
        assert states(root_client)[key] is False, key
        assert switch(root_client, key, True).status_code == 200, key
        assert states(root_client)[key] is True, key


# --- инвариант: выключенный блок не ломает систему никогда ---
#
# Обещание дано без оговорок, а проверять такое выборочно бессмысленно:
# ломается всегда та комбинация, которую не перебирали. Поэтому ниже не список
# разделов, написанный руками, а перебор — блоков по реестру, адресов по самому
# приложению. Новый блок и новый адрес попадают под проверку сами.


#: Ручки, которые НЕЛЬЗЯ обходить голым запросом, и почему.
#:
#: Обход зовёт каждый адрес обычным `client.get(...)`, а тот ждёт тело целиком.
#: Поточная ручка тело не заканчивает по устройству: она держит соединение и
#: шлёт события, пока браузер слушает. Значит обход на ней встаёт — не потому,
#: что что-то сломано, а потому, что обход и поток несовместимы по природе.
#:
#: Это уже случилось: прогон вставал на час без единой строки в журнале, и
#: понять, где именно, удалось только печатью стека по таймауту.
#:
#: Пропуск здесь не дыра: выключение блока у поточной ручки проверяется
#: отдельно — она закрыта тем же `require_module`, что и соседние, и это видно
#: в `tests/test_route_guards.py`.
POTOKOVYE: dict[str, str] = {
    f"{API}/telegram/stream": (
        "поток событий мессенджера: держит соединение минутами и тело не "
        "заканчивает — обход, ждущий тело, на нём встаёт навсегда"
    ),
}


def test_v_spiske_potokovykh_net_ukazyvayushchikh_v_pustotu():
    """Названная поточная ручка обязана существовать.

    Иначе запись переживёт свой маршрут и станет памяткой о прошлом: обход
    будет пропускать адрес, которого нет, а настоящий поток — обходить и
    вставать. Тот же разбор, что у списков исключений в `test_route_guards.py`.
    """
    vse = set(app.openapi()["paths"])
    propavshie = sorted(set(POTOKOVYE) - vse)
    assert propavshie == [], (
        "в списке поточных ручек записи без маршрута: " + ", ".join(propavshie)
    )


def api_get_paths() -> list[str]:
    """Все GET-адреса API без параметров пути — из схемы приложения.

    Список нельзя писать руками: забытый в нём раздел — ровно тот, который
    потом и падает. Схему строит FastAPI по зарегистрированным роутерам, так
    что новый роутер попадает в перебор в день своего появления.

    Адреса с параметрами (`/deals/{id}`) сюда не входят: у них нет осмысленного
    значения без подготовленных записей. Их проверяет
    `test_data_referenced_across_modules_survives_switching_off`, где записи
    настоящие.
    """
    paths = app.openapi()["paths"]
    return sorted(
        path
        for path, methods in paths.items()
        if "get" in methods
        and path.startswith(API)
        and "{" not in path
        and path not in POTOKOVYE
    )


def switch_all(client, enabled: bool) -> None:
    """Все необязательные блоки разом.

    При выключении порядок обратный: в реестре блоки идут от несущих к
    надстройкам, и выбить основание из-под включённой надстройки система
    откажется — правильно откажется (`module_required_by`).
    """
    for key in SWITCHABLE if enabled else list(reversed(SWITCHABLE)):
        response = switch(client, key, enabled)
        assert response.status_code == 200, f"{key}: {response.text}"


def sweep(client, note: str) -> set[str]:
    """Пройтись по всем адресам API. Возвращает закрытые выключенным блоком.

    Проверяется главное: ни одной пятисотки. 403 допустим ровно с одним кодом —
    `module_disabled`; любой другой означает, что закрылся не выключенный блок,
    а пострадавший сосед, то есть ровно то, чего быть не должно.
    """
    closed = set()
    for path in api_get_paths():
        response = client.get(path)
        assert response.status_code < 500, f"{path} упал {note}: {response.text}"
        if response.status_code == 403:
            code = response.json()["error"]["code"]
            assert code == "module_disabled", f"{path} закрылся кодом {code} {note}"
            closed.add(path)
        elif response.status_code == 422:
            # Адрес без параметров пути, но с обязательным параметром запроса:
            # `/labels/print` без списка товаров — это «нечего печатать», а не
            # поломка. Обход зовёт всё голым, и такой отказ здесь ожидаем.
            #
            # Спрятать за 422 выключенный блок нельзя: гейт блока стоит в
            # зависимостях роутера и срабатывает раньше разбора параметров —
            # выключенный отвечает 403, и это проверено отдельно
            # (`test_zakrytyy_blok_otvechaet_403_a_ne_422`). Конверт ошибки
            # требуем: без него 422 мог бы оказаться падением разбора запроса.
            assert response.json()["error"]["code"], f"{path} ответил 422 без кода {note}"
        else:
            assert response.status_code == 200, f"{path} ответил {response.status_code} {note}"
    return closed


def test_zakrytyy_blok_otvechaet_403_a_ne_422(root_client):
    """Гейт блока срабатывает раньше разбора параметров запроса.

    Порядок здесь не косметика. Ответь выключенный блок «не хватает параметра»
    вместо «блок выключен» — и он выдал бы своё существование тому, для кого его
    нет, а обход в `sweep` перестал бы отличать закрытый раздел от работающего.
    Порядок задаёт FastAPI (зависимости роутера решаются раньше параметров
    обработчика), то есть держится не нашим кодом, — тем более его нужно
    проверять, а не предполагать.

    Список адресов набираем перебором, а не руками: сегодня параметр требует
    один `/labels/print`, а забытым окажется тот, который допишут завтра.
    """
    switch_all(root_client, True)
    demanding = [path for path in api_get_paths() if root_client.get(path).status_code == 422]
    assert demanding, "ни один адрес не требует параметров — проверке нечего сторожить"

    switch_all(root_client, False)
    answers = {path: root_client.get(path) for path in demanding}
    switch_all(root_client, True)

    for path, response in answers.items():
        # Если однажды такой адрес появится в несущем блоке, который не
        # выключается, проверка упадёт здесь — и список придётся сузить. Молча
        # этого не произойдёт, что и требуется.
        assert response.status_code == 403, (
            f"{path} при выключенных блоках ответил {response.status_code}, а не 403"
        )
        assert response.json()["error"]["code"] == "module_disabled"


@pytest.mark.parametrize("key", SWITCHABLE)
def test_each_optional_module_off_alone_breaks_nothing(key, root_client):
    """Каждый необязательный блок по одному: выключен — остальное работает.

    Проверка в две стороны. Пока блок выключен, его адреса закрыты, а чужие
    отвечают. Включили обратно — закрытые открылись: это и доказывает, что
    закрывал их именно он, а не задетый по дороге сосед.
    """
    switch_all(root_client, True)

    # Кого блок уведёт за собой: наклейки стоят на складе, и выключение склада
    # гасит их следом. Возвращая склад, наклейки система НЕ поднимает — их
    # могли не хотеть, — поэтому включать обратно надо обоих, и знать об этом
    # тест должен заранее.
    listed = {item["key"]: item for item in root_client.get(f"{API}/modules").json()["items"]}
    cascaded = listed[key]["off_takes"]

    assert switch(root_client, key, False).status_code == 200

    closed = sweep(root_client, f"при выключенном блоке «{key}»")
    assert closed, f"выключили «{key}», а ни один адрес не закрылся"

    assert switch(root_client, key, True).status_code == 200
    for dependent in cascaded:
        assert switch(root_client, dependent, True).status_code == 200
    for path in sorted(closed):
        response = root_client.get(path)
        # «Открылся обратно» — значит блок его больше не запирает. Ровно 200
        # требовать нельзя: `/labels/print`, позванный голым, отвечает «нечего
        # печатать», и это уже разговор по существу, а не запрет блока.
        assert response.status_code in (200, 422), (
            f"{path} не открылся обратно: {response.text}"
        )
        if response.status_code == 422:
            assert response.json()["error"]["code"] != "module_disabled", (
                f"{path} остался закрытым блоком: {response.text}"
            )


def test_system_works_with_every_optional_module_off(root_client, manager_client):
    """Все необязательные разом: остаются клиенты и заявки — этого достаточно.

    Несущее выключить нельзя, поэтому «голая» система — это ровно клиенты и
    заявки. Если в таком виде нельзя завести клиента и работу по нему, значит
    необязательные блоки на самом деле обязательны, и модульность мнимая.
    """
    switch_all(root_client, False)
    closed = sweep(root_client, "при всех выключенных необязательных блоках")

    assert f"{API}/clients" not in closed
    assert f"{API}/deals" not in closed
    assert f"{API}/dashboard" not in closed

    created = manager_client.post(f"{API}/clients", json={"name": "Без единого блока"})
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]
    deal = manager_client.post(
        f"{API}/deals", json={"title": "Работа без блоков", "client_id": client_id}
    )
    assert deal.status_code == 201, deal.text

    for path in (f"/clients/{client_id}", f"/deals/{deal.json()['id']}", "/dashboard"):
        alive = manager_client.get(f"{API}{path}")
        assert alive.status_code == 200, f"{path}: {alive.text}"


@pytest.fixture(scope="module")
def wired(root_client, manager_client) -> dict:
    """Заявка, на которой сошлись все блоки сразу.

    Одна подготовка на весь раздел: списание со склада под заявку, письмо и
    звонок в ленте клиента, бланк с реквизитами фирмы. Дальше каждая проверка
    выключает тот блок, на который эти данные ссылаются, и смотрит, что от них
    осталось.
    """
    from tests.test_mail import FakeTransport, incoming
    from tests.test_telephony import send_event

    switch_all(root_client, True)
    root_client.patch(f"{API}/settings", json={"values": {"default_country_code": "380"}})

    email = "invariant@client.test"
    phone = "+380671110000"
    client = manager_client.post(
        f"{API}/clients", json={"name": "Связанный Клиент", "email": email, "phone": phone}
    ).json()
    deal = manager_client.post(
        f"{API}/deals", json={"title": "Работа со всеми блоками", "client_id": client["id"]}
    ).json()

    # склад: списание под заявку
    product = root_client.post(
        f"{API}/warehouse/products",
        json={"name": "Профиль алюминиевый", "unit": "m", "cost": 5000, "price": 9000},
    ).json()
    root_client.post(
        f"{API}/warehouse/moves", json={"product_id": product["id"], "kind": "in", "quantity": 10}
    )
    root_client.post(
        f"{API}/warehouse/moves",
        json={
            "product_id": product["id"],
            "kind": "out",
            "quantity": 4,
            "deal_id": deal["id"],
        },
    )

    # фирма и бланк с её реквизитами
    company = root_client.post(
        f"{API}/companies",
        json={
            "name": "Инвариант-Фирма",
            "legal_name": "ООО «Инвариант-Фирма»",
            "bank_name": "Банк Инварианта",
            "bank_account": "UA000000000000000000777",
        },
    ).json()
    document = manager_client.post(
        f"{API}/documents",
        json={
            "client_id": client["id"],
            "deal_id": deal["id"],
            "item": "Витрина",
            "company_id": company["id"],
        },
    ).json()

    # почта: входящее письмо ложится в ленту клиента
    FakeTransport.inbox = [
        incoming(
            uid=1,
            message_id="<invariant-letter@client.test>",
            from_addr=email,
            sent_at=now_utc(),
            subject="Письмо, которое переживёт выключение",
        )
    ]
    FakeTransport.sent = []
    FakeTransport.fail_with = None
    mail_service.set_transport_factory(FakeTransport)
    account = root_client.post(
        f"{API}/mail/accounts",
        json={
            "title": "Ящик инварианта",
            "address": "invariant@studio.test",
            "imap_host": "imap.studio.test",
            "smtp_host": "smtp.studio.test",
            "password": "invariant-mailbox-pw",
        },
    ).json()
    synced = root_client.post(f"{API}/mail/accounts/{account['id']}/sync")
    assert synced.status_code == 200, synced.text
    mail_service.reset_transport_factory()

    # телефония: завершённый звонок ложится в ту же ленту
    secret = root_client.post(f"{API}/telephony/settings/secret").json()["secret"]
    call = send_event(
        secret,
        {
            "call_id": "invariant-call",
            "direction": "in",
            "from": phone,
            "to": "0442000000",
            "started_at": "2026-08-05T11:00:00+00:00",
            "outcome": "answered",
            "duration": 42,
        },
    )
    assert call.status_code == 200, call.text

    yield {
        "client_id": client["id"],
        "deal_id": deal["id"],
        "product_id": product["id"],
        "company_id": company["id"],
        "document_id": document["id"],
        "document_number": document["number"],
    }

    root_client.patch(f"{API}/settings", json={"values": {"default_country_code": ""}})


def feed_kinds(client, client_id: int) -> list[str]:
    notes = client.get(f"{API}/clients/{client_id}/notes?per_page=200").json()["items"]
    return [note["kind"] for note in notes]


def test_data_referenced_across_modules_survives_switching_off(root_client, manager_client, wired):
    """Выключение — «убрать с глаз», а не «стереть», в том числе для чужих ссылок.

    Каждый случай здесь про одно и то же: запись в ленте от письма — это запись
    ленты, а не письма. Выключили почту — история осталась; выключили телефонию
    — звонок в ленте остался; выключили фирмы — реквизиты в уже выданном бланке
    остались, потому что они снимок, а не ссылка.
    """
    switch_all(root_client, True)
    kinds_before = feed_kinds(manager_client, wired["client_id"])
    assert "email" in kinds_before and "call" in kinds_before

    # почта и телефония выключены — лента не потеряла ни строки
    assert switch(root_client, "mail", False).status_code == 200
    assert switch(root_client, "telephony", False).status_code == 200
    assert feed_kinds(manager_client, wired["client_id"]) == kinds_before
    card = manager_client.get(f"{API}/clients/{wired['client_id']}")
    assert card.status_code == 200, card.text
    deal = manager_client.get(f"{API}/deals/{wired['deal_id']}")
    assert deal.status_code == 200, deal.text

    # фирмы выключены — бланк по-прежнему печатается с теми же реквизитами
    assert switch(root_client, "companies", False).status_code == 200
    issued = manager_client.get(f"{API}/documents/{wired['document_id']}")
    assert issued.status_code == 200, issued.text
    assert issued.json()["payload"]["company"]["bank_account"] == "UA000000000000000000777"
    printed = manager_client.get(f"{API}/documents/{wired['document_id']}/print")
    assert printed.status_code == 200
    assert "UA000000000000000000777" in printed.text

    # склад выключен — заявка открывается, ссылка на движение не разыменована
    assert switch(root_client, "warehouse", False).status_code == 200
    assert manager_client.get(f"{API}/deals/{wired['deal_id']}").status_code == 200
    assert manager_client.get(f"{API}/warehouse/moves?deal_id={wired['deal_id']}").status_code == 403


def test_everything_is_in_place_after_switching_the_modules_back_on(
    root_client, manager_client, wired
):
    """Передумали — всё на месте: и записи, и связи между ними."""
    switch_all(root_client, False)
    switch_all(root_client, True)

    moves = manager_client.get(f"{API}/warehouse/moves?deal_id={wired['deal_id']}")
    assert moves.status_code == 200, moves.text
    assert [m["quantity_milli"] for m in moves.json()["items"]] == [-4000]

    product = manager_client.get(f"{API}/warehouse/products/{wired['product_id']}").json()
    assert product["stock_milli"] == 6000

    company = manager_client.get(f"{API}/companies/{wired['company_id']}")
    assert company.status_code == 200
    assert company.json()["bank_account"] == "UA000000000000000000777"

    document = manager_client.get(f"{API}/documents/by-number/{wired['document_number']}")
    assert document.status_code == 200, document.text
    assert document.json()["payload"]["fields"]["item"] == "Витрина"

    kinds = feed_kinds(manager_client, wired["client_id"])
    assert "email" in kinds and "call" in kinds


# --- агрегаты сужаются, а не падают ---


def test_dashboard_without_boards_narrows_instead_of_failing(root_client, manager_client):
    """Сводка без досок считает деньги и воронку, а не отказывает.

    Слагаемое про витрины исчезает, ось графика остаётся: сузился отчёт, а не
    его форма — читателю ответа не приходится знать, какие ключи сегодня бывают.
    """
    switch_all(root_client, True)
    # Доска нужна своя: без неё «досок ноль» получилось бы само собой, и тест
    # проходил бы, ничего не проверяя.
    created = manager_client.post(f"{API}/boards", json={"title": "Доска для сводки"})
    assert created.status_code == 201, created.text

    full = manager_client.get(f"{API}/dashboard").json()
    assert full["boards_total"] > 0
    assert full["recent_boards"], "доска не попала в сводку — проверять нечего"

    assert switch(root_client, "boards", False).status_code == 200
    response = manager_client.get(f"{API}/dashboard")
    assert response.status_code == 200, response.text
    narrow = response.json()

    assert narrow["boards_total"] == 0
    assert narrow["boards_published"] == 0
    assert narrow["recent_boards"] == []
    assert narrow["views_7d"] == 0
    assert narrow["unique_viewers_7d"] == 0
    assert narrow["last_view_at"] is None
    assert [day["date"] for day in narrow["views_by_day"]] == [
        day["date"] for day in full["views_by_day"]
    ]

    # всё, что не про доски, посчиталось ровно так же
    for key in ("money_in_work", "money_won_this_month", "clients_total", "deals_by_stage"):
        assert narrow[key] == full[key], key


def test_reports_do_not_depend_on_the_modules_around_them(root_client, manager_client):
    """Отчёты считаются по заявкам и этапам — и больше ни по чему.

    Равенство «до» и «после» здесь сильнее, чем «ответ 200»: оно показывает, что
    выключение соседних блоков не убавило отчёту ни строки, а значит он их и не
    складывал.
    """
    switch_all(root_client, True)
    names = ("funnel", "revenue", "sources")
    before = {name: manager_client.get(f"{API}/reports/{name}").json() for name in names}

    for key in SWITCHABLE:
        if key == "reports":
            continue
        assert switch(root_client, key, False).status_code == 200, key

    for name in names:
        response = manager_client.get(f"{API}/reports/{name}")
        assert response.status_code == 200, f"{name}: {response.text}"
        assert response.json() == before[name], f"отчёт «{name}» изменился от чужих блоков"


def test_search_drops_the_results_of_a_switched_off_module(root_client, manager_client):
    """Командная строка ищет только по включённым блокам.

    Пункт меню спрятать мало: доска находилась поиском и открывалась по Enter —
    в разделе, которого нет ни в меню, ни в маршрутах, ни в API.
    """
    switch_all(root_client, True)
    client = manager_client.post(f"{API}/clients", json={"name": "Искомый Заказчик"}).json()
    manager_client.post(
        f"{API}/boards", json={"title": "Искомая доска", "client_id": client["id"]}
    )

    found = manager_client.get(f"{API}/search", params={"q": "Иском"}).json()
    assert found["boards"]["items"], "доска не нашлась при включённом блоке"

    assert switch(root_client, "boards", False).status_code == 200
    without = manager_client.get(f"{API}/search", params={"q": "Иском"})
    assert without.status_code == 200, without.text
    # Группа остаётся пустой, а не исчезает: форма ответа одна при любом наборе
    # блоков, и клиенту не нужно знать, какие ключи сегодня бывают.
    assert without.json()["boards"] == {"items": [], "total": 0, "has_more": False}
    assert without.json()["clients"]["items"], "клиенты — несущий блок, они обязаны находиться"
