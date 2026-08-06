"""Регрессии на закрытые уязвимости.

Главная: подмена X-Forwarded-For больше не создаёт новый бакет rate-limit
и не даёт обойти защиту подбора PIN.
"""
from starlette.requests import Request

from tests.conftest import API, login, make_manager, png_bytes
from web.api import deps
from web.main import app
from fastapi.testclient import TestClient


def _request(xff: str | None, peer: str = "203.0.113.7") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 5555)})


def test_client_ip_ignores_xff_without_proxy(monkeypatch):
    # trusted_proxy_hops = 0: заголовок клиента — не источник истины, берём TCP-пир
    monkeypatch.setattr(deps._settings, "trusted_proxy_hops", 0)
    assert deps.client_ip(_request("1.2.3.4")) == "203.0.113.7"
    assert deps.client_ip(_request("1.2.3.4, 5.6.7.8")) == "203.0.113.7"
    assert deps.client_ip(_request(None)) == "203.0.113.7"


def test_client_ip_uses_rightmost_behind_one_proxy(monkeypatch):
    # За одним nginx реальный адрес — последний элемент (его дописал nginx).
    # Всё, что слева, шлёт клиент и подделать может как угодно.
    monkeypatch.setattr(deps._settings, "trusted_proxy_hops", 1)
    assert deps.client_ip(_request("9.9.9.9, 203.0.113.7")) == "203.0.113.7"
    assert deps.client_ip(_request("spoof-a, spoof-b, 198.51.100.5")) == "198.51.100.5"


def test_client_ip_two_proxies(monkeypatch):
    monkeypatch.setattr(deps._settings, "trusted_proxy_hops", 2)
    # клиент, наш прокси-1, наш прокси-2 → доверяем 2-му с конца
    assert deps.client_ip(_request("evil, 203.0.113.7, 10.0.0.1")) == "203.0.113.7"


def test_default_trusted_proxy_hops_is_zero():
    # Fail-safe: без явной настройки XFF не доверяется вовсе.
    from config.settings import Settings
    assert Settings().trusted_proxy_hops == 0


def test_pin_bruteforce_not_bypassable_via_xff(manager_client):
    """Ротация X-Forwarded-For не должна давать новые попытки подбора PIN."""
    board = manager_client.post(
        f"{API}/boards", json={"title": "XFF bypass"}
    ).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"pin": "4821"}
    ).json()
    token = share["token"]

    visitor = TestClient(app)
    # исчерпываем лимит (5 неверных без заголовка)
    for _ in range(5):
        assert visitor.post(f"/b/{token}/pin", data={"pin": "0000"}).status_code == 401

    # теперь любые попытки с РАЗНЫМ X-Forwarded-For всё равно заблокированы,
    # включая верный PIN — обход закрыт
    for i in range(10):
        blocked = visitor.post(
            f"/b/{token}/pin",
            data={"pin": "0000"},
            headers={"X-Forwarded-For": f"9.9.{i}.{i}"},
        )
        assert blocked.status_code == 429, f"XFF {i} обошёл лимит"

    assert visitor.post(
        f"/b/{token}/pin",
        data={"pin": "4821"},
        headers={"X-Forwarded-For": "9.9.100.100"},
        follow_redirects=False,
    ).status_code == 429


def test_password_change_revokes_other_sessions(root_client):
    """Смена пароля выкидывает угнанную сессию, текущую сохраняет."""
    session_a = make_manager(root_client, "revoke@test.local", "manager-pass-123")

    # второй вход тем же аккаунтом — как если бы cookie утекли злоумышленнику
    session_b = TestClient(app)
    assert login(session_b, "revoke@test.local", "manager-pass-123").status_code == 200
    assert session_b.get(f"{API}/auth/me").status_code == 200

    changed = session_a.post(
        f"{API}/auth/me/password",
        json={"old_password": "manager-pass-123", "new_password": "brand-new-pass-1"},
    )
    assert changed.status_code == 200

    # старая (чужая) сессия отозвана, текущая жива
    assert session_b.get(f"{API}/auth/me").status_code == 401
    assert session_a.get(f"{API}/auth/me").status_code == 200


def test_a_huge_json_body_is_refused_before_it_is_read(manager_client):
    """Толстое тело отсекается по объявленному размеру, а не после разбора.

    nginx пропускает 220 МБ — иначе не загрузить файл, — и ровно столько же
    принимала форма запроса доступа: открытая в интернет и не требующая входа.
    Замер на стенде: одно тело в 38 МБ стоило процессу 76 МБ памяти, и Python
    возвращает её системе неохотно. Десяток запросов подряд — и на VPS с
    гигабайтом приложение убивает ядро, не оставив в логе ни одной ошибки.

    Проверяем именно отказ до чтения: сервер обязан ответить по заголовку
    `Content-Length`, а не разбирать присланное, чтобы затем сказать «слишком
    длинное имя».
    """
    from web.middleware import MAX_JSON_BODY

    fat = "ы" * (MAX_JSON_BODY + 1024)
    refused = manager_client.post(f"{API}/clients", json={"name": fat})

    assert refused.status_code == 413, refused.text
    assert refused.json()["error"]["code"] == "body_too_large"

    # Обычный запрос той же формы проходит: потолок про размер, а не про форму.
    fine = manager_client.post(f"{API}/clients", json={"name": "Обычный клиент"})
    assert fine.status_code == 201, fine.text


def test_the_cap_does_not_touch_file_uploads(manager_client):
    """Загрузка файла меряется своей меркой, а не потолком JSON.

    Потолок в мегабайт стоит только на `application/json`. Спутай его с
    загрузкой — и разом отвалится всё, ради чего в nginx стоит 220 МБ.
    """
    from web.middleware import MAX_JSON_BODY

    board = manager_client.post(f"{API}/boards", json={"title": "Для тяжёлого файла"}).json()
    heavy = png_bytes() + b"\x00" * (MAX_JSON_BODY + 4096)
    uploaded = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("big.png", heavy, "image/png")},
    )

    assert uploaded.status_code == 202, uploaded.text


def test_public_links_from_settings_cannot_carry_a_script(root_client):
    """Ссылки соцсетей и сайта проверяются так же, как адрес студии.

    `javascript:alert(1)` в поле «Телеграм» доезжал до атрибута href публичной
    витрины и страницы закрытой ссылки. Экранирование Jinja2 схему не трогает, а
    CSP витрины разрешает inline-скрипты — получался хранимый XSS по клиентам
    студии, на том же домене, что и CRM, от любой учётки с правом на настройки.
    """
    from core.services.settings_service import URL_SETTINGS

    for key in URL_SETTINGS:
        refused = root_client.patch(
            f"{API}/settings", json={"values": {key: "javascript:alert(document.domain)"}}
        )
        assert refused.status_code == 422, f"{key}: {refused.text}"
        assert refused.json()["error"]["code"] == "bad_site_url", key

        # Обычный адрес по-прежнему принимается — проверка не мешает работе.
        fine = root_client.patch(
            f"{API}/settings", json={"values": {key: "https://example.com/studio"}}
        )
        assert fine.status_code == 200, f"{key}: {fine.text}"

    root_client.patch(
        f"{API}/settings",
        json={"values": {key: "" for key in URL_SETTINGS}},
    )


def test_a_picture_cannot_smuggle_a_script(root_client):
    """SVG чистится, и чистится не по одному написанию.

    Проверка на образцах, которые прежний вариант пропускал: обработчик без
    кавычек, обработчик у вложенного тега, схема, записанная числовой ссылкой.
    Это второй рубеж — первый заголовки (`sandbox` в CSP и у приложения, и у
    nginx), — но рубеж, который обещан комментарием, обязан работать.
    """
    from core.services.media_service import sanitize_svg

    dangerous = [
        b"<svg onload=alert(1)></svg>",
        b"<svg><animate onbegin=alert(1) /></svg>",
        b'<svg><a xlink:href="javas&#99;ript:alert(1)">x</a></svg>',
        b'<svg><a xlink:href="javas&#x63;ript:alert(1)">x</a></svg>',
        b"<svg><a href=javascript:alert(1)>x</a></svg>",
        b'<svg><a href=" javascript:alert(1)">x</a></svg>',
        b'<svg onload="alert(1)"></svg>',
        b"<svg><script>alert(1)</script></svg>",
        b'<svg><a href="data:text/html,<script>alert(1)</script>">x</a></svg>',
    ]
    for raw in dangerous:
        assert b"alert" not in sanitize_svg(raw), raw

    # И обратная сторона: безобидное не портим.
    keep = b'<svg><image href="data:image/png;base64,AAA"/><a href="https://example.com">ok</a></svg>'
    cleaned = sanitize_svg(keep)
    assert b"https://example.com" in cleaned
    assert b"data:image/png" in cleaned


def test_the_uploaded_logo_is_cleaned_too(root_client):
    """Логотип чистится наравне с работами: /branding/logo.svg открывают прямо."""
    import pathlib

    from config.settings import get_settings

    poisoned = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    uploaded = root_client.post(
        f"{API}/settings/logo", files={"file": ("logo.svg", poisoned, "image/svg+xml")}
    )
    assert uploaded.status_code == 201, uploaded.text

    saved = pathlib.Path(get_settings().branding_dir) / "logo.svg"
    assert saved.exists(), "логотип не сохранился"
    assert b"<script" not in saved.read_bytes(), "скрипт остался в логотипе на диске"
