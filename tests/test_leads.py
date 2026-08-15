"""Заявки с сайта: приём, дубли и всё, чем закрыта публичная ручка.

Ручка открыта в интернет и пишет в базу — то есть каждая защита здесь не
украшение, а единственное, что стоит между формой и справочником клиентов.
Поэтому на каждую из них своя проверка, и каждая из них была прогнана без
защиты: без потолка тела 413 не приходит, без ограничителя 429 не приходит, без
ловушки бот заводит карточку, без `lower()` заявка цепляется к чужому клиенту.
"""

import pytest
from fastapi.testclient import TestClient

from core.services import lead_service
from database.repositories import settings as settings_repo
from database.session import SessionLocal
from tests.conftest import API, make_manager
from web.main import app
from web.public import leads as leads_route

LEADS = "/api/v1/public/leads"


@pytest.fixture(scope="module", autouse=True)
def mounted():
    """Роутер приёма на время файла — пока он не подключён по-настоящему.

    Тем же приёмом и по тому же образцу, что у блока денег
    (`tests/test_finance.py`): подключаем в фикстуре, а не при импорте, и
    снимаем после. Разница принципиальная — перебор маршрутов в
    `test_route_guards` собирает список при сборе тестов, то есть ДО первой
    фикстуры. Смонтируй мы роутер импортом, и перебор судил бы не приложение, а
    временный шов из чужого файла; а судить он должен ровно то, что уезжает на
    сервер.

    Подключён по-настоящему (строка в `web/main.py` появилась) — не трогаем
    вовсе: второе подключение задвоило бы маршрут.
    """
    if any(getattr(route, "path", "") == LEADS for route in app.routes):
        yield
        return
    # Ловец SPA стоит последним, но забирает только GET и HEAD, — POST до него
    # не доходит, и восстанавливать порядок, как в тестах денег, не нужно.
    tail = len(app.routes)
    app.include_router(leads_route.router)
    added = list(app.routes[tail:])
    yield
    for route in added:
        app.routes.remove(route)


@pytest.fixture
def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture(scope="module")
def intake_key(base_client) -> str:
    """Ключ приёма. Задаётся сервисом: своей ручки настроек у него пока нет."""
    db = SessionLocal()
    try:
        key = lead_service.regenerate_intake_key(db)
        db.commit()
    finally:
        db.close()
    yield key
    db = SessionLocal()
    try:
        lead_service.clear_intake_key(db)
        lead_service.set_manager(db, None)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def fresh_limiter():
    """Ограничитель живёт в памяти процесса и общий на все проверки.

    Не сбрось его — и десятая проверка в файле упирается в потолок, набранный
    девятью предыдущими, а падение выглядит как поломка приёма.
    """
    leads_route.lead_limiter._attempts.clear()
    yield
    leads_route.lead_limiter._attempts.clear()


def send(key: str | None, **fields):
    """Заявка так, как её шлёт сервер сайта: без cookie, ключ в заголовке."""
    headers = {} if key is None else {lead_service.INTAKE_KEY_HEADER: key}
    return TestClient(app).post(LEADS, json=fields, headers=headers)


def clients_named(root_client, needle: str) -> list[dict]:
    """Карточки, у которых имя или адрес — ровно `needle`.

    Точное сравнение поверх поиска не придирчивость: поиск по строке с цифрой
    заодно смотрит в нормализованный телефон, и «chastota-3@example.com» находит
    всех, у кого в номере есть тройка.
    """
    response = root_client.get(f"{API}/clients", params={"search": needle})
    assert response.status_code == 200, response.text
    return [card for card in response.json()["items"] if needle in (card["email"], card["name"])]


def deals_of(root_client, client_id: int) -> list[dict]:
    response = root_client.get(f"{API}/deals", params={"client_id": client_id})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def make_client(root_client, **fields) -> dict:
    response = root_client.post(f"{API}/clients", json=fields)
    assert response.status_code == 201, response.text
    return response.json()


# --- приём ---------------------------------------------------------------

def test_zayavka_zavodit_lida_i_rabotu_na_pervom_etape(root_client, intake_key):
    response = send(
        intake_key,
        name="Первый Заявитель",
        email="pervyj.zayavitel@example.com",
        phone="+38 (067) 000-11-22",
        message="Нужен ремонт ноутбука, не включается",
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "accepted"}

    found = clients_named(root_client, "pervyj.zayavitel@example.com")
    assert len(found) == 1, "карточка клиента не появилась"
    card = found[0]
    # «Лид» в системе — это метка плюс источник: отдельной колонки статуса нет.
    assert card["tags"] == [lead_service.LEAD_TAG]
    assert card["source"] == lead_service.SOURCE_SITE
    assert card["email"] == "pervyj.zayavitel@example.com"

    deals = deals_of(root_client, card["id"])
    assert len(deals) == 1, "заявка не завелась"
    deal = deals[0]
    # Первый открытый этап воронки — тот, что настроен у бизнеса, а не зашитый.
    stages = root_client.get(f"{API}/pipeline/stages").json()["items"]
    first_open = next(s["key"] for s in stages if s["kind"] == "open")
    assert deal["stage"] == first_open
    assert "Нужен ремонт ноутбука" in deal["title"]


def test_otvet_ne_rasskazyvaet_o_baze(root_client, intake_key):
    """Тело ответа одинаковое всегда — и на новом клиенте, и на известном.

    Иначе форма превращается в способ проверить, есть ли такой человек в базе:
    прислал адрес, посмотрел на ответ — узнал.
    """
    first = send(intake_key, email="odinakovyj.otvet@example.com", message="раз")
    second = send(intake_key, email="odinakovyj.otvet@example.com", message="два")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"status": "accepted"}


def test_zayavka_bez_svyazi_otvergaetsya(root_client, intake_key):
    """Ни почты, ни телефона — перезванивать некому, карточка выйдет пустой."""
    response = send(intake_key, name="Аноним Безмолвный", message="перезвоните мне")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "contact_required"
    assert clients_named(root_client, "Аноним Безмолвный") == []


def test_krivoj_adres_ne_dohodit_do_bazy(root_client, intake_key):
    """Адрес либо годный, либо отказ. Обрезать и «чинить» его нельзя:
    по испорченному заявка не найдёт своего клиента и заведёт второго."""
    response = send(intake_key, name="Кривой Адрес", email="ne-adres-vovse")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "bad_email"
    assert clients_named(root_client, "Кривой Адрес") == []


# --- дубли ---------------------------------------------------------------

def test_izvestnaya_pochta_ceplyaetsya_k_svoemu_klientu(root_client, intake_key):
    """Заявка с известного адреса не заводит второго клиента.

    Регистр при этом не важен: человек пишет свой адрес как придётся, а
    сравнение идёт через `lower()`.
    """
    existing = make_client(
        root_client, name="Постоянный Клиент", email="Postoyannyj@Example.COM"
    )
    response = send(
        intake_key, name="Совсем Другое Имя", email="postoyannyj@example.com", message="снова я"
    )
    assert response.status_code == 202, response.text

    assert clients_named(root_client, "Совсем Другое Имя") == [], "завелась вторая карточка"
    deals = deals_of(root_client, existing["id"])
    assert len(deals) == 1, "заявка не прицепилась к известному клиенту"

    # Карточку известного клиента форма не правит: знание чужого адреса не
    # должно давать права переписать в ней имя.
    card = root_client.get(f"{API}/clients/{existing['id']}").json()
    assert card["name"] == "Постоянный Клиент"


def test_adres_sravnivaetsya_celikom_a_ne_shablonom(root_client, intake_key):
    """`_` и `%` в адресе — обычные знаки, а не шаблон LIKE.

    Присланный адрес попадает в запрос как искомое значение, и подмена
    `lower()` на `ilike` превращает его в шаблон: `a_b@…` совпал бы с чужим
    `axb@…`, а `%@…` — вообще со всеми на домене. Выглядит такая правка
    безобидно, а стоит чужой переписки в чужой карточке.
    """
    chuzhoj = make_client(root_client, name="Похожий Клиент", email="axb@shablon.example")
    response = send(intake_key, name="Подчёркнутый Заявитель", email="a_b@shablon.example")
    assert response.status_code == 202, response.text

    svoya = clients_named(root_client, "a_b@shablon.example")
    assert len(svoya) == 1, "заявка не завела свою карточку, а прицепилась к чужой"
    assert deals_of(root_client, chuzhoj["id"]) == [], "заявка уехала в чужую карточку"

    # То же со знаком «%»: под шаблоном он совпал бы с кем угодно на домене.
    procent = send(intake_key, name="Процентный Заявитель", email="%@shablon.example")
    assert procent.status_code == 202, procent.text
    assert deals_of(root_client, chuzhoj["id"]) == [], "«%» сработал как шаблон"


def test_izvestnyj_nomer_ceplyaetsya_k_svoemu_klientu(root_client, intake_key):
    """Номер, записанный по-человечески, и номер из формы — один клиент."""
    settings = root_client.patch(
        f"{API}/settings", json={"values": {"default_country_code": "380"}}
    )
    assert settings.status_code == 200, settings.text
    try:
        existing = make_client(
            root_client, name="Телефонный Заявитель", phone="+38 (067) 555-33-11"
        )
        response = send(intake_key, name="Он Же Другим Именем", phone="0675553311")
        assert response.status_code == 202, response.text

        assert clients_named(root_client, "Он Же Другим Именем") == [], "завелась вторая карточка"
        assert len(deals_of(root_client, existing["id"])) == 1
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"default_country_code": ""}})


def test_dvojnoe_nazhatie_ne_zavodit_vtoruyu_zayavku(root_client, intake_key):
    """Две одинаковые отправки подряд — одно обращение, одна работа на доске."""
    fields = {
        "name": "Нетерпеливый Заявитель",
        "email": "neterpelivyj@example.com",
        "message": "страница долго думала",
    }
    assert send(intake_key, **fields).status_code == 202
    assert send(intake_key, **fields).status_code == 202

    card = clients_named(root_client, "neterpelivyj@example.com")
    assert len(card) == 1
    assert len(deals_of(root_client, card[0]["id"])) == 1, "второе нажатие завело вторую заявку"


# --- защиты --------------------------------------------------------------

def test_lovushka_molcha_ne_zavodit_nichego(root_client, intake_key):
    """Заполненное поле-ловушка: ответ как у успеха, в базе — ничего.

    Отвечать отказом нельзя: подбирающий за десяток попыток вычислил бы, какое
    поле его выдаёт, и перестал бы его трогать.
    """
    honest = send(intake_key, name="Честный Человек", email="chestnyj@example.com")
    trapped = send(
        intake_key,
        name="Бот Заполняющий",
        email="bot@example.com",
        website="http://spam.example",
    )
    assert trapped.status_code == honest.status_code
    assert trapped.json() == honest.json()
    assert clients_named(root_client, "bot@example.com") == [], "бот завёл карточку"


def test_chastota_ogranichena(intake_key, monkeypatch, root_client):
    """С одного адреса нельзя стучаться бесконечно.

    Ограничитель подменяем на маленький: проверка про то, что он вообще
    спрашивается, а не про то, каковы сегодня числа в константах.
    """
    from core.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(leads_route, "lead_limiter", SlidingWindowLimiter(2, 60))
    assert send(intake_key, email="chastota-1@example.com").status_code == 202
    assert send(intake_key, email="chastota-2@example.com").status_code == 202

    blocked = send(intake_key, email="chastota-3@example.com")
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["error"]["code"] == "lead_rate_limited"
    assert clients_named(root_client, "chastota-3@example.com") == []


def test_telo_bolshe_potolka_otvergaetsya_do_chteniya(intake_key, root_client):
    """Огромное тело отсекается по заголовку длины, ДО разбора.

    «До чтения» проверяется не на слово: если бы запрос доехал до ручки, её
    ограничитель отметил бы обращение. Он не отметил ни одного — значит тело
    даже не начали разбирать.
    """
    from web.middleware import MAX_JSON_BODY

    assert leads_route.lead_limiter.tracked() == 0
    response = send(intake_key, name="Толстяк", message="ш" * (MAX_JSON_BODY + 1024))
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "body_too_large"
    assert leads_route.lead_limiter.tracked() == 0, "запрос всё-таки доехал до ручки"
    assert clients_named(root_client, "Толстяк") == []


def test_bez_klyucha_ne_pryamo(intake_key, root_client):
    """Чужой ключ и отсутствующий ключ — отказ, и в базе ничего."""
    for key in (None, "", "sovsem-ne-tot-klyuch"):
        response = send(key, name="Безключевой", email="bezklyucha@example.com")
        assert response.status_code == 401, (key, response.text)
        assert response.json()["error"]["code"] == "bad_intake_key"
    assert clients_named(root_client, "bezklyucha@example.com") == []


def test_klyuch_s_ne_ascii_ne_ronyaet_server(intake_key, root_client):
    """Заголовки декодируются как latin-1: не-ASCII в ключе давал бы 500.

    Байт 0xFF — то, что можно послать в заголовке по-настоящему (кириллица в
    заголовок не влезает вовсе, её не пропустит уже отправитель). Сравнение
    `compare_digest` на таком бросает TypeError, и без проверки на ASCII любой
    из интернета одной строкой получал бы ошибку сервера.
    """
    response = TestClient(app).post(
        LEADS,
        json={"email": "latin1@example.com"},
        # Байтами, а не строкой: отправитель строку с таким знаком в заголовок
        # не запишет, а по проводу этот байт приходит без затруднений.
        headers={lead_service.INTAKE_KEY_HEADER: b"klyuch-\xff"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "bad_intake_key"


def test_bez_nastroennogo_klyucha_ruchki_net(root_client, intake_key):
    """Свежая установка не держит открытой ручку, о которой владелец не знает."""
    db = SessionLocal()
    try:
        lead_service.clear_intake_key(db)
        db.commit()
    finally:
        db.close()
    try:
        response = send("hot-by-kakoj-nibud", name="Ранний Гость", email="rannij@example.com")
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "intake_not_configured"
        assert clients_named(root_client, "rannij@example.com") == []
    finally:
        # Ключ общий на весь файл: не вернёшь — соседние проверки уедут на
        # «приём не настроен» и будут искать причину не там.
        db = SessionLocal()
        try:
            settings_repo.write(db, lead_service.SETTING_INTAKE_KEY, intake_key)
            db.commit()
        finally:
            db.close()


def test_potolok_novyh_kartochek(intake_key, monkeypatch, root_client):
    """Поток с разных адресов не превращает справочник в свалку.

    Ограничитель по адресу этого не ловит: у ботнета каждый запрос первый со
    своего адреса. Потолок считает по базе, а не по памяти процесса.
    """
    monkeypatch.setattr(lead_service, "MAX_NEW_CLIENTS_PER_HOUR", 0)
    response = send(intake_key, name="Лишний В Потоке", email="lishnij@example.com")
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "lead_intake_flooded"
    assert clients_named(root_client, "lishnij@example.com") == []


# --- уведомление ответственному ------------------------------------------

def test_zayavka_stavit_napominanie_otvetstvennomu(root_client, intake_key):
    """Ответственный получает напоминание — событием, а не прямым вызовом."""
    otvetstvennyj = make_manager(root_client, "leads-manager@test.local")
    me = otvetstvennyj.get(f"{API}/auth/me").json()
    db = SessionLocal()
    try:
        lead_service.set_manager(db, me["id"])
        db.commit()
    finally:
        db.close()

    try:
        assert send(intake_key, name="Ждущий Звонка", email="zhdushchij@example.com").status_code == 202
        card = clients_named(root_client, "zhdushchij@example.com")[0]
        tasks = root_client.get(f"{API}/tasks", params={"client_id": card["id"]}).json()["items"]
        assert len(tasks) == 1, "напоминание не завелось"
        assert tasks[0]["assignee_id"] == me["id"]
        # Карточка тоже достаётся ответственному, а не тому, кто первым откроет.
        assert card["manager_id"] == me["id"]
    finally:
        db = SessionLocal()
        try:
            lead_service.set_manager(db, None)
            db.commit()
        finally:
            db.close()


def test_bez_bloka_napominanij_zayavka_vse_ravno_prinimaetsya(root_client, intake_key):
    """Выключенный блок напоминаний не отменяет приём заявки.

    Подписчик просто не зовётся — проверки `is_enabled` внутри сервиса для
    этого не нужно, её делает диспетчер событий.
    """
    off = root_client.post(f"{API}/modules/tasks", json={"enabled": False})
    assert off.status_code == 200, off.text
    try:
        response = send(intake_key, name="Без Напоминаний", email="bez-zadach@example.com")
        assert response.status_code == 202, response.text
        card = clients_named(root_client, "bez-zadach@example.com")
        assert len(card) == 1, "заявка не завелась без блока напоминаний"
        assert len(deals_of(root_client, card[0]["id"])) == 1
    finally:
        back = root_client.post(f"{API}/modules/tasks", json={"enabled": True})
        assert back.status_code == 200, back.text


# --- одновременность ---------------------------------------------------------


def test_dvoynaya_dostavka_ne_zavodit_dve_kartochki(intake_key, session):
    """Сервер сайта повторил доставку — карточка и заявка обязаны быть одни.

    Случай назван в самом коде: `DOUBLE_SUBMIT_SECONDS` заведён под то, что
    «сайт повторил доставку после таймаута». Но защита стояла в ветке ИНАЧЕ —
    то есть работала только для УЖЕ известного клиента. Для нового её не было
    вовсе: оба запроса делали `find_client`, оба получали `None` (блокировать
    нечего — строки ещё нет, а gap-блокировок под READ COMMITTED не бывает),
    оба заводили карточку и оба — заявку.

    Итог в справочнике: две карточки с одним адресом почты и две одинаковые
    карточки на доске. Дальше они расходятся ещё сильнее — почта садится на ту,
    что с меньшим id (`database/repositories/mail.py`, `order_by(Client.id)`), а
    звонки на ту, которую правили позже (`telephony.py`, `updated_at.desc()`),
    то есть история одного человека делится надвое молча.

    Утверждение — про ИНВАРИАНТ, а не про то, кто выиграл: гонку никто не
    обязан выигрывать, и требовать этого значит завести мигающий тест.
    """
    from sqlalchemy import func, select

    from database.models.client import Client
    from tests.test_odin_iz_mnogih import duel

    pochta = "dvoynik@example.org"
    telo = {"name": "Двойник", "email": pochta, "message": "Сделайте сайт"}

    ishody = duel(lambda _: send(intake_key, **telo).status_code, None, None)

    session.rollback()  # читаем то, что зафиксировали чужие сессии
    kartochek = session.scalar(
        select(func.count()).select_from(Client).where(Client.email == pochta)
    )
    assert kartochek == 1, (
        f"карточек с одним адресом стало {kartochek}, исходы ударов: {ishody}. "
        "Справочник раздвоился на обычном повторе доставки"
    )

    klient_id = session.scalar(select(Client.id).where(Client.email == pochta))
    from database.models.deal import Deal

    zayavok = session.scalar(
        select(func.count()).select_from(Deal).where(Deal.client_id == klient_id)
    )
    assert zayavok == 1, (
        f"заявок по одному обращению стало {zayavok} — менеджер сделает работу дважды"
    )
