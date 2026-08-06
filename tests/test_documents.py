"""Бланки: приёмная квитанция, коды, состояния.

Это единственная часть системы, копия которой уходит из неё на руки человеку.
Поэтому проверяем не «создаётся ли запись», а то, из-за чего потом спорят:
совпадает ли бумага с базой, читается ли код, находится ли бланк сканом и не
выдаёт ли публичная ссылка лишнего.
"""

from fastapi.testclient import TestClient

from core.services import codes
from tests.conftest import API
from web.main import app

DOCS = f"{API}/documents"


def make_doc(manager_client, **extra):
    client = manager_client.post(f"{API}/clients", json={"name": "Пётр Иванов", "phone": "+7 900 000-00-00"}).json()
    body = {
        "client_id": client["id"],
        "item": "Ноутбук Asus X515",
        "serial": "SN-77123",
        "condition": "Потёртости на крышке, скол у петли",
        "accessories": "Зарядка, чехол",
        "problem": "Не включается после падения",
        "estimate": "от 4000",
        "terms": "Диагностика 2 дня",
    }
    body.update(extra)
    return manager_client.post(DOCS, json=body).json()


def test_document_needs_an_item(manager_client):
    client = manager_client.post(f"{API}/clients", json={"name": "Без вещи"}).json()
    bad = manager_client.post(DOCS, json={"client_id": client["id"], "item": "  "})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "item_required"


def test_numbers_are_sequential_within_the_year(manager_client):
    first = make_doc(manager_client)
    second = make_doc(manager_client)
    year, left = first["number"].split("-")
    assert len(left) == 6 and left.isdigit()
    assert int(second["number"].split("-")[1]) == int(left) + 1
    assert year.isdigit() and len(year) == 4


def test_paper_keeps_what_was_printed_even_if_the_client_changes(manager_client):
    """Снимок, а не ссылка.

    У человека на руках бумага. Переименовали клиента, поправили телефон — на
    его квитанции по-прежнему то, что напечатали, иначе спор «что вы у меня
    приняли» разрешить нечем.
    """
    doc = make_doc(manager_client)
    client_id = doc["client_id"]
    assert doc["payload"]["client"]["name"] == "Пётр Иванов"

    manager_client.patch(f"{API}/clients/{client_id}", json={"name": "Пётр Сидоров", "phone": "+7 111"})

    again = manager_client.get(f"{DOCS}/{doc['id']}").json()
    assert again["payload"]["client"]["name"] == "Пётр Иванов"
    assert again["payload"]["client"]["phone"] == "+7 900 000-00-00"


def test_document_survives_deleting_the_client(manager_client):
    """Клиента удалили — бланк обязан остаться читаемым."""
    doc = make_doc(manager_client)
    manager_client.delete(f"{API}/clients/{doc['client_id']}")

    again = manager_client.get(f"{DOCS}/{doc['id']}")
    assert again.status_code == 200
    assert again.json()["payload"]["client"]["name"] == "Пётр Иванов"


def test_found_by_the_number_from_the_scanner(manager_client):
    doc = make_doc(manager_client)
    found = manager_client.get(f"{DOCS}/by-number/{doc['number']}")
    assert found.status_code == 200
    assert found.json()["id"] == doc["id"]

    assert manager_client.get(f"{DOCS}/by-number/2000-000001").status_code == 404


def test_a_walk_in_client_gets_a_form_without_a_card(manager_client):
    """Человек стоит у стойки. Заводить карточку клиента до квитанции — лишний
    шаг, и форма приёма на это рассчитывает: имя прямо в бланк."""
    doc = manager_client.post(
        DOCS, json={"client_name": "Зашёл с улицы", "client_phone": "+7 999", "item": "Чайник"}
    )
    assert doc.status_code == 201, doc.text
    body = doc.json()
    assert body["client_id"] is None
    assert body["payload"]["client"]["name"] == "Зашёл с улицы"
    assert body["payload"]["client"]["phone"] == "+7 999"


def test_forms_of_a_deal_are_found_by_the_deal(manager_client):
    """Карточка сделки показывает свои бланки. Без фильтра выданный из сделки
    бланк уходил бы в общий список, и связь с работой терялась."""
    client = manager_client.post(f"{API}/clients", json={"name": "Со сделкой"}).json()
    deal = manager_client.post(
        f"{API}/deals", json={"title": "Ремонт", "client_id": client["id"]}
    ).json()

    mine = manager_client.post(
        DOCS, json={"client_id": client["id"], "deal_id": deal["id"], "item": "Ноутбук"}
    ).json()
    make_doc(manager_client)   # чужой бланк, не этой сделки

    found = manager_client.get(DOCS, params={"deal_id": deal["id"]}).json()
    assert [d["id"] for d in found["items"]] == [mine["id"]]
    assert found["total"] == 1


def test_status_moves_are_recorded(manager_client):
    doc = make_doc(manager_client)
    assert doc["status"] == "issued"

    for status in ("in_progress", "ready", "closed"):
        moved = manager_client.post(f"{DOCS}/{doc['id']}/status", json={"status": status})
        assert moved.status_code == 200, moved.text
        assert moved.json()["status"] == status

    events = manager_client.get(f"{DOCS}/{doc['id']}").json()["events"]
    assert [e["to_status"] for e in events] == ["issued", "in_progress", "ready", "closed"]
    assert all(e["author_name"] for e in events)


def test_a_finished_document_cannot_be_reopened(manager_client):
    """Закрытый бланк — уже отданная вещь. Открой его заново, и история
    перестанет отвечать на вопрос «когда клиент забрал»."""
    doc = make_doc(manager_client)
    manager_client.post(f"{DOCS}/{doc['id']}/status", json={"status": "closed"})

    back = manager_client.post(f"{DOCS}/{doc['id']}/status", json={"status": "in_progress"})
    assert back.status_code == 422
    assert back.json()["error"]["code"] == "document_finished"


def test_unknown_status_is_rejected(manager_client):
    doc = make_doc(manager_client)
    bad = manager_client.post(f"{DOCS}/{doc['id']}/status", json={"status": "выдумка"})
    assert bad.status_code == 422


def test_printout_has_two_identical_halves_with_a_cut_line(manager_client):
    """Клиент и мастер держат одно и то же — иначе спорить не о чем."""
    doc = make_doc(manager_client)
    page = manager_client.get(f"{DOCS}/{doc['id']}/print")
    assert page.status_code == 200
    html = page.text

    assert html.count("Экземпляр клиента") == 1
    assert html.count("Экземпляр мастерской") == 1
    assert "линия отреза" in html
    # обе половины несут номер, штрихкод и QR
    assert html.count(doc["number"]) >= 2
    assert html.count("<svg") >= 4          # по штрихкоду и QR на каждую половину
    assert "Ноутбук Asus X515" in html
    assert "Не включается после падения" in html


def test_printout_speaks_three_languages(manager_client):
    doc = make_doc(manager_client)
    checks = {
        "ru": "Квитанция о приёме в работу",
        "en": "Service intake receipt",
        "uk": "Квитанція про приймання в роботу",
    }
    for locale, title in checks.items():
        html = manager_client.get(f"{DOCS}/{doc['id']}/print", params={"locale": locale}).text
        assert title in html, locale


def test_printed_language_can_differ_from_the_stored_one(manager_client):
    """Бланк печатают под клиента, а не под сотрудника: приехал турист —
    печатаем по-английски, ничего в базе не меняя."""
    doc = make_doc(manager_client, locale="ru")
    html = manager_client.get(f"{DOCS}/{doc['id']}/print", params={"locale": "en"}).text
    assert "Service intake receipt" in html
    assert manager_client.get(f"{DOCS}/{doc['id']}").json()["locale"] == "ru"


def test_public_status_page_shows_state_without_login(manager_client):
    """Клиент сканирует QR со своей же квитанции — вход требовать нельзя."""
    doc = make_doc(manager_client)
    manager_client.post(f"{DOCS}/{doc['id']}/status", json={"status": "ready"})

    anon = TestClient(app)
    page = anon.get(f"/d/{doc['number']}")
    assert page.status_code == 200
    assert "Готово к выдаче" in page.text
    assert "Ноутбук Asus X515" in page.text


def test_public_page_does_not_leak_anything_extra(manager_client):
    """Ссылку могут переслать или подобрать: на странице ровно то, что и так
    напечатано у клиента на руках."""
    doc = make_doc(manager_client)
    page = TestClient(app).get(f"/d/{doc['number']}").text

    assert "+7 900 000-00-00" not in page, "телефон клиента виден посторонним"
    assert "от 4000" not in page, "цена видна посторонним"
    assert "Потёртости" not in page, "внутренние заметки видны посторонним"


def test_unknown_number_looks_the_same_as_a_hidden_one(manager_client):
    """Иначе перебором номеров узнают, сколько у мастерской заказов."""
    anon = TestClient(app)
    assert anon.get("/d/2000-000999").status_code == 404


def test_public_status_needs_no_authentication_at_all(base_client):
    assert base_client.get("/d/2000-000999").status_code == 404


# --- коды ---


def test_barcode_and_qr_are_inline_svg():
    """Оба встраиваются в страницу: пролог XML и DOCTYPE внутри HTML недопустимы."""
    bar = codes.barcode_svg("2026-000123")
    qr = codes.qr_svg("https://example.com/d/2026-000123")
    for svg in (bar, qr):
        assert svg.startswith("<svg"), svg[:60]
        assert "<?xml" not in svg
        assert "DOCTYPE" not in svg.upper()


def test_barcode_encodes_the_number_it_was_given():
    """Штрихкод обязан нести именно номер бланка: разойдись они — сканер
    находил бы чужой документ или ничего."""
    svg = codes.barcode_svg("2026-000123")
    assert "2026-000123" in svg


def test_empty_code_is_rejected_instead_of_printing_a_blank():
    for bad in ("", None):
        try:
            codes.barcode_svg(bad)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError("пустой штрихкод молча напечатался")


def test_two_receptionists_issuing_at_once_both_get_paper(manager_client):
    """Номер, занятый соседом, не превращается в ошибку сервера.

    Номер считается как «максимум по году плюс один», и между счётом и вставкой
    есть окно. У стойки в него попадают: двое выдают бумагу одновременно, оба
    видят один и тот же максимум. Уникальный индекс данные спасал — двух бланков
    с одним номером не появлялось, — но проигравший получал 500. Живым прогоном
    на настоящем сервере: из двадцати одновременных выдач тринадцать отвечали
    ошибкой. Приёмщик жмёт «выдать» снова, попадает в ту же гонку и решает, что
    сломалась программа.

    Настоящую параллельность здесь воспроизвести нечем — `TestClient` работает в
    одном процессе, — поэтому проигравшего изображаем: соседняя сессия занимает
    номер ровно между тем, как его посчитали, и тем, как записали.
    """
    from core.services import document_service
    from database.models import Document
    from database.session import SessionLocal

    real_next_number = document_service.next_number
    taken: list[str] = []

    def steal_the_number(db):
        """Посчитать номер — и отдать его соседу первым."""
        number = real_next_number(db)
        if not taken:            # перехватываем только первую попытку
            taken.append(number)
            with SessionLocal() as thief:
                thief.add(Document(
                    number=number, kind="intake", locale="ru", status="issued",
                    payload="{}",
                ))
                thief.commit()
        return number

    document_service.next_number = steal_the_number
    try:
        issued = make_doc(manager_client)
    finally:
        document_service.next_number = real_next_number

    assert "number" in issued, f"выдача бланка сорвалась: {issued}"
    assert issued["number"] != taken[0], "бланк выдан под уже занятым номером"
    assert int(issued["number"].split("-")[1]) == int(taken[0].split("-")[1]) + 1


def test_two_people_closing_one_form_do_not_tear_its_history(manager_client):
    """Второй, кто меняет статус бланка, получает отказ, а не молчаливый перехват.

    У бланка своя история переходов, и она отвечает на единственный вопрос,
    ради которого её ведут: когда клиент забрал вещь. Двое, нажавшие «готово» и
    «выдано» разом, оставляли в ней два перехода из одного состояния — данные
    целы, а история перестаёт быть историей. Та же беда, что была у этапа
    заявки, и лечится тем же условием в самом UPDATE.

    Соседа изображаем отдельной сессией: она меняет статус между чтением и
    записью. Настоящую параллельность `TestClient` не даёт — он в одном
    процессе, — а порядок событий тот же.
    """
    from sqlalchemy import update

    from core import exceptions as errors
    from core.services import document_service
    from database.models import Document, User
    from database.session import SessionLocal

    doc = make_doc(manager_client)

    with SessionLocal() as mine:
        author = mine.query(User).first()
        held = document_service.get(mine, doc["id"])
        assert held.status == "issued"

        with SessionLocal() as neighbour:
            neighbour.execute(
                update(Document).where(Document.id == doc["id"]).values(status="closed")
            )
            neighbour.commit()

        try:
            document_service.set_status(mine, doc["id"], "ready", author)
            raise AssertionError("перехват прошёл молча — история снова рвётся")
        except errors.ConflictError as refused:
            assert refused.code == "document_status_changed"
        mine.rollback()

    card = manager_client.get(f"{DOCS}/{doc['id']}").json()
    chain = [(e["from_status"], e["to_status"]) for e in card["events"]]
    assert ("issued", "ready") not in chain, "записан переход, которого не было"
