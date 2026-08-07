"""Шаблоны сообщений — и главное в них: подстановка.

Проверяется не «сохраняется ли текст», а то, ради чего блок писался и чем он
опасен. Шаблон — единственное место системы, где текст, набранный менеджером
однажды, уходит клиенту много раз и без перечитывания. Поэтому четыре свойства
подстановки закреплены поимённо, и каждое написано на конкретную беду:

1. **набор полей закрыт** — иначе шаблон однажды вытащит клиенту себестоимость
   его же заказа;
2. **неизвестная подстановка не молчит** — иначе клиент получит «Здравствуйте,
   !», и это увидят все, кроме автора;
3. **законно пустое значение видно** — прочерк вместо дырки посреди
   предложения, и список таких полей отдельной строкой;
4. **подставленное не управляет подстановкой** — имя клиента с фигурными
   скобками, обратной косой и переводом строки остаётся именем.

База у тестов общая и переживает файл, поэтому всё созданное убирается за собой,
а искать надо своё по приметному названию, а не считать строки во всей таблице.
"""

import pytest

from core.services import modules_service, template_service
from tests.conftest import API, make_manager

TEMPLATES = f"{API}/templates"
MODULES = f"{API}/modules"
ROLES = f"{API}/roles"


# --- вспомогательное ---------------------------------------------------------


def switch(client, key: str, enabled: bool):
    return client.post(f"{MODULES}/{key}", json={"enabled": enabled})


@pytest.fixture(autouse=True)
def module_on(root_client):
    """Блок включён до теста и возвращается на место после.

    Восстановление в фикстуре, а не в конце теста: тело до конца может и не
    дойти, а выключенный блок посыпал бы совершенно посторонние файлы.
    """
    modules_service.invalidate()
    for key in ("templates", "boards", "companies"):
        assert switch(root_client, key, True).status_code == 200, key
    yield
    modules_service.invalidate()
    for key in ("templates", "boards", "companies"):
        switch(root_client, key, True)


@pytest.fixture
def maker(root_client):
    """Заводит шаблоны и убирает их за собой: название уникально на всю базу."""
    created: list[int] = []

    def make(name: str, body: str, channel: str = "any") -> dict:
        response = root_client.post(
            TEMPLATES, json={"name": name, "body": body, "channel": channel}
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
        return response.json()

    yield make

    for template_id in created:
        root_client.delete(f"{TEMPLATES}/{template_id}")


@pytest.fixture
def client_with_deal(root_client):
    """Клиент, заявка по нему и опубликованная доска с живой ссылкой.

    Одна подготовка на весь файл нужна почти всем проверкам подстановки: без
    заявки половина полей пуста законно, и отличить «поле не подставилось» от
    «подставлять нечего» стало бы нечем.
    """

    def make(name: str, company: str = "") -> dict:
        client = root_client.post(
            f"{API}/clients", json={"name": name, "company": company}
        ).json()
        deal = root_client.post(
            f"{API}/deals", json={"title": "Ремонт витрины", "client_id": client["id"]}
        ).json()
        board = root_client.post(
            f"{API}/boards",
            json={"title": "Доска шаблона", "client_id": client["id"], "deal_id": deal["id"]},
        ).json()
        share = root_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
        return {"client": client, "deal": deal, "board": board, "share": share}

    return make


def render(client, template_id: int, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return client.get(f"{TEMPLATES}/{template_id}/render" + (f"?{query}" if query else ""))


# --- обычная жизнь -----------------------------------------------------------


def test_shablon_zavoditsya_pravitsya_i_udalyaetsya(root_client, maker):
    created = maker("Приветствие ТЕСТ", "Здравствуйте, {client_name}!", channel="email")
    assert created["channel"] == "email"

    listed = root_client.get(TEMPLATES)
    assert listed.status_code == 200
    assert created["id"] in [item["id"] for item in listed.json()["items"]]

    changed = root_client.patch(
        f"{TEMPLATES}/{created['id']}", json={"body": "Добрый день, {client_name}."}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["body"] == "Добрый день, {client_name}."

    assert root_client.delete(f"{TEMPLATES}/{created['id']}").status_code == 200
    assert root_client.get(f"{TEMPLATES}/{created['id']}").status_code == 404


def test_odinakovoe_nazvanie_otvergaetsya(root_client, maker):
    """Шаблон выбирают из списка глазами: два одинаковых имени — выбор наугад.

    Это же и защита от двойного нажатия из второй вкладки, куда засов на кнопке
    (`lib/guard.ts`) не достаёт.
    """
    maker("Напоминание об оплате ТЕСТ", "Напоминаем об оплате {deal_number}.")
    second = root_client.post(
        TEMPLATES,
        json={"name": "Напоминание об оплате ТЕСТ", "body": "Другой текст", "channel": "any"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "template_name_taken"


def test_kanal_otbiraet_shablony(root_client, maker):
    """Универсальный шаблон годится везде — он для того и заведён."""
    letter = maker("Только письмо ТЕСТ", "Текст письма", channel="email")
    note = maker("Только лента ТЕСТ", "Текст заметки", channel="note")
    both = maker("И то и другое ТЕСТ", "Общий текст", channel="any")

    for_email = {item["id"] for item in root_client.get(f"{TEMPLATES}?channel=email").json()["items"]}
    assert letter["id"] in for_email
    assert both["id"] in for_email, "универсальный шаблон не попал в выдачу по каналу"
    assert note["id"] not in for_email

    unknown = root_client.get(f"{TEMPLATES}?channel=telepathy")
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "unknown_channel"

    bad_save = root_client.post(
        TEMPLATES, json={"name": "Канал с опечаткой ТЕСТ", "body": "Текст", "channel": "emial"}
    )
    assert bad_save.status_code == 422
    assert bad_save.json()["error"]["code"] == "unknown_channel"


# --- 1. набор полей закрыт ---------------------------------------------------


def test_nabor_poley_obyavlen_v_kode_i_prihodit_s_servera(root_client):
    """Список полей отдаёт сервер: второй его экземпляр во фронтенде разошёлся
    бы с реестром на первом же новом поле, и заметить это было бы некому."""
    response = root_client.get(f"{TEMPLATES}/fields")
    assert response.status_code == 200, response.text
    keys = [item["key"] for item in response.json()["items"]]
    assert keys == [field.key for field in template_service.FIELDS]
    # У каждого поля сказано, что ему нужно: экран по этому объясняет прочерк
    # («шаблон просит заявку»), а не показывает поломку.
    assert {item["needs"] for item in response.json()["items"]} <= {"", "client", "deal"}


@pytest.mark.parametrize(
    "field",
    [
        # Деньги заявки: ровно то, ради чего набор и закрыт. Открытый набор
        # означает письмо клиенту с его же себестоимостью.
        "amount",
        "prepaid",
        "cost",
        # Внутренние поля, которые клиенту читать незачем.
        "lost_reason",
        "description",
        "client_phone",
        # Просто опечатка — самый частый случай из всех.
        "clietn_name",
    ],
)
def test_shablon_ne_vytaskivaet_togo_chego_net_v_nabore(root_client, field):
    """Чего нет в наборе — того не сохранить, а не «подставится пустым»."""
    assert field not in template_service.FIELDS_BY_KEY, "поле есть в наборе — проверка не о том"
    response = root_client.post(
        TEMPLATES,
        json={"name": f"Утечка {field} ТЕСТ", "body": "Сумма: {%s}" % field, "channel": "any"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_placeholder"
    assert field in response.json()["error"]["message"], "отказ не назвал виноватое поле"
    # И шаблона в самом деле не появилось: отказ, а не «сохранили и потом разберёмся».
    assert f"Утечка {field} ТЕСТ" not in [
        item["name"] for item in root_client.get(TEMPLATES).json()["items"]
    ]


def test_znachenie_polya_ne_beryotsya_po_imeni_iz_modeli(root_client, maker, client_with_deal):
    """Значения считает поимённый список, а не `getattr` по присланному имени.

    Проверка косвенная и другой быть не может: если бы значения брались с
    модели, поле модели, СОВПАДАЮЩЕЕ по имени с чем-то из набора, подставилось
    бы само. `client_name` в наборе есть, а колонки `name` у клиента — тоже; но
    `deal_title` подставляется из заявки, а не из клиента, у которого поля
    `title` нет вовсе. Совпади это со случайностью — тест бы этого не заметил;
    поэтому берём поле, которого нет ни у одной модели набора.
    """
    made = client_with_deal("Проверка полей ТЕСТ")
    template = maker("Все поля ТЕСТ", "{client_name} / {deal_title} / {deal_number}")
    answer = render(root_client, template["id"], deal_id=made["deal"]["id"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["text"] == (
        f"Проверка полей ТЕСТ / Ремонт витрины / #{made['deal']['id']}"
    )


# --- 2. неизвестная подстановка не молчит ------------------------------------


def test_neizvestnaya_podstanovka_ne_sohranyaetsya_molcha(root_client):
    """Главный рубеж: ошибку ловят там, где её сделали.

    Названы ВСЕ виноватые разом — иначе человеку пришлось бы сохранять шаблон
    столько раз, сколько он сделал опечаток.
    """
    response = root_client.post(
        TEMPLATES,
        json={
            "name": "Две опечатки ТЕСТ",
            "body": "Здравствуйте, {clietn_name}! Ваш заказ {order_no} готов.",
            "channel": "any",
        },
    )
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert "clietn_name" in message and "order_no" in message


def test_neizvestnoe_pole_v_predprosmotre_vidno_glazami(root_client, maker, monkeypatch):
    """Поле убрали из набора уже после того, как шаблон написали.

    Это единственный путь к неизвестной подстановке в готовом тексте — и самый
    неприятный: шаблон писали год назад, а замолчал он сегодня. Пустотой он
    стать не должен ни при каких обстоятельствах.
    """
    template = maker("Устаревшее поле ТЕСТ", "Ваша доска: {board_url}")
    monkeypatch.delitem(template_service.FIELDS_BY_KEY, "board_url")

    answer = render(root_client, template["id"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["unknown"] == ["board_url"]
    assert "[?board_url]" in body["text"], "поле исчезло из текста молча"
    assert body["text"] != "Ваша доска: "


# --- 3. законно пустое значение видно ----------------------------------------


def test_pustoe_znachenie_stanovitsya_procherkom_i_nazyvaetsya(
    root_client, maker, client_with_deal
):
    """У клиента нет фирмы — это не ошибка шаблона, но и не повод для дырки.

    Двух вещей мало по отдельности: прочерк без списка `missing` человек примет
    за опечатку в шаблоне, а список без прочерка не спасёт от «Фирма: » посреди
    письма.
    """
    made = client_with_deal("Частное лицо ТЕСТ", company="")
    template = maker("Фирма клиента ТЕСТ", "Фирма: {client_company}.")

    answer = render(root_client, template["id"], client_id=made["client"]["id"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["text"] == "Фирма: —."
    assert body["missing"] == ["client_company"]
    assert body["unknown"] == []


def test_zapolnennoe_pole_v_propavshie_ne_popadaet(root_client, maker, client_with_deal):
    """Обратная половина: список пустых не должен пугать там, где всё на месте."""
    made = client_with_deal("Фирма Есть ТЕСТ", company="ООО «Ромашка»")
    template = maker("Фирма заполнена ТЕСТ", "Фирма: {client_company}.")

    body = render(root_client, template["id"], client_id=made["client"]["id"]).json()
    assert body["text"] == "Фирма: ООО «Ромашка»."
    assert body["missing"] == []


def test_predprosmotr_bez_klienta_otvechaet_a_ne_otkazyvaet(root_client, maker):
    """«Как выглядит шаблон» — законный вопрос, и ответ на него не отказ."""
    template = maker("Без клиента ТЕСТ", "Здравствуйте, {client_name}!")
    answer = render(root_client, template["id"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["text"] == "Здравствуйте, —!"
    assert answer.json()["missing"] == ["client_name"]


def test_forma_otveta_ne_zavisit_ot_togo_povezlo_li_s_dannymi(
    root_client, maker, client_with_deal
):
    """`missing` и `unknown` приходят всегда, пустыми списками в том числе.

    Иначе экрану пришлось бы угадывать, какие ключи сегодня бывают, — ровно то
    же правило, что у выключенных блоков в сводке.
    """
    made = client_with_deal("Полные данные ТЕСТ", company="ООО «Всё есть»")
    template = maker("Форма ответа ТЕСТ", "{client_name}, {client_company}")
    body = render(root_client, template["id"], client_id=made["client"]["id"]).json()
    assert body["missing"] == [] and body["unknown"] == []
    assert set(body) >= {"text", "missing", "unknown", "template_id", "name", "channel"}


# --- 4. подставленное не управляет подстановкой ------------------------------


def test_podstavlennoe_ne_perechityvaetsya(root_client, maker, client_with_deal):
    """Клиент по имени «ООО {board_url}» не вытащит ссылку на доску.

    Подстановка идёт одним проходом: вставленное значение больше не
    просматривается. Иначе имя клиента — то есть текст, который вводит кто
    угодно, — управляло бы тем, что попадёт в письмо.
    """
    made = client_with_deal("ООО {board_url} ТЕСТ")
    template = maker("Перечитывание ТЕСТ", "Здравствуйте, {client_name}!")

    body = render(root_client, template["id"], deal_id=made["deal"]["id"]).json()
    assert body["text"] == "Здравствуйте, ООО {board_url} ТЕСТ!"
    assert "/b/" not in body["text"], "имя клиента вытащило чужое значение"
    # Ссылка при этом существует и подставляется, когда её просят по-настоящему.
    real = maker("Настоящая ссылка ТЕСТ", "{board_url}")
    assert made["share"]["url"] == render(
        root_client, real["id"], deal_id=made["deal"]["id"]
    ).json()["text"]


def test_podstavlennoe_ne_raskryvaetsya_kak_shablon_zameny(
    root_client, maker, client_with_deal
):
    """Обратная косая и угловые скобки в имени остаются собой.

    `re.sub` со СТРОКОЙ вместо функции раскрыл бы `\\1` и `\\g<0>` — то есть
    значение подставляло бы само себя куда захочет. Угловые скобки проверяются
    заодно: тело шаблона — обычный текст, и ломать его имя клиента не должно.
    """
    tricky = r"Иван \1 \g<0> <b>&amp; «Ко»"
    made = client_with_deal(tricky)
    template = maker("Спецсимволы ТЕСТ", "Здравствуйте, {client_name}!")

    body = render(root_client, template["id"], client_id=made["client"]["id"]).json()
    assert body["text"] == f"Здравствуйте, {tricky}!"


def test_podstanovka_ne_dopisyvaet_v_pismo_abzats(root_client, maker, client_with_deal):
    """Значение занимает ровно одну строку.

    Имя с переводами строк дописывало бы в письмо «P.S. переведите деньги на
    другой счёт» — от имени фирмы и в её же письме.
    """
    made = client_with_deal("Иван\n\nP.S. переведите деньги на счёт 000 ТЕСТ")
    template = maker("Одна строка ТЕСТ", "Здравствуйте, {client_name}!")

    body = render(root_client, template["id"], client_id=made["client"]["id"]).json()
    assert "\n" not in body["text"], "подстановка дописала в письмо новый абзац"
    assert body["text"] == (
        "Здравствуйте, Иван P.S. переведите деньги на счёт 000 ТЕСТ!"
    )


# --- значения полей ----------------------------------------------------------


def test_ssylka_na_dosku_beryotsya_zhivaya(root_client, maker, client_with_deal):
    """Отозванная ссылка не годится: она ведёт на страницу с отказом."""
    made = client_with_deal("Доска и ссылка ТЕСТ")
    template = maker("Ссылка на доску ТЕСТ", "Доска: {board_url}")

    body = render(root_client, template["id"], deal_id=made["deal"]["id"]).json()
    assert body["text"] == f"Доска: {made['share']['url']}"
    assert body["missing"] == []

    revoked = root_client.patch(
        f"{API}/shares/{made['share']['id']}", json={"is_active": False}
    )
    assert revoked.status_code == 200, revoked.text

    after = render(root_client, template["id"], deal_id=made["deal"]["id"]).json()
    assert after["text"] == "Доска: —"
    assert after["missing"] == ["board_url"]


def test_vyklyuchennyy_blok_dosok_ne_protekaet_v_shablon(
    root_client, maker, client_with_deal
):
    """Выключенный блок исчезает целиком — в том числе из чужого текста.

    Иначе «выключено» означало бы «не видно в меню», а ссылка на витрину
    продолжала бы уходить клиентам через шаблон.
    """
    made = client_with_deal("Доски выключены ТЕСТ")
    template = maker("Доска при выключенном блоке ТЕСТ", "Доска: {board_url}")
    assert render(root_client, template["id"], deal_id=made["deal"]["id"]).json()["missing"] == []

    assert switch(root_client, "boards", False).status_code == 200
    body = render(root_client, template["id"], deal_id=made["deal"]["id"]).json()
    assert body["text"] == "Доска: —"
    assert body["missing"] == ["board_url"]


def test_podpis_ne_ostayotsya_pustoy_bez_bloka_firm(root_client, maker):
    """Юрлица выключают те, у кого фирма одна и в бумагах не участвует.

    Подпись под письмом им нужна не меньше, поэтому последняя ступень — название
    бизнеса из настроек. Пустая подпись здесь была бы прочерком там, где ответ
    системе известен точно.
    """
    template = maker("Подпись ТЕСТ", "С уважением, {company_name}")
    brand = root_client.get(f"{API}/workspace").json()["brand_name"]

    assert switch(root_client, "companies", False).status_code == 200
    body = render(root_client, template["id"]).json()
    assert body["text"] == f"С уважением, {brand}"
    assert body["missing"] == []


def test_zayavka_chuzhogo_klienta_otvergaetsya(root_client, maker, client_with_deal):
    """Клиент получил бы письмо с чужим именем — молча предпочесть одного нельзя.

    Код тот же, что у письма и у звонка: одинаковая беда обязана отвечать
    одинаково, иначе интерфейсу приходится разбирать три кода про одно и то же.
    """
    one = client_with_deal("Первый Клиент ТЕСТ")
    two = client_with_deal("Второй Клиент ТЕСТ")
    template = maker("Чужая заявка ТЕСТ", "{client_name}: {deal_title}")

    mixed = render(
        root_client, template["id"], client_id=one["client"]["id"], deal_id=two["deal"]["id"]
    )
    assert mixed.status_code == 422, mixed.text
    assert mixed.json()["error"]["code"] == "deal_other_client"


def test_zayavka_nazyvaet_svoego_klienta_sama(root_client, maker, client_with_deal):
    """Заявку выбрал человек, а клиент у неё ровно один — спрашивать незачем."""
    made = client_with_deal("Клиент Из Заявки ТЕСТ")
    template = maker("Клиент из заявки ТЕСТ", "{client_name}")
    body = render(root_client, template["id"], deal_id=made["deal"]["id"]).json()
    assert body["text"] == "Клиент Из Заявки ТЕСТ"


# --- блок и права ------------------------------------------------------------


def test_gate_bloka_obyavlen_na_routere_a_ne_derzhitsya_na_pravah():
    """Блок закрывает раздел сам, а не через право.

    Проверка структурная нарочно, и вот почему её мало заменить проверкой
    поведения (она стоит следом). Отказ выключенного блока приходит и БЕЗ
    `require_module`: `require_perm` проверяет блок первым — таков порядок в
    `web/api/deps.py`. То есть поведенческий тест остаётся зелёным, даже если
    гейт с роутера снять, и снятие пройдёт незамеченным.

    Держаться на этом нельзя. Достаточно одного маршрута без права — у телефонии
    такой есть, вебхук АТС, — и раздел выключенного блока откроется. Гейт на
    роутере закрывает и те маршруты, которые допишут завтра.
    """
    from web.api.routes import templates as templates_routes

    gates = [
        getattr(depends.dependency, "__qualname__", "")
        for depends in templates_routes.router.dependencies
    ]
    assert any("require_module" in name for name in gates), (
        "роутер шаблонов не закрыт блоком: гейт держится на том, что у каждого "
        "маршрута есть право, а это совпадение, а не правило"
    )


def test_vyklyuchennyy_blok_zakryvaet_api(root_client, maker):
    """Спрятать пункт меню мало: адрес остаётся в закладках и в старых письмах."""
    template = maker("Закрытый блок ТЕСТ", "Текст")
    assert switch(root_client, "templates", False).status_code == 200

    for path in (
        "",
        "/fields",
        f"/{template['id']}",
        f"/{template['id']}/render",
    ):
        blocked = root_client.get(f"{TEMPLATES}{path}")
        assert blocked.status_code == 403, path
        assert blocked.json()["error"]["code"] == "module_disabled", path

    created = root_client.post(TEMPLATES, json={"name": "Мимо блока ТЕСТ", "body": "Текст"})
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "module_disabled"

    # Данные при этом на месте: выключение — «убрать с глаз», а не «стереть».
    assert switch(root_client, "templates", True).status_code == 200
    assert root_client.get(f"{TEMPLATES}/{template['id']}").json()["body"] == "Текст"


def test_chitat_shablony_i_pravit_ih_raznye_prava(root_client, maker):
    """Шаблоны видят все, кто пишет клиентам; правит их тот, кому дано право.

    Роль заводится прямо здесь, а не берётся из пресета: пресеты меняются, а
    правило — нет, и проверять надо правило.
    """
    role = root_client.post(
        ROLES, json={"name": "Только читает шаблоны ТЕСТ", "permissions": ["templates.view"]}
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]
    reader = make_manager(root_client, "templates-reader@test.local")
    user_id = next(
        u["id"]
        for u in root_client.get(f"{API}/staff").json()["items"]
        if u["email"] == "templates-reader@test.local"
    )
    assert root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role_id}).status_code == 200

    try:
        template = maker("Право на правку ТЕСТ", "Здравствуйте, {client_name}!")

        assert reader.get(TEMPLATES).status_code == 200
        assert reader.get(f"{TEMPLATES}/fields").status_code == 200
        assert reader.get(f"{TEMPLATES}/{template['id']}/render").status_code == 200

        for refused in (
            reader.post(TEMPLATES, json={"name": "Чужими руками ТЕСТ", "body": "Текст"}),
            reader.patch(f"{TEMPLATES}/{template['id']}", json={"name": "Переписал ТЕСТ"}),
            reader.delete(f"{TEMPLATES}/{template['id']}"),
        ):
            assert refused.status_code == 403, refused.text
            assert refused.json()["error"]["code"] == "permission_denied"
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{ROLES}/{role_id}")


# --- разбор присланного ------------------------------------------------------


def test_pustoe_telo_i_pustoe_nazvanie_otvergayutsya(root_client):
    empty_body = root_client.post(TEMPLATES, json={"name": "Без текста ТЕСТ", "body": "   "})
    assert empty_body.status_code == 422
    assert empty_body.json()["error"]["code"] == "body_required"

    empty_name = root_client.post(TEMPLATES, json={"name": "  ", "body": "Текст"})
    assert empty_name.status_code == 422
    assert empty_name.json()["error"]["code"] == "name_required"


def test_figurnaya_skobka_ne_pohozhaya_na_podstanovku_ostayotsya_tekstom(
    root_client, maker
):
    """`{ }` и `{"ключ": 1}` — обычный текст, а не ошибка сохранения.

    Подстановка — это скобки с именем внутри. Отвергать всё подряд значило бы
    запретить человеку написать фигурную скобку, которую он имел в виду
    буквально.
    """
    template = maker("Скобки ТЕСТ", 'Пришлите {"формат": "json"} и { } — {client_name}')
    body = render(root_client, template["id"]).json()
    assert body["text"] == 'Пришлите {"формат": "json"} и { } — —'
    assert body["unknown"] == []
