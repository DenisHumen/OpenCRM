"""Длинные работы: лонгриды и инфографика на витрине.

У них свои композиции с местами 1:3.4 — на этой высоте работа и обрезается.
Производные при этом ограничиваются по ширине (иначе от лонгрида 1:10 в
`card.webp` осталось бы ~80px ширины).
"""

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.services import media_service
from tests.conftest import API, png_bytes
from web.main import app
from web.public.layout import (
    LONG_MODULES,
    MAX_LONG_MODULE,
    TALL_RATIO,
    build_modules,
    long_indexes,
    split_long_counts,
)


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


# --- длинные композиции: тот же почерк, но места 1:3.4 ---

@pytest.mark.parametrize("size", range(1, MAX_LONG_MODULE + 1))
def test_long_preset_has_a_tile_per_work(size):
    _ratio, tiles = LONG_MODULES[size]
    assert len(tiles) == size


@pytest.mark.parametrize("size", range(1, MAX_LONG_MODULE + 1))
def test_every_long_place_is_exactly_three_point_four_widths(size):
    """Смысл длинной композиции: место ровно 1:3.4, там и проходит обрезка.

    h% рамки / (w% рамки · ratio) = отношение сторон места.
    """
    ratio, tiles = LONG_MODULES[size]
    for tile in tiles:
        assert tile.h / (tile.w * ratio) == pytest.approx(TALL_RATIO, abs=0.01)


@pytest.mark.parametrize("size", range(1, MAX_LONG_MODULE + 1))
def test_long_preset_fills_its_frame(size):
    _ratio, tiles = LONG_MODULES[size]
    assert min(t.x for t in tiles) == 0
    assert min(t.y for t in tiles) == 0
    assert max(t.x + t.w for t in tiles) == pytest.approx(100, abs=0.05)
    assert max(t.y + t.h for t in tiles) == pytest.approx(100, abs=0.05)


@pytest.mark.parametrize("size", range(2, MAX_LONG_MODULE + 1))
def test_long_preset_tiles_overlap_a_neighbour_with_distinct_depth(size):
    """Перекрытие — тот же почерк, что у обычных композиций."""
    _ratio, tiles = LONG_MODULES[size]

    def overlap(a, b) -> bool:
        return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h

    for i, tile in enumerate(tiles):
        assert any(overlap(tile, other) for j, other in enumerate(tiles) if i != j)
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            if overlap(a, b):
                assert a.z != b.z


@pytest.mark.parametrize("total", range(1, 20))
def test_split_long_covers_every_work(total):
    chunks = split_long_counts(total)
    assert sum(chunks) == total
    assert all(1 <= c <= MAX_LONG_MODULE for c in chunks)


def test_split_long_avoids_a_stray_single_tail():
    assert split_long_counts(4) == [4]
    assert split_long_counts(5) == [3, 2]
    assert split_long_counts(6) == [4, 2]
    assert split_long_counts(8) == [4, 4]
    assert split_long_counts(9) == [4, 3, 2]


# --- порядок работ и смешанные доски ---

def test_long_works_group_into_their_own_composition():
    """Три длинные работы — одна композиция из трёх колонок, а не три модуля."""
    modules = build_modules(3, {0, 1, 2})
    assert len(modules) == 1
    assert modules[0]["long"] is True
    assert [tile["index"] for tile in modules[0]["tiles"]] == [0, 1, 2]


def test_mixed_board_keeps_the_order_of_works():
    modules = build_modules(6, {2, 3})
    assert [module["long"] for module in modules] == [False, True, False]
    assert [[t["index"] for t in m["tiles"]] for m in modules] == [[0, 1], [2, 3], [4, 5]]


@pytest.mark.parametrize("total", range(1, 24))
def test_every_work_is_placed_once_in_order(total):
    longs = {index for index in range(total) if index % 3 == 0}
    modules = build_modules(total, longs)
    assert [t["index"] for m in modules for t in m["tiles"]] == list(range(total))


def test_long_indexes_ignores_video_and_unknown_sizes():
    works = [
        {"kind": "image", "width": 400, "height": 4000},
        {"kind": "video", "width": 400, "height": 4000},
        {"kind": "image", "width": None, "height": None},
        {"kind": "image", "width": 600, "height": 1500},  # 1:2.5 — в обычное место влезает
    ]
    assert long_indexes(works) == {0}


# --- витрина целиком ---

def test_showcase_renders_a_long_work_with_glass_and_a_hint(manager_client):
    board = manager_client.post(f"{API}/boards", json={"title": "Лонгрид"}).json()
    for name, size in (("long.png", (400, 2400)), ("wide.png", (640, 480))):
        upload = manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": (name, png_bytes(size=size), "image/png")},
        )
        assert upload.status_code == 202, upload.text
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    # длинная работа — своя композиция, обычная — своя, порядок сохранён
    assert page.count('class="module"') == 2
    assert page.count('class="tile is-long"') == 1
    assert page.count('class="tile"') == 1
    # обрезка прикрыта размытием, и видно, что работу можно открыть целиком
    assert page.count('<div class="glass">') == 1
    assert page.count('class="more"') == 1
    assert "View full" in page
