"""Обрезка работ на витрине и всё, что осталось от «длинных работ».

Правило обрезки одно на обе стороны — витрину и редактор в CRM: работа обрезана,
когда она вытянутее своего места композиции (`layout.is_cropped`). Порога «эта
картинка длинная» в нём нет: обрезка зависит не от того, какая картинка, а от
того, в какое место она попала.

`media_service.LONG_RATIO` при этом жив, но отвечает на другой вопрос — как
обработать сам файл: производные вытянутых изображений ужимаются по ширине
(иначе от лонгрида 1:10 в `card.webp` осталось бы ~80px ширины), а в лайтбоксе
такая работа листается прокруткой.
"""

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.services import media_service
from tests.conftest import API, png_bytes
from web.main import app
from web.public.layout import cropped_indexes, phone_cropped_indexes


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


# --- одно правило: работа обрезана своим местом, а не своей вытянутостью ---

def test_the_same_work_is_whole_in_one_place_and_cropped_in_the_next():
    """Три одинаковых работы, три места разной формы — три разных ответа.

    Портрет 2:3 порог «длинной картинки» не проходит и раньше считался
    помещающимся всегда. Но первое место композиции само вертикальное (~1:1.5),
    а второе и третье — широкие: там у портрета срезано больше трети высоты.
    Отсюда и жалоба «в одному частково видно назву, а в іншому кінчики ледь».
    """
    works = [{"kind": "image", "width": 1000, "height": 1500} for _ in range(3)]
    assert cropped_indexes(works) == {1, 2}


@pytest.mark.parametrize(
    "height, cropped",
    [
        (1495, False),  # ровно под место
        (1510, False),  # на процент выше места — глазу не видно, мигать нечему
        (1560, True),   # срезано больше допуска
        (3000, True),
    ],
)
def test_tolerance_keeps_a_work_made_for_its_place_whole(height, cropped):
    """Место — округлённое число, стороны работы — целые: точного равенства не бывает.

    Первое место композиции ~1:1.4954. Работа, сделанная под него, разойдётся с
    ним на сотые — и без допуска получала бы размытие и «VIEW FULL» из-за
    невидимых глазу двух пикселей, а от пересчёта места мигала бы туда-сюда.
    """
    works = [
        {"kind": "image", "width": 1000, "height": height},
        {"kind": "image", "width": 1000, "height": 1495},  # чтобы мест стало два
    ]
    assert (0 in cropped_indexes(works)) is cropped


def test_a_work_wider_than_its_place_is_not_called_cropped():
    """Широкую работу место режет по бокам — но это не «продолжение снизу».

    Подсказка на витрине — размытие по нижнему срезу и выбор фрагмента по
    вертикали. Широкой работе они сказали бы неправду, поэтому правило говорит
    только про высоту. Если понадобится — это отдельный разговор со своим
    инструментом, а не тихое расширение этого правила.
    """
    # второе место широкое (~1.34), работа 3:1 ещё шире
    works = [{"kind": "image", "width": 1000, "height": 1495},
             {"kind": "image", "width": 3000, "height": 1000}]
    assert cropped_indexes(works) == set()


def test_video_and_unknown_sizes_are_not_cropped():
    """У видео на плитке своё обещание — круг ▶ ровно там, где встала бы плашка.

    А без сторон работы пропорцию не с чем сравнивать: гадать вместо ответа
    нельзя.
    """
    works = [
        {"kind": "image", "width": 1000, "height": 2000},
        {"kind": "video", "width": 400, "height": 4000},
        {"kind": "image", "width": None, "height": None},
    ]
    assert cropped_indexes(works) == {0}


def test_phone_has_one_place_for_everyone():
    """На телефоне композиции нет — там режет только предел высоты кадра.

    Иначе работа, которую резало место композиции, на телефоне была бы видна
    целиком, а плашка «открыть целиком» всё равно обещала бы продолжение.
    """
    works = [
        {"kind": "image", "width": 1000, "height": 1500},
        {"kind": "image", "width": 1000, "height": 1500},
        {"kind": "image", "width": 400, "height": 2400},
    ]
    assert cropped_indexes(works) == {1, 2}
    assert phone_cropped_indexes(works) == {2}


def test_showcase_offers_view_full_to_every_image(manager_client):
    """Кнопка — у каждой картинки, размытие среза — только у обрезанной.

    Три одинаковых работы попадают в места разной формы: в первом, вертикальном,
    работа видна целиком, в двух других срезана. Раньше кнопка была ровно у
    срезанных — и в одном ряду у соседних карточек она то была, то нет, хотя
    клик открывал обе. Теперь «открыть целиком» обещано всем: на плитке работа
    занимает долю экрана, в лайтбоксе — всю величину.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Портреты"}).json()
    for index in range(3):
        upload = manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": (f"{index}.png", png_bytes(size=(1000, 1500)), "image/png")},
        )
        assert upload.status_code == 202, upload.text
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    # первое место композиции вертикальное — та же работа в нём видна целиком
    assert page.count('class="tile is-cropped') == 2
    assert page.count('class="tile"') == 1
    # кнопка у всех трёх, размытие среза — только у двух срезанных
    assert page.count('class="more"') == 3
    assert page.count('<div class="glass">') == 2
    # телефон эти работы покажет целиком: композиции там нет
    assert 'class="tile is-cropped is-tall"' not in page


def test_showcase_promises_no_continuation_to_a_work_seen_whole(manager_client):
    """Работа в пропорциях своего места: кнопка есть, размытия среза нет.

    Кнопка говорит «откроется крупно» — это правда для любой работы. Размытие
    говорит «под срезом есть продолжение» — целой работе оно соврало бы.
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Ровно в место"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("wide.png", png_bytes(size=(640, 480)), "image/png")},
    )
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert 'class="tile"' in page
    assert 'class="tile is-cropped' not in page
    assert '<div class="glass">' not in page
    assert page.count('class="more"') == 1
    assert "View full" in page


def test_editor_hears_the_verdict_from_the_server(manager_client):
    """Правило живёт на сервере: редактор его не повторяет, а читает готовым."""
    board = manager_client.post(f"{API}/boards", json={"title": "Редактор"}).json()
    for index in range(3):
        manager_client.post(
            f"{API}/boards/{board['id']}/works",
            files={"file": (f"{index}.png", png_bytes(size=(1000, 1500)), "image/png")},
        )
    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    assert [w["is_cropped"] for w in detail["works"]] == [False, True, True]


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
    assert page.count('class="tile is-cropped') == 1
    assert page.count('class="tile"') == 1
    # обрезка прикрыта размытием — только она; кнопка у обеих работ
    assert page.count('<div class="glass">') == 1
    assert page.count('class="more"') == 2
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
    assert page.count('class="tile is-cropped') == count


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


def test_work_that_fits_its_place_has_nothing_to_choose(manager_client):
    """Работа в пропорциях своего места видна целиком — выбирать нечего."""
    board = manager_client.post(f"{API}/boards", json={"title": "Короткая"}).json()
    manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("wide.png", png_bytes(size=(640, 480)), "image/png")},
    )
    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    assert detail["works"][0]["is_cropped"] is False


def test_fragment_is_chosen_for_a_work_cropped_by_its_place(manager_client):
    """Портрет 2:3 в широком месте срезан — и подвинуть окно обязано быть чем.

    Раньше служба отвечала «not_a_long_work»: работа не проходила порог
    вытянутости, хотя место срезало ей треть высоты. Это и есть жалоба
    «поправить было нечем».
    """
    board = manager_client.post(f"{API}/boards", json={"title": "Портрет"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("portrait.png", png_bytes(size=(1000, 1500)), "image/png")},
    ).json()
    saved = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}", json={"preview_focus": 0.5}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["preview_focus"] == 0.5


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


# --- память: полноразмерных копий быть не должно ------------------------------


def test_proizvodnye_ne_delayut_polnorazmernyh_kopiy():
    """`derive` выделяет РЕЗУЛЬТАТ, а не копию оригинала.

    **НАЙДЕНО ЗАМЕРОМ В БОЕВОМ ОБРАЗЕ.** Прежде здесь стоял `im.copy()` с
    последующим `thumbnail`: на каждую из трёх производных выделялась
    полноразмерная копия. PNG 7100×7042 (0,67 МБ файлом!) давал пик 612 МБ на
    один поток и 1185 МБ на два — при том, что комментарий над `_razzhatie`
    обещал «предсказуемый пик 382 МБ». После правки те же замеры: 293 и 550 МБ.

    Проверяем не память (замер мигал бы на чужой машине), а её причину: в теле
    не должно быть полноразмерного дубля. Тот же приём, что у сторожа чтения
    копии базы в `tests/test_autoupdate.py`.
    """
    import ast
    import inspect

    from core.services import media_service

    for imya in ("derive", "compute_blurhash"):
        telo = ast.parse(inspect.getsource(getattr(media_service, imya)))
        obrashcheniya = {u.attr for u in ast.walk(telo) if isinstance(u, ast.Attribute)}
        assert "copy" not in obrashcheniya, (
            f"{imya} снова делает полноразмерную копию — вернулся пик втрое выше"
        )
        assert "thumbnail" not in obrashcheniya, (
            f"{imya} снова правит картинку на месте, а значит требует копии"
        )


def test_razmery_proizvodnyh_ne_izmenilis():
    """Отказ от копии не имеет права сдвинуть ни одного пикселя.

    `resize` считает цель сам, `thumbnail` считал её внутри — и разойтись они
    могли бы на округлении, на вырожденных сторонах и на длинных картинках, где
    правило своё. Проверяем на всех трёх видах разом.
    """
    from PIL import Image

    from core.services.media_service import WEBP_MAX_SIDE, derive, is_long_image

    def po_staromu(im, box):
        variant = im.copy()
        if not is_long_image(im.width, im.height):
            variant.thumbnail((box, box), Image.LANCZOS)
            return variant.size
        width = min(im.width, box)
        height = round(width * im.height / im.width)
        if height > WEBP_MAX_SIDE:
            height = WEBP_MAX_SIDE
            width = max(1, round(height * im.width / im.height))
        variant.thumbnail((width, height), Image.LANCZOS)
        return variant.size

    sluchai = [
        (4000, 3000), (3000, 4000), (1000, 1000), (200, 150),
        (800, 12000), (12000, 800), (1, 5000), (5000, 1), (2560, 1080),
    ]
    for shirina, vysota in sluchai:
        im = Image.new("RGB", (shirina, vysota))
        for box in (1600, 640, 320, 32):
            assert derive(im, box).size == po_staromu(im, box), (
                f"{shirina}x{vysota} при коробке {box}: размер производной уехал"
            )
