"""Длинные работы: лонгриды и инфографика на витрине.

Отдельной сетки у них больше нет — лонгрид занимает такое же место композиции,
как любая другая работа, и показывает выбранный менеджером фрагмент. Производные
при этом ограничиваются по ширине (иначе от лонгрида 1:10 в `card.webp` осталось
бы ~80px ширины).
"""

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.services import media_service
from tests.conftest import API, png_bytes
from web.main import app
from web.public.layout import cropped_indexes


# --- производные: ужимаем ширину, а не длину ---

def test_derive_keeps_width_of_a_long_image():
    """У картинки 1:10 обычный thumbnail оставил бы 80px ширины — сплошное мыло."""
    variant = media_service.derive(Image.new("RGB", (1000, 10000)), media_service.SIZE_CARD)
    assert variant.width == media_service.SIZE_CARD
    assert variant.height == media_service.SIZE_CARD * 10


def test_derive_fits_a_normal_image_into_the_box():
    variant = media_service.derive(Image.new("RGB", (1600, 1000)), media_service.SIZE_CARD)
    assert max(variant.size) == media_service.SIZE_CARD


def test_derive_never_upscales():
    """Маленький оригинал остаётся собой: растянутый пиксель хуже мелкой картинки."""
    variant = media_service.derive(Image.new("RGB", (120, 900)), media_service.SIZE_CARD)
    assert variant.size == (120, 900)


def test_derive_respects_the_webp_side_limit():
    variant = media_service.derive(Image.new("RGB", (400, 20000)), media_service.SIZE_LARGE)
    assert variant.height <= media_service.WEBP_MAX_SIDE
    assert variant.width > 0


@pytest.mark.parametrize(
    "size, long",
    [((640, 480), False), ((800, 1200), False), ((900, 1600), False), ((600, 1500), True), ((400, 4000), True)],
)
def test_long_image_threshold(size, long):
    """Портрет 2:3 и сторис 9:16 длинными не считаются — порог выше."""
    assert media_service.is_long_image(*size) is long


def test_cropped_indexes_ignores_video_and_unknown_sizes():
    works = [
        {"kind": "image", "width": 400, "height": 4000},
        {"kind": "video", "width": 400, "height": 4000},
        {"kind": "image", "width": None, "height": None},
        {"kind": "image", "width": 900, "height": 1600},  # 9:16 — в место влезает
    ]
    assert cropped_indexes(works) == {0}


# --- srcset: плитка не должна тянуть `card`, когда мала́ ---

class _Work:
    """Минимальная работа для чистых функций media_service."""

    def __init__(self, width, height, uid="uid", kind="image", mime="image/webp"):
        self.width, self.height = width, height
        self.work_uid, self.kind, self.mime = uid, kind, mime


def test_srcset_offers_the_larger_file_for_a_long_image():
    """Место в композиции бывает шире 800px — иначе `card` там растягивается."""
    srcset = media_service.work_srcset(_Work(1097, 6707))
    assert srcset == "/media/uid/card.webp 800w, /media/uid/large.webp 1097w"


def test_srcset_skips_a_duplicate_candidate():
    """У мелкого оригинала обе производные одного размера — второй кандидат лишний."""
    assert media_service.work_srcset(_Work(736, 736)) == "/media/uid/card.webp 736w"


def test_srcset_is_empty_for_video_and_svg():
    assert media_service.work_srcset(_Work(720, 1280, kind="video", mime="video/mp4")) == ""
    assert media_service.work_srcset(_Work(400, 400, mime="image/svg+xml")) == ""


@pytest.mark.parametrize("size", [(1600, 1000), (1097, 6707), (120, 900), (4000, 4000)])
def test_derived_size_matches_what_derive_actually_produces(size):
    """Ширины в srcset считаются без открытия файла — они обязаны совпадать."""
    image = Image.new("RGB", size)
    for box in (media_service.SIZE_CARD, media_service.SIZE_LARGE):
        assert media_service.derived_size(*size, box) == media_service.derive(image, box).size


# --- витрина целиком ---

def test_long_and_short_works_share_one_composition(manager_client):
    """Раньше лонгриды уезжали в свою сетку, и доска рвалась на два модуля."""
    board = manager_client.post(f"{API}/boards", json={"title": "Смешанная"}).json()
    for name, size in (("long.png", (400, 2400)), ("wide.png", (640, 480))):
        upload = manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": (name, png_bytes(size=size), "image/png")},
        )
        assert upload.status_code == 202, upload.text
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert page.count('class="module"') == 1
    assert page.count('class="tile is-long"') == 1
    assert page.count('class="tile"') == 1
    # обрезка прикрыта размытием, и видно, что работу можно открыть целиком
    assert page.count('<div class="glass">') == 1
    assert page.count('class="more"') == 1
    assert "View full" in page


@pytest.mark.parametrize("count", range(1, 8))
def test_a_board_of_long_works_is_one_composition(manager_client, count):
    """Пять лонгридов — одна композиция, а не лента из двух модулей."""
    board = manager_client.post(f"{API}/boards", json={"title": f"Лонгриды {count}"}).json()
    for index in range(count):
        upload = manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": (f"{index}.png", png_bytes(size=(400, 2400)), "image/png")},
        )
        assert upload.status_code == 202, upload.text
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert page.count('class="module"') == 1
    assert page.count('class="tile is-long"') == count


# --- выбор фрагмента ---

def _long_work(manager_client, size=(400, 4000)) -> tuple[dict, dict]:
    board = manager_client.post(f"{API}/boards", json={"title": "Обрезка"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("long.png", png_bytes(size=size), "image/png")},
    ).json()
    return board, work


def test_fragment_defaults_to_the_top(manager_client):
    _board, work = _long_work(manager_client)
    assert work["preview_focus"] is None


def test_manager_chooses_the_visible_fragment(manager_client):
    board, work = _long_work(manager_client)
    updated = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}", json={"preview_focus": 0.5}
    )
    assert updated.status_code == 200
    assert updated.json()["preview_focus"] == 0.5


def test_fragment_is_clamped_to_the_work(manager_client):
    board, work = _long_work(manager_client)
    path = f"{API}/boards/{board['id']}/works/{work['id']}"
    assert manager_client.patch(path, json={"preview_focus": 4}).json()["preview_focus"] == 1.0
    assert manager_client.patch(path, json={"preview_focus": -1}).json()["preview_focus"] == 0.0


def test_fragment_resets_to_the_top(manager_client):
    board, work = _long_work(manager_client)
    path = f"{API}/boards/{board['id']}/works/{work['id']}"
    manager_client.patch(path, json={"preview_focus": 0.5})
    assert manager_client.patch(path, json={"preview_focus": None}).json()["preview_focus"] is None


def test_short_work_has_nothing_to_choose(manager_client):
    """Обычная картинка помещается в своё место целиком — окно двигать негде."""
    board = manager_client.post(f"{API}/boards", json={"title": "Короткая"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("wide.png", png_bytes(size=(640, 480)), "image/png")},
    ).json()
    rejected = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}", json={"preview_focus": 0.5}
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "not_a_long_work"


def test_showcase_shows_the_chosen_fragment(manager_client):
    board, work = _long_work(manager_client)
    manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}", json={"preview_focus": 0.5}
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert "--focus: 50.0%;" in page


def test_editor_knows_the_shape_of_every_place(manager_client):
    """Рамка фрагмента в CRM обязана совпасть с местом на витрине."""
    board, _work = _long_work(manager_client)
    for index in range(3):
        manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": (f"more{index}.png", png_bytes(size=(400, 4000)), "image/png")},
        )
    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    ratios = [w["place_ratio"] for w in detail["works"]]
    assert len(ratios) == 4
    assert all(ratio > 0 for ratio in ratios)
