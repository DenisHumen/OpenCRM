"""Модульная сетка витрины: инварианты композиций и разбиения на модули."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API, png_bytes
from web.main import app
from web.public.layout import MAX_MODULE, MODULES, build_modules, split_counts


@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_preset_has_a_tile_per_work(size):
    _ratio, tiles = MODULES[size]
    assert len(tiles) == size


@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_preset_fills_its_frame(size):
    """Композиция занимает рамку целиком — иначе по краям будет пустое поле."""
    _ratio, tiles = MODULES[size]
    assert min(t.x for t in tiles) == 0
    assert min(t.y for t in tiles) == 0
    assert max(t.x + t.w for t in tiles) == 100
    assert max(t.y + t.h for t in tiles) == 100


@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_preset_tiles_stay_inside_frame(size):
    _ratio, tiles = MODULES[size]
    for tile in tiles:
        assert tile.w > 0 and tile.h > 0
        assert 0 <= tile.x and tile.x + tile.w <= 100
        assert 0 <= tile.y and tile.y + tile.h <= 100


@pytest.mark.parametrize("size", range(2, MAX_MODULE + 1))
def test_every_tile_overlaps_a_neighbour(size):
    """Перекрытие — суть композиции: одиночная плитка «в стороне» ломала бы её."""
    _ratio, tiles = MODULES[size]

    def overlap(a, b) -> bool:
        return (
            a.x < b.x + b.w and b.x < a.x + a.w
            and a.y < b.y + b.h and b.y < a.y + a.h
        )

    for i, tile in enumerate(tiles):
        assert any(overlap(tile, other) for j, other in enumerate(tiles) if i != j)


@pytest.mark.parametrize("size", range(2, MAX_MODULE + 1))
def test_overlapping_tiles_have_distinct_depth(size):
    """У перекрывающихся плиток разный z — иначе порядок наложения случаен."""
    _ratio, tiles = MODULES[size]
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            overlaps = (
                a.x < b.x + b.w and b.x < a.x + a.w
                and a.y < b.y + b.h and b.y < a.y + a.h
            )
            if overlaps:
                assert a.z != b.z


@pytest.mark.parametrize("total", range(1, 40))
def test_split_covers_every_work(total):
    chunks = split_counts(total)
    assert sum(chunks) == total
    assert all(1 <= c <= MAX_MODULE for c in chunks)


def test_split_avoids_a_stray_tail():
    """Хвост в 1–2 работы добирается из предыдущего модуля."""
    assert split_counts(7) == [7]
    assert split_counts(8) == [4, 4]
    assert split_counts(9) == [5, 4]
    assert split_counts(14) == [7, 7]
    assert split_counts(15) == [7, 4, 4]
    assert split_counts(16) == [7, 5, 4]
    for total in range(3, 40):
        assert min(split_counts(total)) >= 3


def test_split_of_empty_board():
    assert split_counts(0) == []
    assert build_modules(0) == []


@pytest.mark.parametrize("total", range(1, 30))
def test_build_modules_places_each_work_once_in_order(total):
    modules = build_modules(total)
    indices = [tile["index"] for module in modules for tile in module["tiles"]]
    assert indices == list(range(total))


@pytest.mark.parametrize("total", range(1, 30))
def test_built_tiles_stay_inside_frame_even_mirrored(total):
    for module in build_modules(total):
        assert module["ratio"] > 0
        for tile in module["tiles"]:
            assert 0 <= tile["x"] and tile["x"] + tile["w"] <= 100
            assert 0 <= tile["y"] and tile["y"] + tile["h"] <= 100


def test_second_module_is_mirrored():
    """Одинаковые модули подряд читались бы как повтор шаблона."""
    modules = build_modules(14)
    first = [(t["x"], t["w"]) for t in modules[0]["tiles"]]
    second = [(t["x"], t["w"]) for t in modules[1]["tiles"]]
    assert first != second
    assert second == [(round(100 - x - w, 2), w) for x, w in first]


def test_single_work_fills_the_whole_frame():
    """Одна работа — крупный кадр без пустого пространства вокруг."""
    (tile,) = build_modules(1)[0]["tiles"]
    assert (tile["x"], tile["y"], tile["w"], tile["h"]) == (0, 0, 100, 100)


# --- витрина целиком: связка «роут → шаблон» ---

@pytest.mark.parametrize("works_count, modules_count", [(1, 1), (3, 1), (7, 1), (10, 2)])
def test_showcase_renders_a_tile_per_work(manager_client, works_count, modules_count):
    board = manager_client.post(
        f"{API}/boards", json={"title": f"Сетка {works_count}"}
    ).json()
    for _ in range(works_count):
        upload = manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": ("work.png", png_bytes(), "image/png")},
        )
        assert upload.status_code == 202, upload.text
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}")
    assert page.status_code == 200
    assert page.text.count('class="tile"') == works_count
    assert page.text.count('class="module"') == modules_count
    # каждая работа рендерится ровно один раз (старая masonry дублировала DOM ×3)
    for index in range(works_count):
        assert page.text.count(f'data-index="{index}"') == 1
