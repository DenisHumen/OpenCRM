"""Наборы блоков под тип дела.

Набор существует ради одного: человек, только что поставивший CRM, не знает,
что ему нужно, и десяток переключателей с незнакомыми словами он закрывает
вместе со вкладкой. Тот же довод, по которому у воронки сделаны пресеты, а не
пустой редактор.

Поэтому и проверки здесь — про две вещи. Что набор **ничего не выключает**:
забрать раздел с данными одним нажатием — самый быстрый способ потерять
доверие. И что состав **не расходится с реестром**: список ключей, живущий
отдельно от блоков, разъезжается с ними молча.
"""

import pytest

from core import modules
from core.services import pipeline_service
from tests.conftest import API

MODULES = f"{API}/modules"


@pytest.fixture(autouse=True)
def restore_blocks(root_client):
    """Наборы включают блоки насовсем, а набор гоняется среди соседей.

    Возвращаем состояние, каким оно было: иначе включённые здесь заказы и
    телефония меняли бы условия у тех, кто пришёл следом.
    """
    before = {item["key"]: item["enabled"] for item in root_client.get(MODULES).json()["items"]}
    yield
    for key, was in before.items():
        module = modules.get(key)
        if module is None or module.core or not module.ready:
            continue
        now = {i["key"]: i["enabled"] for i in root_client.get(MODULES).json()["items"]}
        if now.get(key) != was:
            root_client.post(f"{MODULES}/{key}", json={"enabled": was})


# --- состав не расходится с реестром ------------------------------------------


def test_v_naborakh_net_nesushchestvuyushchikh_blokov():
    """Опечатка в ключе — это набор, который тихо не включает обещанное.

    Проверка механическая нарочно: список, который сверяют глазами, расходится
    с реестром на первом же переименовании.
    """
    for preset in modules.PRESETS:
        for key in preset.modules:
            module = modules.get(key)
            assert module is not None, f"набор «{preset.key}» называет несуществующий блок {key}"
            assert module.ready, f"набор «{preset.key}» включает ненаписанный блок {key}"
            assert not module.core, (
                f"набор «{preset.key}» называет несущий блок {key} — он и так включён всегда"
            )


def test_voronka_i_slovo_iz_naborov_sushchestvuyut():
    """Набор отвечает на вопрос целиком: блоки, воронка и слово для заявки."""
    from database.models.settings import SETTING_DEFAULTS

    for preset in modules.PRESETS:
        assert preset.pipeline in pipeline_service.PRESETS, (
            f"набор «{preset.key}» ссылается на несуществующую воронку {preset.pipeline}"
        )
        assert preset.deal_term in ("deal", "order", "request", "booking"), preset.deal_term
    # Значение по умолчанию обязано быть из того же списка — иначе набор
    # ставил бы слово, которого система не понимает.
    assert SETTING_DEFAULTS["deal_term"] in ("deal", "order", "request", "booking")


def test_kazhdyy_blok_nazvan_hotya_by_v_odnom_nabore():
    """Новый блок обязан попасть в наборы — или не попасть осознанно.

    Тест не требует включать его везде: у блока может не быть своего типа дела.
    Он требует, чтобы решение было принято, а не забыто, — поэтому исключения
    названы поимённо и с причиной.
    """
    #: Блоки, которых в наборах нет намеренно.
    ASIDE = {
        # Отчёты включены у всех по умолчанию: считать выручку хочет любой, и
        # спрашивать об этом отдельно незачем.
        "reports",
        # Шаблоны — тоже: типовой ответ нужен всякому, кто пишет клиентам,
        # и спрашивать об этом отдельным вопросом незачем.
        "templates",
    }
    named = {key for preset in modules.PRESETS for key in preset.modules}
    optional = {m.key for m in modules.MODULES if not m.core and m.ready}
    forgotten = sorted(optional - named - ASIDE)
    assert not forgotten, (
        "эти блоки не попали ни в один набор: " + ", ".join(forgotten)
        + ". Добавьте их в набор или назовите в ASIDE с причиной."
    )


# --- применение ---------------------------------------------------------------


def test_nabor_vklyuchaet_nazvannoe(root_client):
    """Выбрали «Розница» — включились ровно названные блоки и то, на чём стоят."""
    applied = root_client.post(f"{MODULES}/presets", json={"key": "retail"})
    assert applied.status_code == 200, applied.text
    body = applied.json()

    state = {item["key"]: item["enabled"] for item in body["items"]}
    for key in modules.preset("retail").modules:
        assert state[key] is True, f"{key} не включился"
    # Склад стоит на заявках, наклейки — на складе: зависимости поднимаются
    # сами и тем же путём, что при переключении руками.
    assert state["deals"] is True


def test_nabor_nichego_ne_vyklyuchaet(root_client):
    """Забрать раздел с данными одним нажатием — способ потерять доверие.

    Человек выбирает «Розница», а из меню пропадают доски, на которых лежит
    полгода работы. Поэтому блоки вне набора остаются как есть.
    """
    root_client.post(f"{API}/modules/boards", json={"enabled": True})
    root_client.post(f"{API}/modules/mail", json={"enabled": True})

    applied = root_client.post(f"{MODULES}/presets", json={"key": "retail"})
    state = {item["key"]: item["enabled"] for item in applied.json()["items"]}

    # Ни доски, ни почта в «Рознице» не названы — и остались включёнными.
    assert state["boards"] is True, "набор выключил доски"
    assert state["mail"] is True, "набор выключил почту"


def test_povtornyy_vybor_nabora_nichego_ne_lomaet(root_client):
    """Нажали дважды — второй раз включать нечего, и это не ошибка.

    Гасим блоки набора заранее: иначе проверка зависела бы от того, кто из
    соседей отработал раньше и что успел включить, — и падала бы через раз не
    на своей ошибке.
    """
    for key in reversed(modules.preset("services").modules):
        root_client.post(f"{MODULES}/{key}", json={"enabled": False})

    first = root_client.post(f"{MODULES}/presets", json={"key": "services"})
    assert first.status_code == 200
    assert first.json()["asked"], "первый раз ничего не включилось"

    second = root_client.post(f"{MODULES}/presets", json={"key": "services"})
    assert second.status_code == 200, second.text
    assert second.json()["asked"] == [], "второй раз что-то включилось заново"
    state = {item["key"]: item["enabled"] for item in second.json()["items"]}
    assert all(state[key] for key in modules.preset("services").modules)


def test_nabor_stavit_voronku_i_slovo(root_client):
    """Человек отвечал на вопрос «чем вы занимаетесь», а не «какие разделы»."""
    applied = root_client.post(f"{MODULES}/presets", json={"key": "retail"})
    assert applied.status_code == 200, applied.text

    stages = root_client.get(f"{API}/pipeline/stages").json()["items"]
    expected = [key for key, _, _ in pipeline_service.PRESETS["shop"]["stages"]]
    assert [s["key"] for s in stages if not s["is_archived"]] == expected

    assert root_client.get(f"{API}/workspace").json()["deal_term"] == "order"

    root_client.patch(f"{API}/settings", json={"values": {"deal_term": "deal"}})
    root_client.post(f"{API}/pipeline/preset", json={"preset": "universal"})


def test_voronku_i_slovo_mozhno_ne_trogat(root_client):
    """Тому, кто вернулся к экрану с уже настроенной воронкой, менять её незачем."""
    root_client.patch(f"{API}/settings", json={"values": {"deal_term": "booking"}})
    try:
        applied = root_client.post(
            f"{MODULES}/presets",
            json={"key": "retail", "apply_pipeline": False, "apply_deal_term": False},
        )
        assert applied.status_code == 200, applied.text
        assert root_client.get(f"{API}/workspace").json()["deal_term"] == "booking"
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"deal_term": "deal"}})


def test_neizvestnyy_nabor_otvergaetsya(root_client):
    denied = root_client.post(f"{MODULES}/presets", json={"key": "нет такого"})
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "unknown_preset"


def test_spisok_naborov_govorit_chto_poyavitsya(root_client):
    """Экран показывает, что именно включится, — иначе выбор выглядит как
    «нажму и что-то произойдёт»."""
    listed = root_client.get(f"{MODULES}/presets")
    assert listed.status_code == 200, listed.text
    items = {item["key"]: item for item in listed.json()["items"]}
    assert set(items) == {p.key for p in modules.PRESETS}

    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    retail = {i["key"]: i for i in root_client.get(f"{MODULES}/presets").json()["items"]}["retail"]
    assert "warehouse" in retail["modules"]
    assert "warehouse" not in retail["will_enable"], "уже включённое обещано заново"


def test_nabor_pod_pravom_nastroek(manager_client):
    """Состав системы — решение уровня «каким делом мы занимаемся»."""
    denied = manager_client.post(f"{MODULES}/presets", json={"key": "retail"})
    assert denied.status_code == 403, denied.text
    # И смотреть тоже: наборы нужны тому, кто решает состав системы. Список
    # блоков при этом остаётся открытым любому — меню без него не построить.
    assert manager_client.get(f"{MODULES}/presets").status_code == 403
    assert manager_client.get(MODULES).status_code == 200
