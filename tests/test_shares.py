from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.conftest import API, png_bytes
from web.main import app


def _published_board(client, title="Витрина"):
    board = client.post(f"{API}/boards", json={"title": title, "description": "Подборка работ"}).json()
    upload = client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("work.png", png_bytes(), "image/png")},
    )
    assert upload.status_code == 202
    client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    return board


def _share(client, board_id, **kwargs):
    response = client.post(f"{API}/boards/{board_id}/shares", json=kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def test_public_showcase_and_view_counter(manager_client):
    board = _published_board(manager_client)
    share = _share(manager_client, board["id"])

    visitor = TestClient(app)
    page = visitor.get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "Витрина" in page.text
    assert "og:image" in page.text
    assert "card.webp" in page.text

    # повторный визит
    visitor.get(f"/b/{share['token']}")

    views = manager_client.get(f"{API}/shares/{share['id']}/views").json()
    assert views["views_count"] == 2
    assert views["last_viewed_at"] is not None

    # data-эндпоинт
    data = visitor.get(f"/b/{share['token']}/data").json()
    assert data["board"]["title"] == "Витрина"
    assert len(data["works"]) == 1
    assert data["works"][0]["media"]["large"].endswith("large.webp")


def test_unpublished_board_is_closed(manager_client):
    board = _published_board(manager_client, "Черновик")
    share = _share(manager_client, board["id"])
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": False})
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 404


def test_revoke_and_reactivate(manager_client):
    board = _published_board(manager_client, "Отзыв")
    share = _share(manager_client, board["id"])
    visitor = TestClient(app)
    assert visitor.get(f"/b/{share['token']}").status_code == 200

    revoked = manager_client.patch(f"{API}/shares/{share['id']}", json={"is_active": False})
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert visitor.get(f"/b/{share['token']}").status_code == 404

    manager_client.patch(f"{API}/shares/{share['id']}", json={"is_active": True})
    assert visitor.get(f"/b/{share['token']}").status_code == 200


def test_expired_link(manager_client):
    board = _published_board(manager_client, "Истёкшая")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    share = _share(manager_client, board["id"], expires_at=past)
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 404

    # продлеваем — открывается
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    manager_client.patch(f"{API}/shares/{share['id']}", json={"expires_at": future})
    assert TestClient(app).get(f"/b/{share['token']}").status_code == 200


def test_pin_flow(manager_client):
    board = _published_board(manager_client, "Защищённая")
    share = _share(manager_client, board["id"], pin="4821")
    assert share["has_pin"] is True

    visitor = TestClient(app)
    page = visitor.get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert "pin" in page.text.lower()
    assert "Защищённая" not in page.text  # PIN-страница не раскрывает доску
    assert "card.webp" not in page.text  # и обложку — даже в OG (допустима только брендовая заглушка)

    wrong = visitor.post(f"/b/{share['token']}/pin", data={"pin": "0000"})
    assert wrong.status_code == 401

    right = visitor.post(f"/b/{share['token']}/pin", data={"pin": "4821"}, follow_redirects=False)
    assert right.status_code == 303

    opened = visitor.get(f"/b/{share['token']}")
    assert opened.status_code == 200
    assert "Защищённая" in opened.text

    # другой посетитель без PIN-cookie всё ещё видит форму
    stranger = TestClient(app)
    assert "Защищённая" not in stranger.get(f"/b/{share['token']}").text


def test_pin_longer_than_four_digits(manager_client):
    # PIN разрешён 4–8 цифр; экран ввода не должен отправлять код на 4-й цифре
    board = _published_board(manager_client, "Длинный PIN")
    share = _share(manager_client, board["id"], pin="123456")

    visitor = TestClient(app)
    assert "Длинный PIN" not in visitor.get(f"/b/{share['token']}").text

    # префикс верного кода доступа не даёт
    assert visitor.post(f"/b/{share['token']}/pin", data={"pin": "1234"}).status_code == 401

    ok = visitor.post(f"/b/{share['token']}/pin", data={"pin": "123456"}, follow_redirects=False)
    assert ok.status_code == 303
    assert "Длинный PIN" in visitor.get(f"/b/{share['token']}").text


def test_pin_rate_limit(manager_client):
    board = _published_board(manager_client, "PIN лимит")
    share = _share(manager_client, board["id"], pin="7777")
    visitor = TestClient(app)
    for _ in range(5):
        assert visitor.post(f"/b/{share['token']}/pin", data={"pin": "1111"}).status_code == 401
    limited = visitor.post(f"/b/{share['token']}/pin", data={"pin": "7777"})
    assert limited.status_code == 429


def test_bad_pin_validation(manager_client):
    board = _published_board(manager_client, "PIN валидация")
    response = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"pin": "12ab"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_pin"


def test_regenerate_kills_old_token(manager_client):
    board = _published_board(manager_client, "Перегенерация")
    share = _share(manager_client, board["id"])
    visitor = TestClient(app)
    assert visitor.get(f"/b/{share['token']}").status_code == 200

    regen = manager_client.post(f"{API}/shares/{share['id']}/regenerate")
    assert regen.status_code == 200
    new = regen.json()
    assert new["token"] != share["token"]

    assert visitor.get(f"/b/{share['token']}").status_code == 404
    assert visitor.get(f"/b/{new['token']}").status_code == 200


def test_remove_pin(manager_client):
    board = _published_board(manager_client, "Снятие PIN")
    share = _share(manager_client, board["id"], pin="5555")
    visitor = TestClient(app)
    assert "Снятие PIN" not in visitor.get(f"/b/{share['token']}").text

    manager_client.patch(f"{API}/shares/{share['id']}", json={"pin": None})
    assert "Снятие PIN" in visitor.get(f"/b/{share['token']}").text


def test_multiple_links_per_board(manager_client):
    board = _published_board(manager_client, "Несколько ссылок")
    a = _share(manager_client, board["id"])
    b = _share(manager_client, board["id"], pin="9999")
    listing = manager_client.get(f"{API}/boards/{board['id']}/shares").json()["items"]
    assert {link["id"] for link in listing} >= {a["id"], b["id"]}
    # отзыв одной не трогает другую
    manager_client.patch(f"{API}/shares/{a['id']}", json={"is_active": False})
    visitor = TestClient(app)
    assert visitor.get(f"/b/{a['token']}").status_code == 404
    assert visitor.get(f"/b/{b['token']}").status_code == 200


def test_an_expired_link_closes_on_time_not_hours_later(manager_client):
    """Срок ссылки считается в UTC, а не в зоне сервера.

    Срок приводился к местной зоне, а сравнивался с UTC — ссылка жила дольше
    заявленного ровно на смещение. На машине UTC+3 закрытая «в 15:00» ссылка
    работала до 18:00. Тихо: ни в логе, ни в интерфейсе не видно. Прежний тест
    брал запас в сутки и трёх часов не замечал — здесь запас в полчаса.
    """
    from datetime import datetime, timedelta, timezone

    from core.utils import now_utc

    board = manager_client.post(f"{API}/boards", json={"title": "Доска со сроком"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})

    half_hour_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    link = manager_client.post(
        f"{API}/boards/{board['id']}/shares",
        json={"expires_at": half_hour_ago.isoformat()},
    ).json()

    assert TestClient(app).get(f"/b/{link['token']}").status_code == 404, (
        "просроченная полчаса назад ссылка всё ещё открывается"
    )

    # И обратная сторона: живая ссылка не должна закрыться раньше времени.
    later = datetime.now(timezone.utc) + timedelta(minutes=30)
    alive = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"expires_at": later.isoformat()}
    ).json()
    assert TestClient(app).get(f"/b/{alive['token']}").status_code == 200


def test_changing_the_pin_closes_the_door_for_everyone(manager_client):
    """Смена кода отзывает выданные пропуска.

    Пропуск был подписанной строкой без метки времени и без связи с самим
    кодом: скопировал значение — доступ навсегда, а «сменить PIN» не отрезало
    никого. Единственным способом закрыть доступ оставался отзыв ссылки, то
    есть кнопка обещала не то, что делала.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Доска с кодом"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    link = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"pin": "1234"}
    ).json()

    guest = TestClient(app)
    assert guest.get(f"/b/{link['token']}").status_code == 200        # страница ввода кода
    entered = guest.post(f"/b/{link['token']}/pin", data={"pin": "1234"}, follow_redirects=False)
    assert entered.status_code == 303, entered.text
    assert guest.get(f"/b/{link['token']}/data").status_code == 200, "код не пустил"

    # Владелец сменил код — старый пропуск обязан перестать работать.
    assert manager_client.patch(f"{API}/shares/{link['id']}", json={"pin": "9999"}).status_code == 200
    assert guest.get(f"/b/{link['token']}/data").status_code == 401, (
        "после смены кода старый пропуск всё ещё открывает доску"
    )

    # А новый код пускает.
    again = guest.post(f"/b/{link['token']}/pin", data={"pin": "9999"}, follow_redirects=False)
    assert again.status_code == 303
    assert guest.get(f"/b/{link['token']}/data").status_code == 200


def test_the_showcase_does_not_ship_internal_fields(manager_client):
    """Клиенту студии не уходит ни имя файла, ни размер, ни счётчики.

    Имя файла на диске студии — это обычно фамилия клиента, сумма или внутренняя
    пометка («Иванов_Смета_180000грн», «правки_после_скандала_v3»). Менеджер
    правит видимую подпись работы и не подозревает, что рядом уезжает исходное
    имя: витрина встраивала весь ответ сериализатора в HTML страницы.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Витрина без лишнего"}).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    uploaded = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("Иванов_Смета_180000грн.png", png_bytes(), "image/png")},
    )
    assert uploaded.status_code == 202, uploaded.text
    link = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{link['token']}")
    assert page.status_code == 200
    assert "Иванов_Смета" not in page.text, "исходное имя файла уехало на публичную страницу"
    assert "original_name" not in page.text
    assert "size_bytes" not in page.text

    data = TestClient(app).get(f"/b/{link['token']}/data").json()
    for work in data["works"]:
        assert "original_name" not in work, work.keys()
        assert "size_bytes" not in work
        assert "id" not in work, "внутренний счётчик работ виден снаружи"
        # А то, ради чего витрина существует, на месте.
        assert "media" in work and "title" in work


def _svoy_predel_ssylki(monkeypatch, skolko):
    import uuid

    from core.ratelimit import SlidingWindowLimiter
    from web.public import routes as public_routes

    limiter = SlidingWindowLimiter(skolko, 3600, name=f"pin_link-{uuid.uuid4().hex}")
    monkeypatch.setattr(public_routes, "pin_ssylka_limiter", limiter)
    return limiter


def test_pin_obshchiy_predel_ssylki_so_vsekh_adresov(manager_client, monkeypatch):
    """Разбор 29.09.2026: счёт шёл на пару «ссылка + адрес», и у кого адресов сотня,
    у того пятьсот попыток на четыре цифры. Теперь у ссылки есть общий предел."""
    _svoy_predel_ssylki(monkeypatch, 3)
    board = _published_board(manager_client, "Общий предел")
    share = _share(manager_client, board["id"], pin="5813")
    for nomer in range(3):
        chuzhoy = TestClient(app, client=(f"198.51.100.{nomer + 1}", 40000))
        assert chuzhoy.post(f"/b/{share['token']}/pin", data={"pin": "0000"}).status_code == 401
    novyy = TestClient(app, client=("198.51.100.77", 40000))
    otvet = novyy.post(f"/b/{share['token']}/pin", data={"pin": "0000"})
    assert otvet.status_code == 429, "новый адрес получил свежий запас попыток"


def test_pin_udacha_ne_tratit_predel_ssylki(manager_client, monkeypatch):
    """Клиенты с верным PIN не съедают общий предел — иначе витрину, которую открыли
    трижды, запирало бы для четвёртого."""
    _svoy_predel_ssylki(monkeypatch, 2)
    board = _published_board(manager_client, "Удача не в счёт")
    share = _share(manager_client, board["id"], pin="2468")
    for nomer in range(4):
        klient = TestClient(app, client=(f"203.0.113.{nomer + 1}", 40000))
        otvet = klient.post(f"/b/{share['token']}/pin", data={"pin": "2468"}, follow_redirects=False)
        assert otvet.status_code == 303, f"{nomer + 1}-й клиент с верным PIN не вошёл"


def test_pin_ipv6_odna_set_64_odin_schyot(manager_client):
    """Разбор 29.09.2026: одной машине провайдер даёт целую /64, и счёт по адресу
    давал ей бесконечный запас попыток — каждая шла с нового адреса."""
    board = _published_board(manager_client, "IPv6")
    share = _share(manager_client, board["id"], pin="9173")
    for nomer in range(5):
        adres = TestClient(app, client=(f"2001:db8:5:6::{nomer + 1:x}", 40000))
        assert adres.post(f"/b/{share['token']}/pin", data={"pin": "0000"}).status_code == 401
    ta_zhe_set = TestClient(app, client=("2001:db8:5:6:ffff::1", 40000))
    assert ta_zhe_set.post(f"/b/{share['token']}/pin", data={"pin": "0000"}).status_code == 429
    sosed = TestClient(app, client=("2001:db8:5:7::1", 40000))
    assert sosed.post(f"/b/{share['token']}/pin", data={"pin": "0000"}).status_code == 401


def test_klyuch_adresa_skleivaet_set_64():
    from core.security import tokens

    k = tokens.klyuch_adresa
    assert k("2001:db8:1:2::1") == k("2001:db8:1:2:abcd::9")
    assert k("2001:db8:1:2::1") != k("2001:db8:1:3::1")
    assert k("::ffff:198.51.100.5") == k("198.51.100.5")
    assert k("198.51.100.5") != k("198.51.100.6")
    assert k("testclient") == tokens.hash_ip("testclient")

