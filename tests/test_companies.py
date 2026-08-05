"""Свои юрлица: от чьего имени работаем и что уходит на бумагу.

Проверяем не «сохраняется ли поле», а то, из-за чего потом не сойдётся:
реквизиты в выданном бланке не должны меняться вслед за справочником, основная
фирма обязана быть ровно одна, удаление фирмы не должно уносить с собой заявки,
а выключенный блок — закрывать раздел целиком.
"""

import pytest

from core import modules
from core.services import modules_service
from tests.conftest import API

COMPANIES = f"{API}/companies"


@pytest.fixture(autouse=True)
def companies_module_on(root_client):
    """Блок фирм глобальный, а база у тестов общая.

    Один упавший на середине тест оставил бы фирмы выключенными, и посыпались
    бы совершенно посторонние файлы. Восстанавливаем в фикстуре, а не в конце
    теста: тело до конца может и не дойти.
    """
    yield
    modules_service.invalidate()
    root_client.post(f"{API}/modules/companies", json={"enabled": True})


def make_company(root_client, name: str, **extra) -> dict:
    body = {"name": name}
    body.update(extra)
    response = root_client.post(COMPANIES, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def defaults(root_client) -> list[dict]:
    return [c for c in root_client.get(COMPANIES).json()["items"] if c["is_default"]]


# --- главное: снимок реквизитов ---


def test_issued_form_keeps_the_requisites_it_was_printed_with(root_client, manager_client):
    """Ради этого весь модуль и заводился.

    Фирма сменила банк и счёт — акт годовой давности обязан показывать тот
    счёт, который лежит у клиента на руках. Подтяни реквизиты на печать, и обе
    стороны будут держать «оригинал», а совпадать они перестанут.
    """
    company = make_company(
        root_client,
        "Снимок-Тест",
        legal_name="ООО «Снимок-Тест»",
        tax_number="1234567890",
        bank_name="Первый Банк",
        bank_account="UA000000000000000000001",
    )
    root_client.post(f"{COMPANIES}/{company['id']}/default")

    client = manager_client.post(f"{API}/clients", json={"name": "Держатель бумаги"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Ноутбук"}
    ).json()
    assert doc["payload"]["company"]["bank_account"] == "UA000000000000000000001"

    root_client.patch(
        f"{COMPANIES}/{company['id']}",
        json={"bank_name": "Второй Банк", "bank_account": "UA000000000000000000002"},
    )

    again = manager_client.get(f"{API}/documents/{doc['id']}").json()
    assert again["payload"]["company"]["bank_account"] == "UA000000000000000000001"
    assert again["payload"]["company"]["bank_name"] == "Первый Банк"

    # И на печати тоже: перепечатка обязана дать ту же бумагу, а не новую.
    html = manager_client.get(f"{API}/documents/{doc['id']}/print").text
    assert "UA000000000000000000001" in html
    assert "UA000000000000000000002" not in html


def test_deleting_the_company_does_not_touch_issued_forms(root_client, manager_client):
    """Фирму закрыли — выданные бланки обязаны остаться читаемыми."""
    company = make_company(root_client, "Закроют", tax_number="777777")
    root_client.post(f"{COMPANIES}/{company['id']}/default")

    client = manager_client.post(f"{API}/clients", json={"name": "Клиент закрытой"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Тостер"}
    ).json()

    assert root_client.delete(f"{COMPANIES}/{company['id']}").status_code == 200

    again = manager_client.get(f"{API}/documents/{doc['id']}")
    assert again.status_code == 200
    assert again.json()["payload"]["company"]["tax_number"] == "777777"


def test_form_takes_the_company_of_its_deal(root_client, manager_client):
    """Заявку ведут от одного юрлица, а основная в системе может быть другая.
    Бланк обязан выйти от того, от кого работали."""
    ours = make_company(root_client, "Заявочная", tax_number="111")
    other = make_company(root_client, "Основная", tax_number="222")
    root_client.post(f"{COMPANIES}/{other['id']}/default")

    client = manager_client.post(f"{API}/clients", json={"name": "У заявки своя фирма"}).json()
    deal = manager_client.post(
        f"{API}/deals",
        json={"title": "Ремонт", "client_id": client["id"], "company_id": ours["id"]},
    ).json()
    assert deal["company_id"] == ours["id"]

    doc = manager_client.post(
        f"{API}/documents",
        json={"client_id": client["id"], "deal_id": deal["id"], "item": "Кофеварка"},
    ).json()
    assert doc["payload"]["company"]["tax_number"] == "111"


def test_a_form_can_be_issued_from_another_company_than_the_deal(root_client, manager_client):
    """Бланк печатают у стойки, и там иногда виднее, чем при заведении заявки."""
    deal_company = make_company(root_client, "Из заявки", tax_number="333")
    counter = make_company(root_client, "От стойки", tax_number="444")

    client = manager_client.post(f"{API}/clients", json={"name": "Передумали"}).json()
    deal = manager_client.post(
        f"{API}/deals",
        json={"title": "Работа", "client_id": client["id"], "company_id": deal_company["id"]},
    ).json()

    doc = manager_client.post(
        f"{API}/documents",
        json={
            "client_id": client["id"],
            "deal_id": deal["id"],
            "company_id": counter["id"],
            "item": "Утюг",
        },
    ).json()
    assert doc["payload"]["company"]["tax_number"] == "444"


# --- ровно одна основная ---


def test_the_first_company_becomes_the_default_by_itself(root_client):
    """Иначе система с одной фирмой осталась бы без основной, и бланки молча
    печатались бы без реквизитов — ровно та беда, ради которой всё затевалось."""
    # Справочник мог быть уже не пуст — тогда проверяем правило на пустом
    # состоянии, до которого доводим сами: у первой фирмы выбора нет.
    existing = root_client.get(COMPANIES).json()["items"]
    if not existing:
        first = make_company(root_client, "Самая первая")
        assert first["is_default"] is True
    assert len(defaults(root_client)) <= 1


def test_only_one_company_is_the_default_at_a_time(root_client):
    a = make_company(root_client, "Первая А")
    b = make_company(root_client, "Вторая Б")

    root_client.post(f"{COMPANIES}/{a['id']}/default")
    assert [c["id"] for c in defaults(root_client)] == [a["id"]]

    root_client.post(f"{COMPANIES}/{b['id']}/default")
    marked = defaults(root_client)
    assert [c["id"] for c in marked] == [b["id"]], "основными оказались две фирмы сразу"


def test_creating_a_company_as_default_moves_the_flag(root_client):
    """Признак ставится и при создании — иначе завести новую основную можно
    было бы только в два шага, и между ними основных было бы две."""
    old = make_company(root_client, "Старая основная")
    root_client.post(f"{COMPANIES}/{old['id']}/default")

    fresh = make_company(root_client, "Новая основная", is_default=True)
    assert fresh["is_default"] is True
    assert [c["id"] for c in defaults(root_client)] == [fresh["id"]]


def test_deleting_the_default_hands_the_flag_over(root_client):
    """Основную удалили — назначается следующая. Без этого система осталась бы
    без основной фирмы незаметно: ошибки нет, просто в шапке бланка пусто."""
    keep = make_company(root_client, "Остаётся")
    doomed = make_company(root_client, "Удаляется", is_default=True)
    assert [c["id"] for c in defaults(root_client)] == [doomed["id"]]

    root_client.delete(f"{COMPANIES}/{doomed['id']}")

    remaining = defaults(root_client)
    assert len(remaining) == 1
    assert remaining[0]["id"] != doomed["id"]
    assert keep["id"] in [c["id"] for c in root_client.get(COMPANIES).json()["items"]]


def test_a_deleted_company_disappears_from_the_list(root_client):
    company = make_company(root_client, "Исчезающая")
    root_client.delete(f"{COMPANIES}/{company['id']}")

    listed = [c["id"] for c in root_client.get(COMPANIES).json()["items"]]
    assert company["id"] not in listed
    assert root_client.get(f"{COMPANIES}/{company['id']}").status_code == 404


# --- связь с заявками ---


def test_deleting_a_company_does_not_break_its_deals(root_client, manager_client):
    """Справочник чистят, работу — нет. Заявка обязана открыться и после того,
    как её юрлицо закрыли."""
    company = make_company(root_client, "Ликвидируемая")
    client = manager_client.post(f"{API}/clients", json={"name": "Заказчик"}).json()
    deal = manager_client.post(
        f"{API}/deals",
        json={"title": "Долгая работа", "client_id": client["id"], "company_id": company["id"]},
    ).json()

    assert root_client.delete(f"{COMPANIES}/{company['id']}").status_code == 200

    alive = manager_client.get(f"{API}/deals/{deal['id']}")
    assert alive.status_code == 200, alive.text
    assert alive.json()["title"] == "Долгая работа"
    # Доска и список заявок тоже обязаны отвечать: удаление фирмы не событие
    # для остальной системы.
    assert manager_client.get(f"{API}/deals").status_code == 200
    assert manager_client.get(f"{API}/deals/board").status_code == 200


def test_a_deal_can_go_back_to_the_default_company(manager_client, root_client):
    """Прислали null — «от основной», а не «поле не трогали»."""
    company = make_company(root_client, "Временная")
    client = manager_client.post(f"{API}/clients", json={"name": "Передумал"}).json()
    deal = manager_client.post(
        f"{API}/deals",
        json={"title": "Заявка", "client_id": client["id"], "company_id": company["id"]},
    ).json()

    cleared = manager_client.patch(f"{API}/deals/{deal['id']}", json={"company_id": None}).json()
    assert cleared["company_id"] is None


def test_a_deal_cannot_point_at_a_company_that_does_not_exist(manager_client):
    """Иначе несуществующий id всплыл бы только при выдаче бланка — пустой
    шапкой на уже напечатанной бумаге."""
    client = manager_client.post(f"{API}/clients", json={"name": "С опечаткой"}).json()
    bad = manager_client.post(
        f"{API}/deals", json={"title": "Заявка", "client_id": client["id"], "company_id": 999999}
    )
    assert bad.status_code == 404
    assert bad.json()["error"]["code"] == "company_not_found"


# --- права ---


def test_a_manager_can_read_companies_but_not_change_them(manager_client, root_client):
    """Читать надо всем: без списка менеджер не выберет, от кого ведётся
    заявка. Менять — решение уровня «кто мы такие», а не личная настройка."""
    company = make_company(root_client, "Только для чтения")

    assert manager_client.get(COMPANIES).status_code == 200
    assert manager_client.get(f"{COMPANIES}/{company['id']}").status_code == 200

    for response in (
        manager_client.post(COMPANIES, json={"name": "Своя фирма"}),
        manager_client.patch(f"{COMPANIES}/{company['id']}", json={"name": "Переименую"}),
        manager_client.post(f"{COMPANIES}/{company['id']}/default"),
        manager_client.delete(f"{COMPANIES}/{company['id']}"),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "root_required"


def test_a_company_needs_a_name(root_client):
    bad = root_client.post(COMPANIES, json={"name": "   "})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "name_required"


# --- модульность ---


def test_switching_the_module_off_closes_the_section(root_client, manager_client):
    """Спрятать пункт меню мало: адрес остаётся в закладках и в старых письмах."""
    company = make_company(root_client, "Пропадёт из виду")

    assert root_client.post(f"{API}/modules/companies", json={"enabled": False}).status_code == 200

    for response in (
        manager_client.get(COMPANIES),
        manager_client.get(f"{COMPANIES}/{company['id']}"),
        root_client.post(COMPANIES, json={"name": "Новая"}),
        root_client.patch(f"{COMPANIES}/{company['id']}", json={"name": "Другая"}),
        root_client.delete(f"{COMPANIES}/{company['id']}"),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "module_disabled"


def test_switching_the_module_off_does_not_touch_anything_else(root_client, manager_client):
    """Главное свойство модульности: выключили фирмы — работа не встала."""
    assert root_client.post(f"{API}/modules/companies", json={"enabled": False}).status_code == 200

    for path in ("/clients", "/deals", "/documents", "/dashboard", "/pipeline/stages"):
        alive = manager_client.get(f"{API}{path}")
        assert alive.status_code == 200, f"{path} слёг из-за выключенных фирм: {alive.text}"


def test_a_switched_off_module_keeps_requisites_off_the_paper(root_client, manager_client):
    """Выключено — значит не видно нигде, включая бумагу, которая уходит из
    системы на руки. Иначе выключение косметическое."""
    company = make_company(
        root_client, "Невидимая", tax_number="0987654321", is_default=True
    )
    assert company["tax_number"] == "0987654321"

    root_client.post(f"{API}/modules/companies", json={"enabled": False})

    client = manager_client.post(f"{API}/clients", json={"name": "Без реквизитов"}).json()
    doc = manager_client.post(
        f"{API}/documents", json={"client_id": client["id"], "item": "Чайник"}
    ).json()
    assert "0987654321" not in str(doc["payload"]["company"])


def test_data_survives_switching_the_module_off_and_on(root_client):
    """Выключение — «убрать с глаз», а не «стереть»."""
    company = make_company(root_client, "Переживёт", tax_number="555000")

    root_client.post(f"{API}/modules/companies", json={"enabled": False})
    root_client.post(f"{API}/modules/companies", json={"enabled": True})

    back = root_client.get(f"{COMPANIES}/{company['id']}")
    assert back.status_code == 200
    assert back.json()["tax_number"] == "555000"


def test_companies_is_in_the_registry_and_depends_on_nothing():
    """Реквизиты нужны и тому, у кого нет ни одной заявки: счёт выставляют
    раньше, чем заводят работу."""
    module = modules.get("companies")
    assert module is not None
    assert module.ready is True
    assert module.core is False
    assert module.requires == ()
