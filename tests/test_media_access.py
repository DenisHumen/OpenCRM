"""Файлы витрины закрываются теми же ручками, что и сама витрина.

Раньше — ни одной. Проверено прогоном: клиент один раз открыл витрину и
запомнил адреса картинок; после смены PIN, отзыва ссылки, истечения срока,
снятия доски с публикации и даже удаления ссылки страница отвечала 401/404, а
файлы — 200, причём анониму и вовсе без PIN.

Смягчало это одно: имя каталога — `token_hex(16)`, его не подобрать. То есть
беда была не «доступно всем», а «доступно навсегда тому, кому однажды
показали», — ровно то, от чего отзыв ссылки и заводят.

Каждая ручка проверяется отдельно и своим тестом. Один общий тест «после отзыва
не видно» пропустил бы четыре из пяти: сломать можно любую по отдельности, и
именно по отдельности их и правят.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from core.utils import now_utc
from tests.conftest import API, png_bytes
from web.main import app


@pytest.fixture
def showcase(manager_client):
    """Опубликованная доска с работой и ссылкой под PIN. Возвращает всё нужное."""
    board = manager_client.post(f"{API}/boards", json={"title": "Витрина с замком"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    ).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(
        f"{API}/boards/{board['id']}/shares", json={"pin": "1357"}
    ).json()

    ready = manager_client.get(f"{API}/boards/{board['id']}/works/{work['id']}").json()
    media = ready["media"]["thumb"]

    visitor = TestClient(app)
    entered = visitor.post(f"/b/{share['token']}/pin", data={"pin": "1357"},
                           follow_redirects=False)
    assert entered.status_code in (302, 303), entered.text
    assert visitor.get(media).status_code == 200, "клиент должен видеть файл, пока ссылка жива"

    return {
        "board": board,
        "share": share,
        "media": media,
        "visitor": visitor,
        "client": manager_client,
    }


def _revoke(showcase, payload: dict) -> None:
    """Закрыть доступ одной из ручек. Ответ проверяем: промах по адресу молча
    ничего бы не отозвал, и тест «после отзыва не видно» прошёл бы, не отозвав."""
    changed = showcase["client"].patch(f"{API}/shares/{showcase['share']['id']}", json=payload)
    assert changed.status_code == 200, changed.text


def test_a_stranger_never_sees_the_file(showcase):
    """Без пропуска — «нет такого». Не «нельзя»: это подтвердило бы, что работа есть."""
    stranger = TestClient(app)
    denied = stranger.get(showcase["media"])
    assert denied.status_code == 404, denied.text


def test_changing_the_pin_takes_the_file_back(showcase):
    """Сменили код — старый пропуск не годится ни для страницы, ни для файла."""
    _revoke(showcase, {"pin": "2468"})
    assert showcase["visitor"].get(showcase["media"]).status_code == 404


def test_revoking_the_link_takes_the_file_back(showcase):
    _revoke(showcase, {"is_active": False})
    assert showcase["visitor"].get(showcase["media"]).status_code == 404


def test_an_expired_link_takes_the_file_back(showcase):
    _revoke(showcase, {"expires_at": (now_utc() - timedelta(days=1)).isoformat()})
    assert showcase["visitor"].get(showcase["media"]).status_code == 404


def test_unpublishing_the_board_takes_the_file_back(showcase):
    showcase["client"].patch(
        f"{API}/boards/{showcase['board']['id']}", json={"is_published": False}
    )
    assert showcase["visitor"].get(showcase["media"]).status_code == 404


def test_deleting_the_link_takes_the_file_back(showcase):
    dropped = showcase["client"].delete(f"{API}/shares/{showcase['share']['id']}")
    assert dropped.status_code in (200, 204), dropped.text
    assert showcase["visitor"].get(showcase["media"]).status_code == 404


def test_a_link_without_a_pin_stays_open_to_everyone(manager_client):
    """Ссылка без кода — это приглашение всем, и картинка обязана открываться.

    Иначе сломалось бы превью ссылки в мессенджерах и почте: те приходят за
    картинкой без единой cookie.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Витрина без замка"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    ).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    manager_client.post(f"{API}/boards/{board['id']}/shares", json={})

    ready = manager_client.get(f"{API}/boards/{board['id']}/works/{work['id']}").json()
    media = ready["media"]["thumb"]

    assert TestClient(app).get(media).status_code == 200


def test_a_second_link_still_opens_its_own_invitation(manager_client):
    """У доски бывает несколько ссылок: закрыть одну из-за другой было бы неверно."""
    board = manager_client.post(f"{API}/boards", json={"title": "Две ссылки"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    ).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    first = manager_client.post(f"{API}/boards/{board['id']}/shares", json={"pin": "1111"}).json()
    second = manager_client.post(f"{API}/boards/{board['id']}/shares", json={"pin": "2222"}).json()

    ready = manager_client.get(f"{API}/boards/{board['id']}/works/{work['id']}").json()
    media = ready["media"]["thumb"]

    guest = TestClient(app)
    guest.post(f"/b/{second['token']}/pin", data={"pin": "2222"}, follow_redirects=False)
    assert guest.get(media).status_code == 200

    # Отозвали ПЕРВУЮ — второе приглашение это не касается.
    revoked = manager_client.patch(f"{API}/shares/{first['id']}", json={"is_active": False})
    assert revoked.status_code == 200, revoked.text
    assert guest.get(media).status_code == 200


def test_staff_see_the_files_of_any_board(manager_client, showcase):
    """Сотрудник в CRM видит работы всех досок — картинки не должны отставать.

    Право на доски здесь не спрашивается: экраны, где эта картинка показана,
    уже закрыты правами. Требовать право повторно на каждом файле значило бы
    получить рамки вместо изображений там, где сам экран открыт.
    """
    assert manager_client.get(showcase["media"]).status_code == 200


def test_the_original_is_never_served(showcase):
    """Наружу уходят только производные. Оригинал закрыт списком имён в коде.

    Раньше его закрывало отдельное правило в конфиге nginx — то есть знание
    жило в двух местах сразу и могло разъехаться молча.
    """
    from core.services import media_service

    assert "original.png" not in media_service.PUBLIC_FILENAMES
    uid = showcase["media"].split("/")[2]
    for name in ("original.png", "original.jpg", "original.webp"):
        denied = showcase["visitor"].get(f"/media/{uid}/{name}")
        assert denied.status_code == 404, name


def test_the_file_is_not_cached_by_shared_caches(showcase):
    """`private`, а не `public`: за файлом стоит проверка.

    Общий кэш — прокси провайдера, кэш в офисе — не должен раздавать его тем,
    кто эту проверку не проходил.
    """
    served = showcase["visitor"].get(showcase["media"])
    assert served.status_code == 200
    assert "private" in served.headers.get("Cache-Control", "")
    assert "public" not in served.headers.get("Cache-Control", "")
