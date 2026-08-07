"""Мастер первого запуска обязан открыться сам.

Сам экран (`screens/Setup.tsx`), наборы (`core/modules.PRESETS`) и точка API
(`/modules/presets`) были написаны и проверены, а показывался мастер только по
ссылке из настроек — то есть тому, кто и так знает, чего хочет. Человек, только
что поставивший систему, видел дашборд с нулями и оставался на наборе блоков по
умолчанию навсегда.

Здесь проверяется не сам экран (браузера в тестах нет), а то, на чём держится
его появление: признак «систему ещё не настраивали» и то, что этот признак
меняется, как только настройку сделали. Признак — `updated_at` у блоков:
и набор из мастера, и переключатель руками пишут строку в `module_states`.

Размен выбран осознанно и записан здесь же: показать мастер второй раз — потеря
десяти секунд, не показать ни разу — оставить систему ненастроенной. При
сомнении спрашиваем.
"""

import pathlib

from core import modules
from tests.conftest import API

MODULES = f"{API}/modules"
SRC = pathlib.Path(__file__).resolve().parent.parent / "web" / "frontend" / "crm" / "src"


def test_priznak_svezhesti_priezzhaet_vmeste_so_spiskom_blokov(manager_client):
    """Отметка о переключении есть у каждого блока — по ней и видно свежесть.

    Поле читает интерфейс, а не человек, поэтому пропасть оно может молча:
    исчезнет — и `updated_at` у всех окажется пустым, то есть любая система
    станет «свежей» и мастер начнёт спрашивать вечно. Сторона безопасная, но
    заметить это надо здесь, а не на боевом сервере.
    """
    items = manager_client.get(MODULES).json()["items"]
    assert items, "список блоков пуст — проверка смотрит не туда"
    for item in items:
        assert "updated_at" in item, f"у блока {item['key']} нет отметки о переключении"


def test_pereklyuchenie_bloka_ostavlyaet_sled(root_client):
    """Настроенная система второй раз не спрашивает.

    Это половина размена, которую нельзя нарушать: мастер, возвращающийся к
    тому, кто уже собрал систему под себя, перестаёт быть подсказкой и
    становится помехой. След оставляет любое переключение — и руками, и набором.

    Блок берём без зависимых: включение и выключение не должно тащить за собой
    соседей, иначе проверка меряла бы каскад, а не отметку.
    """
    def stamp_of(key: str) -> str | None:
        items = root_client.get(MODULES).json()["items"]
        return next(item["updated_at"] for item in items if item["key"] == key)

    was = {item["key"]: item["enabled"] for item in root_client.get(MODULES).json()["items"]}
    try:
        assert root_client.post(f"{MODULES}/tasks", json={"enabled": False}).status_code == 200
        assert stamp_of("tasks") is not None, "переключение блока не оставило отметки"
    finally:
        root_client.post(f"{MODULES}/tasks", json={"enabled": was["tasks"]})


def test_kazhdyy_nabor_ostavlyaet_sled_na_svezhey_sisteme():
    """Набор обязан включить хоть что-то, чего на свежей системе ещё нет.

    Признак свежести держится ровно на этом: `apply_preset` трогает только те
    блоки, которые ещё не включены, а на свежей системе включены ровно те, у
    кого `default=True`. Набор целиком из таких блоков **не написал бы ни одной
    строки** в `module_states` — и мастер, пройденный до конца, спрашивал бы
    снова при каждом входе.

    Сегодня это верно у всех наборов (в каждом есть склад, почта или заказы), и
    проверка стоит здесь затем, чтобы следующий набор не отменил признак молча.
    """
    for preset in modules.PRESETS:
        off_by_default = [
            key for key in preset.modules if not modules.BY_KEY[key].default
        ]
        assert off_by_default, (
            f"набор «{preset.key}» состоит из блоков, включённых по умолчанию: "
            "пройденный мастер не оставит следа и спросит снова"
        )


def test_master_uvodit_k_sebe_sam_i_tolko_togo_komu_on_otkryt():
    """Правило живёт в `lib/app.tsx` — проверяем чтением, как и прочие правила экранов.

    Три части, и каждая нужна:
    - переход на `/setup` (без него мастер снова становится ссылкой в настройках);
    - признак по `updated_at` (пустота справочников соврала бы: клиентов заводят
      и не настроив ничего);
    - право `settings.manage` — то же, каким закрыт сам маршрут, иначе менеджер
      получал бы отказ вместо экрана, на который он не просился.
    """
    text = (SRC / "lib" / "app.tsx").read_text(encoding="utf-8")
    assert 'navigate("/setup"' in text, "мастер первого запуска никуда не уводит"
    assert "updated_at" in text, "признак свежести системы больше не читается"
    assert 'can(user, "settings.manage")' in text, (
        "на мастер уводят и того, кому он закрыт правом"
    )
