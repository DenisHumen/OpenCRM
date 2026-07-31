"""Модульная сетка витрины: инварианты композиций и разбиения на модули."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API, png_bytes
from web.main import app
from web.public.layout import (
    FIRST_ROW,
    MASTER,
    MASTER_FRAME,
    MAX_MODULE,
    MODULE_ROWS,
    MODULE_SPANS,
    MODULES,
    SINGLE_INDEX,
    SINGLE_ZOOM,
    build_modules,
    place_ratios,
    split_counts,
)


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


# --- одна композиция на все размеры доски ---

def _place(size: int, index: int) -> tuple[float, float]:
    """Размер места в долях колонки: так его видно на экране при любом size."""
    ratio, tiles = MODULES[size]
    span = MODULE_SPANS[size]
    tile = tiles[index]
    return tile.w / 100 * span, tile.h / 100 * span / ratio


@pytest.mark.parametrize("index", range(MAX_MODULE))
def test_a_place_keeps_its_size_whatever_the_board_size(index):
    """Смысл мастер-композиции: убрали работу — соседние не поехали и не выросли.

    Раньше под каждое число работ была своя раскладка, и одно и то же место
    у доски на 6 и на 7 работ отличалось по размеру.
    """
    sizes = [size for size in range(2, MAX_MODULE + 1) if size > index]
    reference = _place(sizes[0], index)
    for size in sizes[1:]:
        width, height = _place(size, index)
        assert width == pytest.approx(reference[0], abs=0.002)
        assert height == pytest.approx(reference[1], abs=0.002)


@pytest.mark.parametrize("size", range(2, MAX_MODULE + 1))
def test_smaller_board_keeps_the_places_of_the_bigger_one(size):
    """Композиция на N работ — первые N мест мастера, без пересчёта."""
    _ratio, tiles = MODULES[size]
    _bigger_ratio, bigger = MODULES[MAX_MODULE]
    assert [t.z for t in tiles] == [t.z for t in bigger[:size]]


def test_a_board_of_seven_fills_the_column():
    """Полная композиция занимает колонку целиком, меньшие — соответственно у́же."""
    assert MODULE_SPANS[MAX_MODULE] == 1.0
    assert MODULE_SPANS[2] < 1.0


def test_single_work_takes_the_showiest_place_enlarged():
    """Одинокая работа не должна попасть в узкое вертикальное место мастера."""
    place = MASTER[SINGLE_INDEX]
    single_ratio, _tiles = MODULES[1]
    # то же место: отношение сторон совпадает с его формой в мастере
    assert single_ratio == pytest.approx(place.w / place.h, abs=0.01)
    # и оно крупнее, чем было бы внутри композиции
    assert MODULE_SPANS[1] == pytest.approx(
        place.w / MASTER_FRAME[0] * SINGLE_ZOOM, abs=0.002
    )
    # ровно настолько, насколько крупнее на макете: 992px при колонке 1568px
    assert MODULE_SPANS[1] == pytest.approx(992 / 1568, abs=0.002)


# --- совпадение с макетами (tmp/image/template) ---

# Рамки макетов `tmp/image/template/Image_N.jpg` в пикселях, снятые по цветам.
# Колонка на всех макетах одна — 1568px, отсюда и доля ширины у каждой рамки.
# Тройку когда-то занесли как 1568×820: `Image_3.jpg` — снимок экрана, а не
# страницы целиком, и нижнее место оказалось срезано краем окна.
MOCKUP_FRAMES = {
    1: (992, 740),
    2: (1192, 780),
    3: (1568, 848),
    4: (1568, 1424),
    5: (1568, 1648),
    6: (1568, 1648),
    7: (1568, 2064),
}


@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_frame_matches_the_mockup(size):
    """Рамка композиции — ровно та, что на макетах, снятых по пикселям."""
    width, height = MOCKUP_FRAMES[size]
    assert MODULES[size][0] == pytest.approx(width / height, abs=0.005)


# Места мастера, снятые с `Image_7.jpg` в полном разрешении: (x, y, w, h) в
# координатах рамки 1568×2064. Числа сходятся на `Image_5/6/7`, а места 1–4 —
# ещё и на `Image_3/4`. Допуск 8px — на растекание края в JPEG.
MOCKUP_PLACES = [
    (0, 103, 455, 677),
    (385, 0, 808, 605),
    (980, 400, 590, 451),
    (470, 616, 605, 808),
    (0, 1070, 632, 580),
    (980, 1045, 589, 605),
    (470, 1435, 495, 629),
]


@pytest.mark.parametrize("index", range(MAX_MODULE))
def test_every_place_matches_the_mockup(index):
    """Сверяем каждое место, а не только рамку композиции.

    Пятое место когда-то стояло как `Place(0, 616, 628, 1032)` — высокий портрет
    во всю левую сторону вместо низкого почти квадрата. Рамку это не меняло: низ
    в обоих случаях приходился на 1648, потому что там же кончается шестое место.
    Проверка рамки такое пропускает, поэтому нужна проверка по местам.
    """
    place = MASTER[index]
    x, y, w, h = MOCKUP_PLACES[index]
    assert (place.x, place.y, place.w, place.h) == pytest.approx((x, y, w, h), abs=8)


def test_fifth_place_sits_flush_with_the_sixth():
    """Нижние края пятого и шестого мест совпадают — так на макете."""
    fifth, sixth = MASTER[4], MASTER[5]
    assert fifth.y + fifth.h == sixth.y + sixth.h


@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_composition_takes_the_same_share_of_the_column_as_on_the_mockup(size):
    """Доля колонки — тоже с макета.

    Композиция на три и больше работ занимает колонку целиком; у́же — только
    одна и две работы. Без этого доска висела бы узкой полосой посреди пустых
    полей, чего на макетах нет ни на одном.
    """
    width, _height = MOCKUP_FRAMES[size]
    assert MODULE_SPANS[size] == pytest.approx(width / MASTER_FRAME[0], abs=0.002)


def test_places_have_the_shapes_the_editor_shows():
    """Редактор обрезки берёт форму рамки отсюда — они обязаны совпадать."""
    ratios = place_ratios(MAX_MODULE)
    _frame, tiles = MODULES[MAX_MODULE]
    frame_ratio = MODULES[MAX_MODULE][0]
    assert len(ratios) == MAX_MODULE
    for tile, ratio in zip(tiles, ratios):
        assert ratio == pytest.approx(tile.w / tile.h * frame_ratio, abs=0.001)


def test_place_ratios_cover_boards_longer_than_one_module():
    assert len(place_ratios(19)) == 19


# --- первый ряд обязан влезать в экран ---

@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_first_row_height_is_measured_from_the_top_places(size):
    """`--row` — низ верхних мест в долях ширины композиции.

    Из неё CSS получает предельную ширину модуля: свободная высота, делённая на
    `--row`. Считаем то же самое по плиткам — числа обязаны сойтись.
    """
    ratio, tiles = MODULES[size]
    bottom = max(t.y + t.h for t in tiles[:FIRST_ROW]) / 100
    assert MODULE_ROWS[size] == pytest.approx(bottom / ratio, abs=0.001)


@pytest.mark.parametrize("size", range(FIRST_ROW, MAX_MODULE + 1))
def test_first_row_takes_the_same_height_on_any_board(size):
    """Тройка сверху занимает одну и ту же высоту хоть на трёх работах, хоть на семи.

    Места не пересчитываются от числа работ, поэтому и высота первого ряда на
    экране одна. Иначе потолок `--fit / --row` пришлось бы считать для каждого
    размера доски отдельно, а он общий для всех модулей страницы.
    """
    assert MODULE_ROWS[size] * MODULE_SPANS[size] == pytest.approx(0.5408, abs=0.001)


@pytest.mark.parametrize("size", range(1, MAX_MODULE + 1))
def test_first_row_never_exceeds_the_whole_module(size):
    ratio, _tiles = MODULES[size]
    assert 0 < MODULE_ROWS[size] <= 1 / ratio + 0.001


def test_every_module_carries_its_row():
    """Без `--row` в разметке потолок высоты молча выключился бы."""
    for module in build_modules(19):
        assert module["row"] > 0


@pytest.mark.parametrize("works_count", [1, 2, 3, 7])
def test_showcase_tells_css_how_tall_the_first_row_is(manager_client, works_count):
    """`--rowk` на колонке — из него шапка считает, сколько ей ужиматься.

    У доски на одну-две работы ряд ниже, чем у тройки, и шапку под него жать
    незачем: без этого числа страница ужимала бы её всегда.
    """
    board = manager_client.post(
        f"{API}/boards", json={"title": f"Ряд {works_count}"}
    ).json()
    for _ in range(works_count):
        manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": ("work.png", png_bytes(), "image/png")},
        )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}")
    expected = round(MODULE_ROWS[works_count] * MODULE_SPANS[works_count], 4)
    assert f"--rowk: {expected}" in page.text


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
