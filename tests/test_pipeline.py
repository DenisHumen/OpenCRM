"""Воронка: настраиваемые этапы под любой бизнес.

CRM рассчитана на кого угодно — магазин, салон, ремонт техники. Названия этапов
у всех свои, поэтому проверяем не конкретные слова, а инварианты, без которых
разваливаются отчёты: тип этапа фиксирован, «выиграна» и «провалена» не могут
исчезнуть, а переименование не осиротит карточки.
"""

import pytest

from tests.conftest import API

PIPE = f"{API}/pipeline"
DEALS = f"{API}/deals"


@pytest.fixture(autouse=True)
def restore_pipeline(root_client):
    """Воронка одна на всю базу, а база одна на прогон.

    Восстанавливать её в конце теста нельзя: упавший тест до этой строки не
    дойдёт и оставит соседям чужой набор этапов — красными станут файлы, к
    которым поломка отношения не имеет. Поэтому только фикстура.
    """
    yield
    root_client.post(f"{PIPE}/preset", json={"preset": "universal"})


def test_pipeline_is_not_empty_out_of_the_box(manager_client):
    """Пустая воронка — пустая доска на первом экране и причина закрыть вкладку."""
    stages = manager_client.get(f"{PIPE}/stages").json()["items"]
    assert stages, "воронка пуста сразу после установки"
    kinds = {s["kind"] for s in stages}
    assert "open" in kinds and "won" in kinds and "lost" in kinds


def test_only_root_changes_the_pipeline(manager_client, root_client):
    """Структура работы всей студии: случайная правка задевает всех сразу."""
    assert manager_client.post(f"{PIPE}/preset", json={"preset": "shop"}).status_code == 403
    assert manager_client.get(f"{PIPE}/stages").status_code == 200, "читать должны все"


def test_presets_cover_different_businesses(root_client):
    presets = {p["key"] for p in root_client.get(f"{PIPE}/presets").json()["items"]}
    assert {"universal", "services", "beauty", "shop", "agency"} <= presets


def test_preset_replaces_the_pipeline(root_client):
    applied = root_client.post(f"{PIPE}/preset", json={"preset": "services"}).json()["items"]
    # По КЛЮЧУ, а не по названию: докстрока этого файла обещает проверять
    # инварианты, а не конкретные слова, и здесь это обещание держалось только
    # до первого перевода. Ключ и есть то, чем этап опознаётся везде остальном —
    # на него ссылаются карточки, отчёты и журнал переходов; название владелец
    # меняет в первый же день.
    keys = [s["key"] for s in applied]
    assert "diagnostics" in keys, "набор для ремонта без диагностики бессмыслен"
    assert [s["kind"] for s in applied][-2:] == ["won", "lost"]



def test_switching_preset_keeps_existing_deals_on_the_board(root_client, manager_client):
    """Сменили набор — старые карточки обязаны остаться видимыми.

    Этапы, которых в новом наборе нет, прячутся. Сделка, оставшаяся ссылаться на
    спрятанный этап, формально цела, но с доски исчезает и найти её нельзя —
    поэтому такие сделки переезжают на первый открытый этап.
    """
    client = manager_client.post(f"{API}/clients", json={"name": "Переживший"}).json()
    deal = manager_client.post(
        DEALS, json={"title": "До смены набора", "client_id": client["id"]}
    ).json()

    root_client.post(f"{PIPE}/preset", json={"preset": "beauty"})

    moved = manager_client.get(f"{DEALS}/{deal['id']}").json()
    board = manager_client.get(f"{DEALS}/board").json()
    on_board = [d["id"] for column in board["columns"] for d in column["deals"]]

    assert moved["stage"] in {c["key"] for c in board["columns"]}
    assert deal["id"] in on_board, "сделка пропала с доски после смены воронки"


def test_renaming_a_stage_keeps_its_deals(root_client, manager_client):
    """Ключ этапа стабилен: иначе переименование осиротит все карточки в нём."""
    client = manager_client.post(f"{API}/clients", json={"name": "Переименование"}).json()
    deal = manager_client.post(DEALS, json={"title": "Держится", "client_id": client["id"]}).json()
    stage = deal["stage"]

    renamed = root_client.patch(f"{PIPE}/stages/{stage}", json={"name": "Свежие обращения"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Свежие обращения"
    assert renamed.json()["key"] == stage, "ключ обязан пережить переименование"

    board = manager_client.get(f"{DEALS}/board").json()
    column = next(c for c in board["columns"] if c["key"] == stage)
    assert deal["id"] in [d["id"] for d in column["deals"]]


def test_the_pipeline_cannot_lose_its_closing_stages(root_client):
    """Без «выиграна»/«провалена» некуда закрыть сделку, а конверсия и потери
    перестают считаться — воронка превращается в список без итога."""
    stages = root_client.get(f"{PIPE}/stages").json()["items"]
    won = next(s for s in stages if s["kind"] == "won")

    dropped = root_client.delete(f"{PIPE}/stages/{won['key']}")
    assert dropped.status_code == 422
    assert dropped.json()["error"]["code"] == "last_stage_of_kind"

    retyped = root_client.patch(f"{PIPE}/stages/{won['key']}", json={"kind": "open"})
    assert retyped.status_code == 422


def test_archived_stage_hands_its_deals_to_an_open_one(root_client, manager_client):
    """Этап убирают с доски — сделки из него не должны пропасть."""
    added = root_client.post(f"{PIPE}/stages", json={"name": "Временный", "kind": "open"}).json()
    client = manager_client.post(f"{API}/clients", json={"name": "Переезд"}).json()
    deal = manager_client.post(DEALS, json={"title": "Переедет", "client_id": client["id"]}).json()
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": added["key"]})

    assert root_client.delete(f"{PIPE}/stages/{added['key']}").status_code == 200

    moved = manager_client.get(f"{DEALS}/{deal['id']}").json()
    assert moved["stage"] != added["key"]
    keys = {c["key"] for c in manager_client.get(f"{DEALS}/board").json()["columns"]}
    assert moved["stage"] in keys, "сделка должна оказаться на видимом этапе"


def test_stage_key_is_generated_from_a_cyrillic_name(root_client):
    """Ключ попадает в адреса и выгрузки — кириллице там не место."""
    stage = root_client.post(f"{PIPE}/stages", json={"name": "Ожидает оплаты", "kind": "open"}).json()
    assert stage["key"].isascii() and stage["key"]
    root_client.delete(f"{PIPE}/stages/{stage['key']}")


def test_unknown_preset_is_rejected(root_client):
    bad = root_client.post(f"{PIPE}/preset", json={"preset": "выдуманный"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "unknown_preset"


def test_a_preset_change_does_not_move_deals_behind_the_journal(root_client, manager_client):
    """Переезд заявок при смене воронки виден в журнале этапов.

    Правило проекта: журнал заполняется всегда, иначе отчёт «сколько заявка
    простояла в этапе» дырявый ровно там, где этап поменяли другим путём. Смена
    пресета и была таким путём — десятки заявок меняли этап молча, и в истории
    каждой оставался разрыв: последний записанный переход вёл в этап, которого у
    заявки уже нет.
    """
    from tests.test_deals import DEALS, make_client

    root_client.post(f"{API}/pipeline/preset", json={"preset": "universal"})
    client = make_client(manager_client, "Клиент переезда")
    deal = manager_client.post(DEALS, json={"title": "Переедет", "client_id": client["id"]}).json()

    # Ставим заявку на этап, которого в другом наборе нет.
    stages = root_client.get(f"{API}/pipeline/stages").json()["items"]
    doomed = next(s["key"] for s in stages if s["kind"] == "open" and s["key"] != deal["stage"])
    assert manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": doomed}).status_code == 200

    applied = root_client.post(f"{API}/pipeline/preset", json={"preset": "services"})
    assert applied.status_code == 200, applied.text

    card = manager_client.get(f"{DEALS}/{deal['id']}").json()
    chain = [(row["from_stage"], row["to_stage"]) for row in card["stage_history"]]

    assert chain[-1][1] == card["stage"], (
        "последний записанный переход ведёт не туда, где заявка стоит сейчас"
    )
    assert (doomed, card["stage"]) in chain, "переезд при смене воронки не записан"

    # В ленте заявки записи о переезде быть не должно: воронку перестроил
    # администратор, работы по заявке никто не делал.
    feed = manager_client.get(f"{DEALS}/{deal['id']}/feed?kind=stage").json()["items"]
    assert not any(doomed in entry["body"] for entry in feed), (
        "перестройка воронки написала в ленту заявки то, чего никто не делал"
    )
