import pytest
from fastapi.testclient import TestClient

from tests.conftest import API, login, register
from web.main import app


def test_register_pending_then_approve_then_login(root_client):
    anon = TestClient(app)
    response = register(anon, "Anna", "anna@test.local")
    assert response.status_code == 201
    user = response.json()["user"]
    assert user["status"] == "pending"

    # вход до одобрения запрещён
    blocked = login(TestClient(app), "anna@test.local", "manager-pass-123")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_pending"

    assert root_client.post(f"{API}/staff/{user['id']}/approve").status_code == 200

    ok = login(TestClient(app), "anna@test.local", "manager-pass-123")
    assert ok.status_code == 200
    assert ok.json()["role"] == "manager"


def test_register_duplicate_email(root_client):
    anon = TestClient(app)
    assert register(anon, "Bob", "bob@test.local").status_code == 201
    duplicate = register(anon, "Bob2", "bob@test.local")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_taken"


def test_register_weak_password():
    response = register(TestClient(app), "Weak", "weak@test.local", password="short")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "weak_password"


def test_login_wrong_password_and_rate_limit():
    email = "ratelimit@test.local"
    register(TestClient(app), "Rate", email)
    client = TestClient(app)
    for _ in range(5):
        response = login(client, email, "wrong-password-x")
        assert response.status_code == 401
    limited = login(client, email, "wrong-password-x")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "login_rate_limited"


def test_reject_registration(root_client):
    response = register(TestClient(app), "Rejected", "rejected@test.local")
    user_id = response.json()["user"]["id"]
    assert root_client.post(f"{API}/staff/{user_id}/reject").status_code == 200
    # аккаунт удалён — можно зарегистрироваться заново
    assert register(TestClient(app), "Rejected", "rejected@test.local").status_code == 201


def test_disable_kills_session(root_client, manager_client):
    from tests.conftest import make_manager

    victim = make_manager(root_client, "victim@test.local")
    victim_id = victim.get(f"{API}/auth/me").json()["id"]
    assert root_client.post(f"{API}/staff/{victim_id}/disable").status_code == 200
    # сессия отозвана немедленно
    assert victim.get(f"{API}/auth/me").status_code == 401
    # вход запрещён
    blocked = login(TestClient(app), "victim@test.local", "manager-pass-123")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_disabled"
    # enable возвращает доступ
    assert root_client.post(f"{API}/staff/{victim_id}/enable").status_code == 200
    assert login(TestClient(app), "victim@test.local", "manager-pass-123").status_code == 200


def test_staff_endpoints_root_only(manager_client):
    assert manager_client.get(f"{API}/staff").status_code == 403


def test_locale_saved_in_profile(root_client, manager_client):
    """Язык интерфейса — свойство аккаунта: свой у каждого, живёт в БД."""
    me = manager_client.get(f"{API}/auth/me").json()
    assert me["locale"] == "en"  # английский по умолчанию — и при первой установке тоже
    updated = manager_client.patch(f"{API}/auth/me", json={"locale": "ru"})
    assert updated.status_code == 200
    assert updated.json()["locale"] == "ru"
    # выбор сохранён в БД: новый вход с другого клиента видит ru
    fresh = TestClient(app)
    assert login(fresh, "manager@test.local", "manager-pass-123").json()["locale"] == "ru"
    # у соседа язык свой — переключение одного не задевает остальных
    assert root_client.get(f"{API}/auth/me").json()["locale"] == "en"
    bad = manager_client.patch(f"{API}/auth/me", json={"locale": "de"})
    assert bad.status_code == 422

    manager_client.patch(f"{API}/auth/me", json={"locale": "en"})


def test_password_reset_flow(root_client):
    from tests.conftest import make_manager

    make_manager(root_client, "resetme@test.local")
    staff = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in staff if u["email"] == "resetme@test.local")
    response = root_client.post(f"{API}/staff/{user_id}/reset-password")
    assert response.status_code == 200
    temp_password = response.json()["temp_password"]

    client = TestClient(app)
    assert login(client, "resetme@test.local", temp_password).status_code == 200
    # рабочие эндпоинты закрыты до смены
    assert client.get(f"{API}/clients").status_code == 403
    changed = client.post(
        f"{API}/auth/me/password",
        json={"old_password": temp_password, "new_password": "brand-new-pass-1"},
    )
    assert changed.status_code == 200
    assert client.get(f"{API}/clients").status_code == 200


def test_logout(root_client):
    from tests.conftest import make_manager

    client = make_manager(root_client, "logout@test.local")
    assert client.get(f"{API}/auth/me").status_code == 200
    assert client.post(f"{API}/auth/logout").status_code == 200
    assert client.get(f"{API}/auth/me").status_code == 401


def test_csrf_required_for_mutations(manager_client):
    # запрос с session-cookie, но без CSRF-заголовка — отклоняется
    headers = dict(manager_client.headers)
    headers.pop("X-CSRF-Token", None)
    response = manager_client.post(
        f"{API}/clients", json={"name": "CSRF Test"}, headers={"X-CSRF-Token": "wrong"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_double_click_on_request_access_is_a_conflict_not_a_crash():
    """Второе нажатие «Запросить доступ» отвечает «адрес занят», а не 500.

    Между проверкой «такой почты ещё нет» и вставкой есть окно, и попадают в
    него не злоумышленники, а обычное двойное нажатие: форма открыта в
    интернет, кнопка отправляет сразу. Живой прогон на настоящем сервере: из
    двенадцати одновременных заявок восемь отвечали ошибкой сервера — человек
    смотрел на успешно принятую заявку как на поломку и слал третью.

    Соседа изображаем: он заводит того же пользователя ровно между проверкой и
    вставкой. Настоящую параллельность `TestClient` не даёт — он в одном
    процессе, — а порядок событий тот же.
    """
    from core.services import auth_service
    from database.repositories import users as users_repo
    from database.session import SessionLocal

    email = "double-click@test.local"
    real_lookup = users_repo.get_by_email
    stolen: list[str] = []

    def free_then_taken(db, address):
        found = real_lookup(db, address)
        if found is None and address == email and not stolen:
            stolen.append(address)
            with SessionLocal() as neighbour:
                auth_service.register(neighbour, "Сосед", email, "neighbour-pass-123")
                neighbour.commit()
        return found

    users_repo.get_by_email = free_then_taken
    try:
        response = register(TestClient(app), "Двойное нажатие", email, "manager-pass-123")
    finally:
        users_repo.get_by_email = real_lookup

    assert stolen, "перехвата не случилось — тест ничего не проверил"
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "email_taken"


def test_povtornoe_chtenie_vidit_soseda():
    """Уровень изоляции — условие правильности, а не настройка.

    Приём «вставили, получили отказ, перечитали — правда ли занято»
    (`core/uniqueness.py`) держит на себе четыре места: регистрацию, название
    должности, артикул склада и номер бланка. При REPEATABLE READ перечитывание
    не видит соседа, зафиксировавшего строку, — и отказ базы уходит наверх
    пятисоткой там, где система отработала верно.

    Проверяется сам движок, а не поведение: уровень изоляции — свойство
    соединения, и назвать его может только сервер.
    """
    from database.session import engine

    if engine.dialect.name != "mysql":
        pytest.skip("уровень изоляции проверяется на MySQL")
    with engine.connect() as c:
        uroven = c.exec_driver_sql("SELECT @@transaction_isolation").scalar()
    assert uroven.replace("-", " ").upper() == "READ COMMITTED", uroven


# --- предел длины пароля -----------------------------------------------------


def test_dlinnyy_parol_ne_ronyaet_registratsiyu():
    """Пароль длиннее предела bcrypt получает отказ, а не пятисотую.

    `bcrypt` считает БАЙТЫ и с версии 4 не обрезает лишнее молча, а отказывает:
    «password cannot be longer than 72 bytes». Проверка длины при этом считала
    ЗНАКИ и только снизу, поэтому длинный пароль проходил её и падал уже внутри
    хэширования — то есть человек, придумавший хорошую длинную фразу, получал
    пятисотую вместо подсказки.

    Порог в знаках зависит от языка, и это главная неприятность: латиницей до
    него 73 знака, кириллицей — 37, потому что каждая буква весит два байта.
    Человек, пишущий пароль по-русски, упирался в предел вдвое раньше и ничего
    об этом не узнавал.
    """
    dlinnyy = "A" * 73
    otvet = register(TestClient(app), "Long", "long@test.local", password=dlinnyy)
    assert otvet.status_code == 422, "длинный пароль обязан получить отказ, а не 500"
    assert otvet.json()["error"]["code"] == "weak_password"


def test_predel_schitaetsya_v_baytakh_a_ne_v_znakakh():
    """Кириллический пароль упирается в предел на 37 знаках, и это тот же предел.

    Проверка отдельная, потому что ошибиться здесь можно молча в обе стороны:
    считая знаки, мы пропустим кириллическую фразу в bcrypt (пятисотая), а
    считая байты как знаки — запретим латинскую фразу, которая на самом деле
    помещается.
    """
    kirillitsey = "Пароль" * 7  # 42 знака, 84 байта
    assert len(kirillitsey) < 73, "опыт бессмыслен: фраза длинна и в знаках тоже"

    otvet = register(TestClient(app), "Кир", "kir@test.local", password=kirillitsey)
    assert otvet.status_code == 422, "кириллический пароль в 84 байта прошёл предел"

    vlezaet = "Пароль" * 6  # 36 знаков, 72 байта — ровно предел
    assert len("".join(vlezaet).encode()) == 72
    ok = register(TestClient(app), "Кир2", "kir2@test.local", password=vlezaet)
    assert ok.status_code == 201, "пароль ровно в предел обязан приниматься"
