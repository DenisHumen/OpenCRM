from config.settings import get_settings
from tests.conftest import API, png_bytes


def _board(client, title="Логотипы для кофеен"):
    response = client.post(f"{API}/boards", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def _upload_png(client, board_id, name="logo.png", color=(217, 119, 87)):
    response = client.post(
        f"{API}/boards/{board_id}/works",
        files={"file": (name, png_bytes(color=color), "image/png")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_upload_work_processing_pipeline(manager_client):
    board = _board(manager_client)
    work = _upload_png(manager_client, board["id"])
    assert work["status"] == "processing"

    # фоновая обработка в TestClient выполняется сразу после ответа
    ready = manager_client.get(f"{API}/boards/{board['id']}/works/{work['id']}").json()
    assert ready["status"] == "ready"
    assert ready["width"] == 640 and ready["height"] == 480
    assert ready["blurhash"]
    assert ready["media"]["card"].endswith("card.webp")

    # производные файлы на диске, оригинал не в публичном списке
    #
    # Каталог берётся СВОЙ, по адресу производной этой самой работы. Здесь
    # стояло `next(media_dir.glob("*"))` — первый попавшийся, — и это работало,
    # пока работа в каталоге была одна. Порядок у `glob` файловый, а не по
    # времени создания: стоило появиться соседней работе, и проверка начинала
    # смотреть в чужой каталог. Поймано обратным порядком прогона на CI, где
    # первым оказался каталог с `video.mp4`.
    media_dir = get_settings().media_dir
    work_uid = ready["media"]["card"].rstrip("/").split("/")[-2]
    work_dir = media_dir / work_uid
    assert work_dir.is_dir(), f"каталог работы не найден: {work_dir}"
    names = {p.name for p in work_dir.iterdir()}
    assert {"original.png", "large.webp", "card.webp", "thumb.webp"} <= names

    # медиа отдаётся с корректным MIME и вечным кэшем; оригинал снаружи недоступен
    media = manager_client.get(ready["media"]["card"])
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/webp"
    assert "immutable" in media.headers["cache-control"]
    assert manager_client.get(f"/media/{work_dir.name}/original.png").status_code == 404


def test_upload_names_the_work_after_the_file(manager_client):
    """Свежая работа не должна быть безымянной: подпись на витрине рисуется
    только при заполненном title, а имя файла почти всегда осмысленно."""
    board = _board(manager_client, "Имена работ")
    work = _upload_png(manager_client, board["id"], name="sigma_science.packaging.png")
    assert work["title"] == "sigma science packaging"  # без расширения и разделителей


def test_title_from_filename_rules():
    from core.services.board_service import title_from_filename

    assert title_from_filename("logo.png") == "logo"
    assert title_from_filename("Айдентика кофейни.jpg") == "Айдентика кофейни"
    assert title_from_filename("brand_guide_v2.final.pdf") == "brand guide v2 final"
    assert title_from_filename("  spaced  .webp") == "spaced"
    # имя без расширения не портим
    assert title_from_filename("README") == "README"
    assert len(title_from_filename("и" * 500 + ".png")) == 200


def test_freshly_uploaded_work_is_captioned_on_the_showcase(manager_client):
    board = _board(manager_client, "Подписи")
    _upload_png(manager_client, board["id"], name="Логотип кофейни.png")
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    share = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    from fastapi.testclient import TestClient

    from web.main import app

    page = TestClient(app).get(f"/b/{share['token']}").text
    assert "Логотип кофейни" in page
    assert "Логотип кофейни.png" not in page  # расширение в подписи не нужно


def test_upload_rejects_unknown_type(manager_client):
    board = _board(manager_client, "Мусор")
    response = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("hack.exe", b"MZ\x90\x00 not a picture", "image/png")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_reorder_and_cover(manager_client):
    board = _board(manager_client, "Порядок")
    w1 = _upload_png(manager_client, board["id"], "a.png", (200, 60, 60))
    w2 = _upload_png(manager_client, board["id"], "b.png", (60, 200, 60))
    w3 = _upload_png(manager_client, board["id"], "c.png", (60, 60, 200))

    # порядок по умолчанию — порядок загрузки
    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    assert [w["id"] for w in detail["works"]] == [w1["id"], w2["id"], w3["id"]]

    reorder = manager_client.put(
        f"{API}/boards/{board['id']}/works/order",
        json={"work_ids": [w3["id"], w1["id"], w2["id"]]},
    )
    assert reorder.status_code == 200
    assert [w["id"] for w in reorder.json()["items"]] == [w3["id"], w1["id"], w2["id"]]

    # неполный список — ошибка
    bad = manager_client.put(
        f"{API}/boards/{board['id']}/works/order", json={"work_ids": [w1["id"]]}
    )
    assert bad.status_code == 422

    # обложка: только работа этой доски
    other_board = _board(manager_client, "Другая")
    foreign = _upload_png(manager_client, other_board["id"])
    assert (
        manager_client.patch(
            f"{API}/boards/{board['id']}", json={"cover_work_id": foreign["id"]}
        ).status_code
        == 422
    )
    ok = manager_client.patch(
        f"{API}/boards/{board['id']}", json={"cover_work_id": w2["id"]}
    )
    assert ok.status_code == 200
    assert ok.json()["cover_work_id"] == w2["id"]


def test_delete_work_removes_files_and_cover(manager_client):
    board = _board(manager_client, "Удаление работ")
    work = _upload_png(manager_client, board["id"])
    manager_client.patch(f"{API}/boards/{board['id']}", json={"cover_work_id": work["id"]})

    media_dirs_before = set(get_settings().media_dir.glob("*"))
    assert (
        manager_client.delete(f"{API}/boards/{board['id']}/works/{work['id']}").status_code
        == 200
    )
    media_dirs_after = set(get_settings().media_dir.glob("*"))
    assert len(media_dirs_before - media_dirs_after) == 1  # каталог работы удалён

    detail = manager_client.get(f"{API}/boards/{board['id']}").json()
    assert detail["cover_work_id"] is None
    assert detail["works"] == []


def test_board_soft_delete(manager_client):
    board = _board(manager_client, "Доска на удаление")
    assert manager_client.delete(f"{API}/boards/{board['id']}").status_code == 200
    assert manager_client.get(f"{API}/boards/{board['id']}").status_code == 404
    listing = manager_client.get(f"{API}/boards", params={"search": "Доска на удаление"}).json()
    assert listing["total"] == 0


def test_work_title_description_update(manager_client):
    board = _board(manager_client, "Подписи")
    work = _upload_png(manager_client, board["id"])
    updated = manager_client.patch(
        f"{API}/boards/{board['id']}/works/{work['id']}",
        json={"title": "Логотип, вариант 1", "description": "Тёплая палитра"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Логотип, вариант 1"


def test_the_board_list_asks_a_fixed_number_of_questions(manager_client):
    """Список досок не растёт запросами вместе с числом досок.

    Карточка доски собиралась поштучно: работы, обложка, просмотры, ссылки,
    клиент — пять запросов на доску, то есть двести пятьдесят на страницу из
    пятидесяти. И собиралась она так в трёх местах сразу — в списке, на
    дашборде и в палитре Ctrl+K, — слово в слово.

    Считаем сами запросы, а не время: время на стенде зависит от диска и
    соседей, число обращений — только от кода.
    """
    from sqlalchemy import event

    from database.session import engine

    #: Таблицы, чтение которых оплачивает не список досок, а протухший кэш.
    #:
    #: Состав блоков живёт в памяти процесса две секунды (`CACHE_SECONDS`), и
    #: попадёт ли обновление кэша в замер, решает секундомер, а не код. Раньше
    #: это лечилось холостым заходом перед измерением — но между заходом и
    #: замером те же две секунды могут истечь, и тест падал через раз на
    #: загруженной машине, показывая «11 против 10». Считать надо то, о чём тест
    #: спрашивает, а спрашивает он про число досок.
    CACHE_TABLES = ("module_states", "site_settings")

    def cost_of_listing(boards_count: int) -> int:
        client = manager_client.post(
            f"{API}/clients", json={"name": f"Заказчик досок {boards_count}"}
        ).json()
        for i in range(boards_count):
            created = _board(manager_client, title=f"Доска {boards_count}-{i}")
            manager_client.patch(
                f"{API}/boards/{created['id']}", json={"client_id": client["id"]}
            )
            _upload_png(manager_client, created["id"])

        queries = []
        listener = lambda conn, cursor, statement, *rest: queries.append(statement)
        event.listen(engine, "before_cursor_execute", listener)
        try:
            listed = manager_client.get(f"{API}/boards?per_page=200")
            assert listed.status_code == 200, listed.text
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return [q for q in queries if not any(table in q for table in CACHE_TABLES)]

    few = cost_of_listing(2)
    many = cost_of_listing(10)

    assert len(many) <= len(few), (
        f"восемь лишних досок стоили лишних запросов: было {len(few)}, стало {len(many)}\n"
        + "\n".join(" ".join(q.split())[:120] for q in many)
    )


def test_the_three_places_show_the_same_board_card(manager_client):
    """Список, дашборд и палитра показывают одну и ту же карточку.

    Пока сборка была скопирована трижды, поле, добавленное в одном месте, в
    двух других не появлялось — и заметить это можно было только глазами.
    """
    client = manager_client.post(f"{API}/clients", json={"name": "Общий заказчик"}).json()
    board = _board(manager_client, title="Доска для трёх экранов")
    manager_client.patch(f"{API}/boards/{board['id']}", json={"client_id": client["id"]})
    _upload_png(manager_client, board["id"])

    def find(items):
        return next((b for b in items if b["id"] == board["id"]), None)

    in_list = find(manager_client.get(f"{API}/boards?per_page=200").json()["items"])
    in_palette = find(
        manager_client.get(f"{API}/search", params={"q": "Доска для трёх"}).json()["boards"]["items"]
    )
    in_dashboard = find(manager_client.get(f"{API}/dashboard").json()["recent_boards"])

    assert in_list and in_palette, "доска потерялась в списке или в палитре"
    assert set(in_list) == set(in_palette), "палитра отдаёт другой набор полей"
    assert in_list["works_count"] == in_palette["works_count"] == 1
    assert in_list["client_name"] == in_palette["client_name"] == "Общий заказчик"
    if in_dashboard is not None:   # дашборд показывает только четыре свежих
        assert set(in_dashboard) <= set(in_list)
        assert in_dashboard["works_count"] == 1


def test_ogromnaya_kartinka_otvergaetsya_do_razzhatiya(manager_client):
    """Картинка, которая не влезает в бюджет памяти, получает отказ при ЗАГРУЗКЕ.

    Цена разжатия замерена и линейна: 3,83 МБ на мегапиксель. У службы `app` в
    `docker-compose.yml` нет `mem_limit` — он стоит у всех служб наблюдения и
    отсутствует у трёх главных, — значит верхнюю границу задаёт машина: два
    гигабайта на всё, включая MySQL. Жертву при нехватке выбирает ядро по
    наибольшему RSS, то есть скорее всего базу.

    Приманка тут не JPEG, а PNG: 25 Мпикс весят 2 МБ файлом и 95,6 МБ в памяти.
    Предел `max_upload_mb: 200` такое пропускает не заметив — он про размер
    посылки, а расход зависит от числа пикселей.

    Отказ обязан прийти В ЗАПРОСЕ. Превью строятся фоновой задачей, и отказ там
    пометил бы работу `failed` без единого слова о причине: поля для причины у
    модели нет, и человек повторял бы тот же файл.
    """
    import io as _io

    from PIL import Image as _Image

    storona = 7200  # 51,8 Мпикс — чуть выше бюджета
    bomba = _io.BytesIO()
    _Image.new("RGB", (storona, storona)).save(bomba, "PNG", compress_level=1)
    telo = bomba.getvalue()
    assert len(telo) < 20 * 1024 * 1024, (
        "опыт бессмыслен: файл сам по себе великоват, и его отвергнет потолок "
        "размера, а не бюджет памяти"
    )

    doska = _board(manager_client, "Доска для бомбы")
    otvet = manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("bomba.png", telo, "image/png")},
    )
    assert otvet.status_code == 422, f"ожидался понятный отказ, пришло {otvet.status_code}"
    assert otvet.json()["error"]["code"] == "image_too_large"


def test_obychnyy_snimok_s_kamery_prohodit(manager_client):
    """Снимок с телефона проходит, и это половина смысла правки.

    Бюджет должен отсекать бомбы, а не фотографии. 24 Мпикс — обычный телефон;
    JPEG вдобавок идёт через `draft` и приходит к проверке уже уменьшенным,
    поэтому камеры на 100 Мпикс тоже проходят.
    """
    import io as _io

    from PIL import Image as _Image

    snimok = _io.BytesIO()
    _Image.new("RGB", (5657, 4243)).save(snimok, "JPEG", quality=80)

    doska = _board(manager_client, "Доска для снимка")
    otvet = manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("foto.jpg", snimok.getvalue(), "image/jpeg")},
    )
    assert otvet.status_code == 202, f"обычный снимок отвергнут: {otvet.text[:200]}"
