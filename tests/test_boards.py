import io
import zipfile

from fastapi.testclient import TestClient

from config.settings import get_settings
from tests.conftest import API, png_bytes
from web.main import app


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


# --- выгрузка исходников -----------------------------------------------------
#
# Наружу с доски уходят только производные: WebP по длинной стороне, постер
# видео, размытый след. Исходник закрыт списком разрешённых имён и не отдавался
# никому — в том числе тому, кто его сюда и загрузил. Проверки ниже стерегут обе
# половины правки: сотрудник исходник забирает, клиент витрины — нет.


def test_sotrudnik_zabiraet_ishodnik_raboty(manager_client):
    """Менеджер скачивает ровно тот файл, который загрузили, — байт в байт.

    Без этого исходника в системе не достать вовсе: на витрину уходит сжатая
    производная в WebP, а оригинал лежит в каталоге работ под служебным именем и
    закрыт списком `media_service.PUBLIC_FILENAMES`. Верни ручка производную —
    беда была бы тихой: файл скачивается, открывается, выглядит похоже, а в
    печать не годится, и узнают об этом в типографии.
    """
    ishodnik = png_bytes(color=(12, 34, 56))
    doska = _board(manager_client, "Доска для выгрузки")
    rabota = manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("logo.png", ishodnik, "image/png")},
    ).json()

    skachano = manager_client.get(
        f"{API}/boards/{doska['id']}/works/{rabota['id']}/download"
    )
    assert skachano.status_code == 200, skachano.text
    assert skachano.content == ishodnik, "отдали не исходник, а что-то другое"
    # Именно СОХРАНИТЬ, а не показать: иначе браузер откроет файл вкладкой, и
    # кнопка «скачать» перестанет скачивать.
    raspolozhenie = skachano.headers["content-disposition"]
    assert raspolozhenie.startswith("attachment"), raspolozhenie
    assert "logo.png" in raspolozhenie, raspolozhenie


def test_ishodnik_sohranyaetsya_s_nastoyashchim_rasshireniem(manager_client):
    """Имя для сохранения несёт расширение НАСТОЯЩЕГО файла, а не прежнее.

    Вид файла определяется по сигнатуре, а расширению не верят: JPEG, присланный
    под именем «foto.png», лежит у нас как `original.jpg`. Отдай мы его под
    прежним именем — человек получит файл, который не открывается двойным
    щелчком, и решит, что скачивание сломано.
    """
    snimok = io.BytesIO()
    from PIL import Image as _Image

    _Image.new("RGB", (120, 90), (200, 40, 40)).save(snimok, "JPEG", quality=80)

    doska = _board(manager_client, "Доска с чужим расширением")
    rabota = manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("foto.png", snimok.getvalue(), "image/jpeg")},
    ).json()

    skachano = manager_client.get(
        f"{API}/boards/{doska['id']}/works/{rabota['id']}/download"
    )
    assert skachano.status_code == 200, skachano.text
    raspolozhenie = skachano.headers["content-disposition"]
    assert "foto.jpg" in raspolozhenie, raspolozhenie


def test_skachivanie_ne_vydayot_rabotu_chuzhoy_doski(manager_client):
    """Работа обязана принадлежать НАЗВАННОЙ доске, иначе «нет такого».

    Номер работы стоит в адресе рядом с номером доски, и подставить чужой —
    первое, что приходит в голову. Без проверки принадлежности сотрудник (а с
    ним и всякий, кто однажды получил право на доски) забирал бы исходники
    любого клиента, зная только счётчик. Ровно этой проверки не хватало по всей
    отрасли в целом классе мест, отдающих файлы по паре номеров.
    """
    svoya = _board(manager_client, "Своя доска")
    chuzhaya = _board(manager_client, "Чужая доска")
    chuzhaya_rabota = _upload_png(manager_client, chuzhaya["id"], "secret.png")

    podmena = manager_client.get(
        f"{API}/boards/{svoya['id']}/works/{chuzhaya_rabota['id']}/download"
    )
    assert podmena.status_code == 404, podmena.text
    # И сама работа при этом жива — отказ именно про принадлежность.
    assert (
        manager_client.get(
            f"{API}/boards/{chuzhaya['id']}/works/{chuzhaya_rabota['id']}/download"
        ).status_code
        == 200
    )


def test_klient_vitriny_skachat_ishodnik_ne_mozhet(manager_client):
    """Витрина остаётся витриной: смотреть — да, забирать исходники — нет.

    Клиент открывает доску по внешней ссылке и получает страницу с
    производными. Исходник — файл в печатном качестве, часто с внутренним
    именем в придачу; отдавать его тому, кому показали подборку, никто не
    собирался. Проверка стережёт три двери сразу: обе ручки выгрузки и сама
    публичная выдача, из которой адрес скачивания не должен даже быть виден.
    """
    doska = _board(manager_client, "Витрина без выгрузки")
    rabota = _upload_png(manager_client, doska["id"], "Иванов_исходник.png")
    manager_client.patch(f"{API}/boards/{doska['id']}", json={"is_published": True})
    ssylka = manager_client.post(
        f"{API}/boards/{doska['id']}/shares", json={"pin": "4821"}
    ).json()

    gost = TestClient(app)
    voshyol = gost.post(
        f"/b/{ssylka['token']}/pin", data={"pin": "4821"}, follow_redirects=False
    )
    assert voshyol.status_code in (302, 303), voshyol.text
    # Пропуск настоящий: страницу гость видит, и отказы ниже — про выгрузку, а
    # не про то, что гость никуда не вошёл.
    stranitsa = gost.get(f"/b/{ssylka['token']}")
    assert stranitsa.status_code == 200

    odna = gost.get(f"{API}/boards/{doska['id']}/works/{rabota['id']}/download")
    assert odna.status_code == 401, odna.text
    vse = gost.get(f"{API}/boards/{doska['id']}/download")
    assert vse.status_code == 401, vse.text

    # Адреса выгрузки нет и в том, что уходит клиенту: ни в странице, ни в
    # выдаче. Иначе кнопку «скачать» дорисовал бы кто угодно — а отказ выше
    # обещает лишь то, что она не сработает.
    assert "/download" not in stranitsa.text
    dannye = gost.get(f"/b/{ssylka['token']}/data").json()
    for work in dannye["works"]:
        assert "download_url" not in work, work.keys()


def test_arhiv_doski_sobiraet_vse_ishodniki(manager_client):
    """«Скачать все» отдаёт целый архив исходников — в порядке доски.

    Доска это НАБОР, и сдают её набором: щёлкать по каждой из тридцати работ —
    тридцать щелчков и тридцать строк в «Загрузках». Порядок держат номера в
    именах: распаковщик раскладывает файлы по алфавиту, а он у имён свой, — и
    они же разводят совпадающие имена, иначе две работы, загруженные как
    `logo.png`, столкнулись бы в одном архиве.
    """
    doska = _board(manager_client, "Доска на выгрузку архивом")
    pervaya = png_bytes(color=(10, 10, 200))
    vtoraya = png_bytes(color=(200, 10, 10))
    manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("logo.png", pervaya, "image/png")},
    )
    manager_client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("logo.png", vtoraya, "image/png")},
    )

    otvet = manager_client.get(f"{API}/boards/{doska['id']}/download")
    assert otvet.status_code == 200, otvet.text
    assert otvet.headers["content-type"] == "application/zip"
    assert otvet.headers["content-disposition"].startswith("attachment")

    arhiv = zipfile.ZipFile(io.BytesIO(otvet.content))
    assert arhiv.testzip() is None, "архив собран битым"
    imena = arhiv.namelist()
    assert imena == ["01 logo.png", "02 logo.png"], imena
    assert arhiv.read(imena[0]) == pervaya
    assert arhiv.read(imena[1]) == vtoraya


def test_arhiv_pustoy_doski_otkazyvaet_ponyatno(manager_client):
    """У доски без работ архива нет — и отказ обязан это назвать.

    Пустой zip выглядел бы как успешная выгрузка: файл скачался, а внутри
    ничего. Человек решит, что пропали работы, а не что доска пуста.
    """
    doska = _board(manager_client, "Совсем пустая доска")
    otvet = manager_client.get(f"{API}/boards/{doska['id']}/download")
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "board_has_no_files"


def test_arhiv_idyot_potokom_a_ne_sobiraetsya_v_pamyati(tmp_path):
    """Архив уходит кусками, а не появляется в памяти целиком.

    Довод замерен в самом `media_service`: у службы приложения нет `mem_limit`,
    на машине два гигабайта на всё вместе с MySQL, а жертву при нехватке
    выбирает ядро по наибольшему RSS — то есть базу. Доска на три десятка
    снимков это полтора гигабайта исходников, и собери мы такой архив в
    `BytesIO`, одна кнопка «скачать все» роняла бы сайт.

    Проверяем свойство, а не устройство: первый кусок обязан прийти РАНЬШЕ, чем
    прочитан весь архив. Перепиши кто-нибудь сборку через `BytesIO` — первый же
    кусок окажется размером со всё, и проверка покраснеет.
    """
    from core.services import media_service

    krupnyy = bytes(1024 * 1024)  # мегабайт на файл
    fayly = []
    for imya in ("a.bin", "b.bin"):
        put = tmp_path / imya
        put.write_bytes(krupnyy)
        fayly.append((put, imya))

    potok = media_service.potok_zip(fayly)
    pervyy = next(potok)
    assert len(pervyy) < len(krupnyy), (
        f"первым куском пришло {len(pervyy)} байт — архив собрался целиком до отдачи"
    )

    sobrano = pervyy + b"".join(potok)
    arhiv = zipfile.ZipFile(io.BytesIO(sobrano))
    assert arhiv.namelist() == ["a.bin", "b.bin"]
    assert arhiv.read("b.bin") == krupnyy
