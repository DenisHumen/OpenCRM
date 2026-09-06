"""Напоминания: срок, исполнитель, счётчики.

CRM, которая не напоминает перезвонить, — записная книжка.

Главное здесь не «сохраняется ли строка», а часовой пояс: «сегодня до 18:00»
означает местное время, а в базе всё лежит в UTC. Перепутай их — и напоминание
сработает не тогда, причём тихо.
"""

from datetime import datetime, timedelta, timezone

from database.session import SessionLocal
from tests.conftest import API, png_bytes
from tests.test_deals import DEALS, make_client

TASKS = f"{API}/tasks"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def test_deadline_with_an_offset_is_stored_as_utc(manager_client):
    """18:00 в Киеве и 18:00 в Варшаве — разные мгновения.

    Клиент присылает абсолютный момент со смещением; сервер обязан привести его
    к UTC, иначе сравнение «просрочено ли» врёт на величину смещения.
    """
    kyiv = timezone(timedelta(hours=3))
    local = datetime(2026, 8, 10, 18, 0, tzinfo=kyiv)   # 18:00 по Киеву
    task = manager_client.post(TASKS, json={"title": "Позвонить", "due_at": iso(local)}).json()

    stored = datetime.fromisoformat(task["due_at"])
    # 18:00+03:00 — это 15:00 UTC. Ни минутой позже.
    assert stored.hour == 15 and stored.minute == 0, task["due_at"]


def test_a_deadline_without_an_offset_is_taken_as_utc(manager_client):
    """Гадать о зоне отправителя хуже, чем принять соглашение и держать его."""
    task = manager_client.post(
        # Дата от «сейчас», а не зашитая: проверяется РАЗБОР строки без смещения,
        # и день тут безразличен. А вот оставленная в прошлом задача становится
        # просроченной и меняет порядок в общем для всех тестов списке — этим
        # уже уронило соседний файл, см. `skoro` в test_dashboard_deals.py.
        TASKS,
        json={
            "title": "Без зоны",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
                "%Y-%m-%dT09:30:00"
            ),
        },
    ).json()
    stored = datetime.fromisoformat(task["due_at"])
    assert (stored.hour, stored.minute) == (9, 30)


def test_task_without_an_assignee_goes_to_its_author(manager_client):
    """«Ничья» задача не делается никем: каждый думает, что возьмёт другой."""
    task = manager_client.post(TASKS, json={"title": "Кто-нибудь сделает"}).json()
    assert task["assignee_id"] is not None
    assert task["assignee_name"]


def test_a_task_needs_neither_client_nor_deal(manager_client):
    """«Отвезти документы в банк» — тоже задача."""
    task = manager_client.post(TASKS, json={"title": "Отвезти документы"})
    assert task.status_code == 201, task.text
    assert task.json()["client_id"] is None
    assert task.json()["deal_id"] is None


def test_overdue_and_today_are_separate_lists(manager_client):
    now = datetime.now(timezone.utc)
    late = manager_client.post(
        TASKS, json={"title": "Просрочено", "due_at": iso(now - timedelta(hours=2))}
    ).json()
    soon = manager_client.post(
        TASKS, json={"title": "Сегодня", "due_at": iso(now + timedelta(hours=3))}
    ).json()
    later = manager_client.post(
        TASKS, json={"title": "Через месяц", "due_at": iso(now + timedelta(days=30))}
    ).json()

    overdue = [t["id"] for t in manager_client.get(f"{TASKS}?scope=overdue").json()["items"]]
    today = [t["id"] for t in manager_client.get(f"{TASKS}?scope=today").json()["items"]]

    assert late["id"] in overdue
    assert soon["id"] not in overdue, "будущий срок попал в просроченные"
    assert soon["id"] in today
    assert later["id"] not in today


def test_done_tasks_leave_the_open_lists(manager_client):
    now = datetime.now(timezone.utc)
    task = manager_client.post(
        TASKS, json={"title": "Сделаю и закрою", "due_at": iso(now - timedelta(hours=1))}
    ).json()
    assert task["id"] in [t["id"] for t in manager_client.get(f"{TASKS}?scope=overdue").json()["items"]]

    done = manager_client.patch(f"{TASKS}/{task['id']}", json={"is_done": True}).json()
    assert done["is_done"] is True
    assert done["done_at"], "не записано, когда закрыли"

    assert task["id"] not in [t["id"] for t in manager_client.get(f"{TASKS}?scope=overdue").json()["items"]]
    assert task["id"] in [t["id"] for t in manager_client.get(f"{TASKS}?scope=done").json()["items"]]

    # и обратно: отметили по ошибке
    back = manager_client.patch(f"{TASKS}/{task['id']}", json={"is_done": False}).json()
    assert back["is_done"] is False and back["done_at"] is None


def test_undated_tasks_do_not_push_overdue_ones_down(manager_client):
    """Задача без срока не должна оттеснять просроченную наверх списка.

    Сравниваются позиции СВОИХ двух задач, а не место в общем списке. База у
    тестов одна на всю сессию, и «моя запись первая» — это утверждение не про
    сортировку, а про то, что соседи не успели ничего завести. Такая проверка
    держится ровно до первого нового теста с просроченной задачей и падает
    потом в чужом файле, где искать её никто не станет.
    """
    now = datetime.now(timezone.utc)
    bez_sroka = manager_client.post(TASKS, json={"title": "Когда-нибудь"}).json()
    dated = manager_client.post(
        TASKS, json={"title": "Срочное", "due_at": iso(now - timedelta(days=1))}
    ).json()

    poryadok = [t["id"] for t in manager_client.get(f"{TASKS}?scope=open").json()["items"]]
    assert dated["id"] in poryadok and bez_sroka["id"] in poryadok, "задачи не попали в список"
    assert poryadok.index(dated["id"]) < poryadok.index(bez_sroka["id"]), (
        "бессрочная задача оказалась выше просроченной"
    )


def test_summary_counts_what_the_navigation_shows(manager_client):
    """Без счётчика в задачи заходят «на всякий случай»."""
    before = manager_client.get(f"{TASKS}/summary").json()
    now = datetime.now(timezone.utc)
    manager_client.post(TASKS, json={"title": "Ещё просрочка", "due_at": iso(now - timedelta(minutes=5))})

    after = manager_client.get(f"{TASKS}/summary").json()
    assert after["overdue"] == before["overdue"] + 1
    assert after["open"] == before["open"] + 1


def test_summary_counts_past_the_length_of_the_list(manager_client):
    """Счётчик считает все напоминания, а не первую страницу.

    Список отдаётся с потолком в 200 строк, и счётчик легко сделать его
    заложником — тогда на большой базе в меню навсегда застынет «200». Проверка
    заводит больше задач, чем помещается в список, и сверяет число с тем, что
    насчитал сервер до и после.
    """
    from core.services.task_service import LIST_LIMIT

    before = manager_client.get(f"{TASKS}/summary").json()["open"]
    added = LIST_LIMIT + 5
    for i in range(added):
        assert manager_client.post(TASKS, json={"title": f"Напоминание {i}"}).status_code == 201

    listed = manager_client.get(f"{TASKS}?scope=open").json()["items"]
    counted = manager_client.get(f"{TASKS}/summary").json()["open"]

    assert len(listed) == LIST_LIMIT, "список отдал больше своего потолка"
    assert counted == before + added, "счётчик остановился на длине списка"


def test_task_is_linked_to_a_deal_and_found_by_it(manager_client):
    client = make_client(manager_client, "Клиент задачи")
    deal = manager_client.post(DEALS, json={"title": "Заказ", "client_id": client["id"]}).json()
    task = manager_client.post(
        TASKS, json={"title": "Заказать деталь", "deal_id": deal["id"]}
    ).json()

    found = manager_client.get(f"{TASKS}?deal_id={deal['id']}").json()["items"]
    assert [t["id"] for t in found] == [task["id"]]


def test_tasks_disappear_with_their_module(root_client, manager_client):
    root_client.post(f"{API}/modules/tasks", json={"enabled": False})
    try:
        closed = manager_client.get(TASKS)
        assert closed.status_code == 403
        assert closed.json()["error"]["code"] == "module_disabled"
        # соседние разделы не задеты
        assert manager_client.get(f"{API}/clients").status_code == 200
    finally:
        root_client.post(f"{API}/modules/tasks", json={"enabled": True})


# --- важность -----------------------------------------------------------------


def test_vazhnost_po_umolchaniyu_obychnaya(manager_client):
    """Заведённое на бегу напоминание не должно кричать само по себе."""
    task = manager_client.post(TASKS, json={"title": "Без важности"}).json()
    assert task["vazhnost"] == "normal"
    assert task["note_est"] is False
    assert task["files_count"] == 0


def test_neizvestnaya_vazhnost_otvergaetsya(manager_client):
    """Молча съеденное «срочно» — это напоминание, которое не заметят."""
    otkaz = manager_client.post(TASKS, json={"title": "Ну очень", "vazhnost": "kritichno"})
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "vazhnost_unknown"


def test_vazhnost_vyshe_sroka(manager_client):
    """Важность выше срока — иначе метка «срочно» ничего не меняет.

    Список берётся по своему клиенту: база у набора одна, соседние файлы
    заводят сотни напоминаний, и «моя запись выше» в общем списке — это
    утверждение про соседей, а не про сортировку.
    """
    now = datetime.now(timezone.utc)
    client = make_client(manager_client, "Клиент порядка важности")
    obychnoe = manager_client.post(
        TASKS,
        json={
            "title": "Обычное на завтра",
            "client_id": client["id"],
            "due_at": iso(now + timedelta(days=1)),
        },
    ).json()
    srochnoe = manager_client.post(
        TASKS, json={"title": "Срочное без срока", "client_id": client["id"], "vazhnost": "urgent"}
    ).json()

    poryadok = [
        t["id"] for t in manager_client.get(f"{TASKS}?client_id={client['id']}").json()["items"]
    ]
    assert poryadok.index(srochnoe["id"]) < poryadok.index(obychnoe["id"]), (
        "срочное бессрочное оказалось ниже обычного со сроком"
    )


def test_prosrochennoe_vyshe_srochnogo(manager_client):
    """А просроченное — выше всего, и это не про красоту.

    У списка потолок в двести строк. Поставь важность первым ключом — и две
    сотни бессрочных «срочно» вытеснят из выдачи ВСЁ просроченное, то есть
    обещания, которые уже нарушены. Внутри просроченных важность работает.
    """
    now = datetime.now(timezone.utc)
    client = make_client(manager_client, "Клиент просрочки")
    srochnoe = manager_client.post(
        TASKS, json={"title": "Срочное без срока", "client_id": client["id"], "vazhnost": "urgent"}
    ).json()
    prosrochennoe = manager_client.post(
        TASKS,
        json={
            "title": "Обычное просроченное",
            "client_id": client["id"],
            "due_at": iso(now - timedelta(days=2)),
        },
    ).json()

    poryadok = [
        t["id"] for t in manager_client.get(f"{TASKS}?client_id={client['id']}").json()["items"]
    ]
    assert poryadok.index(prosrochennoe["id"]) < poryadok.index(srochnoe["id"]), (
        "просроченное оказалось ниже срочного бессрочного — потолок списка съест просрочку"
    )


def test_vazhnost_menyaetsya_v_kartochke(manager_client):
    task = manager_client.post(TASKS, json={"title": "Поднимем важность"}).json()
    povyshennoe = manager_client.patch(f"{TASKS}/{task['id']}", json={"vazhnost": "urgent"})
    assert povyshennoe.status_code == 200, povyshennoe.text
    assert povyshennoe.json()["vazhnost"] == "urgent"
    assert manager_client.get(f"{TASKS}/{task['id']}").json()["vazhnost"] == "urgent"

    otkaz = manager_client.patch(f"{TASKS}/{task['id']}", json={"vazhnost": "ochen"})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "vazhnost_unknown"
    assert manager_client.get(f"{TASKS}/{task['id']}").json()["vazhnost"] == "urgent"


# --- углублённая карточка -----------------------------------------------------


def test_kartochka_hranit_podrobnosti_i_vlozheniya(manager_client):
    """Строки списка хватает на «перезвонить», но не на «вот фото шильдика»."""
    from core.services import task_service

    task = manager_client.post(TASKS, json={"title": "Разобраться с шумом"}).json()
    manager_client.patch(f"{TASKS}/{task['id']}", json={"note": "Гудит на холодную, звонить после 10"})

    foto = manager_client.post(
        f"{TASKS}/{task['id']}/files", files={"file": ("shildik.png", png_bytes(), "image/png")}
    )
    assert foto.status_code == 201, foto.text
    zapis = foto.json()
    assert zapis["mime"] == "image/png" and zapis["task_id"] == task["id"]

    skachat = manager_client.get(zapis["download_url"])
    assert skachat.status_code == 200 and skachat.headers["content-type"].startswith("image/png")
    assert skachat.headers["content-disposition"].startswith("inline")

    kartochka = manager_client.get(f"{TASKS}/{task['id']}").json()
    assert kartochka["note"].startswith("Гудит на холодную")
    assert [f["id"] for f in kartochka["files"]] == [zapis["id"]]

    # Список отвечает «есть ли», не таская ни файлы, ни сам разбор: двести
    # строк по двадцать тысяч знаков — это мегабайты на один заход в раздел.
    v_spiske = next(
        t for t in manager_client.get(f"{TASKS}?scope=open").json()["items"] if t["id"] == task["id"]
    )
    assert v_spiske["files_count"] == 1 and "files" not in v_spiske
    assert v_spiske["note_est"] is True and "note" not in v_spiske

    with SessionLocal() as db:
        put = task_service.file_path_on_disk(task_service.get_file(db, task["id"], zapis["id"]))
    assert put.exists()
    assert manager_client.delete(f"{TASKS}/{task['id']}/files/{zapis['id']}").status_code == 200
    assert not put.exists(), "файл остался на диске после удаления"


def test_v_napominanie_kladut_foto_i_video_a_dogovor_net(manager_client):
    task = manager_client.post(TASKS, json={"title": "Приёмка вложений"}).json()

    dogovor = manager_client.post(
        f"{TASKS}/{task['id']}/files", files={"file": ("dogovor.pdf", b"%PDF-1.4 x", "application/pdf")}
    )
    assert dogovor.status_code == 422 and dogovor.json()["error"]["code"] == "file_type_not_allowed"

    podmena = manager_client.post(
        f"{TASKS}/{task['id']}/files", files={"file": ("hack.png", b"MZ not a picture", "image/png")}
    )
    assert podmena.status_code == 422 and podmena.json()["error"]["code"] == "file_content_mismatch"


def test_vlozheniya_uhodyat_vmeste_s_napominaniem(manager_client):
    """Снятое напоминание не должно оставлять снимки на диске навсегда."""
    from core.services import task_service

    task = manager_client.post(TASKS, json={"title": "Снесём вместе с фото"}).json()
    zapis = manager_client.post(
        f"{TASKS}/{task['id']}/files", files={"file": ("foto.png", png_bytes(), "image/png")}
    ).json()
    with SessionLocal() as db:
        put = task_service.file_path_on_disk(task_service.get_file(db, task["id"], zapis["id"]))
    assert put.exists()

    assert manager_client.delete(f"{TASKS}/{task['id']}").status_code == 200
    assert manager_client.get(f"{TASKS}/{task['id']}").status_code == 404
    assert not put.exists(), "снимок пережил напоминание"


def test_vlozhenie_chuzhogo_napominaniya_ne_otdayotsya(manager_client):
    """Номер файла угадать легко; принадлежность обязан сверять сервер."""
    svoyo = manager_client.post(TASKS, json={"title": "Своё"}).json()
    chuzhoe = manager_client.post(TASKS, json={"title": "Чужое"}).json()
    zapis = manager_client.post(
        f"{TASKS}/{svoyo['id']}/files", files={"file": ("foto.png", png_bytes(), "image/png")}
    ).json()

    mimo = manager_client.get(f"{TASKS}/{chuzhoe['id']}/files/{zapis['id']}/download")
    assert mimo.status_code == 404 and mimo.json()["error"]["code"] == "file_not_found"


def test_podrobnosti_s_potolkom_otvergayutsya_a_ne_rezhutsya(manager_client):
    """Молча обрезанный разбор хуже отвергнутого: человек уйдёт уверенным."""
    from core.services.task_service import MAX_NOTE

    task = manager_client.post(TASKS, json={"title": "Длинный разбор"}).json()
    otkaz = manager_client.patch(f"{TASKS}/{task['id']}", json={"note": "я" * (MAX_NOTE + 1)})
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "note_too_long"
    assert manager_client.get(f"{TASKS}/{task['id']}").json()["note"] == ""

    # Ровно по потолку — принимается, в том числе четырёхбайтными знаками:
    # колонка `MEDIUMTEXT`, а не `TEXT`, и 65 535 БАЙТ ей не предел.
    v_prityk = manager_client.patch(f"{TASKS}/{task['id']}", json={"note": "🙂" * MAX_NOTE})
    assert v_prityk.status_code == 200, v_prityk.text
    assert len(manager_client.get(f"{TASKS}/{task['id']}").json()["note"]) == MAX_NOTE


def test_pustaya_vazhnost_ne_ponizhaet_srochnoe(manager_client):
    """`{"vazhnost": ""}` — тоже чужое слово, а не «оставить как было»."""
    task = manager_client.post(TASKS, json={"title": "Срочное", "vazhnost": "urgent"}).json()
    otkaz = manager_client.patch(f"{TASKS}/{task['id']}", json={"vazhnost": ""})
    assert otkaz.status_code == 422 and otkaz.json()["error"]["code"] == "vazhnost_unknown"
    assert manager_client.get(f"{TASKS}/{task['id']}").json()["vazhnost"] == "urgent"


def test_vlozhenie_otdayotsya_s_nosniff_i_tipom_po_imeni(manager_client):
    """Заголовок ответа не должен зависеть ни от загружающего, ни от строки.

    Вложение отдаётся `inline` — то есть браузер его РИСУЕТ. Значит тип обязан
    считаться из имени (уже сверенного с содержимым), а не браться из
    присланного `Content-Type`, и `nosniff` обязан стоять.
    """
    task = manager_client.post(TASKS, json={"title": "Отдача вложения"}).json()
    zapis = manager_client.post(
        f"{TASKS}/{task['id']}/files",
        # Присланный тип — чужой и нарочно опасный: если он доедет до
        # заголовка ответа, картинка станет страницей в сессии сотрудника.
        files={"file": ("shildik.png", png_bytes(), "text/html")},
    ).json()
    assert zapis["mime"] == "image/png", "тип взят у загружающего"

    otvet = manager_client.get(zapis["download_url"])
    assert otvet.headers["content-type"].startswith("image/png")
    assert otvet.headers.get("x-content-type-options") == "nosniff"


def test_v_inline_ne_popadayut_risuemye_brauzerom(manager_client):
    """Перечень вложений не должен пересекаться с тем, что браузер исполняет.

    Вложение отдаётся `inline`, и единственное, что стоит между `<img src>` на
    странице сотрудника и чужим скриптом, — состав перечня. Приписать туда
    `svg` — одно слово, и оно выглядит безобидно: у файлов клиента `svg`
    разрешён. Поэтому правило записано проверкой, а не памятью.
    """
    from core.services.task_service import VLOZHENIYA

    risuemye = {"svg", "html", "htm", "xhtml", "xml", "pdf"}
    assert not VLOZHENIYA & risuemye, (
        f"в перечень вложений напоминания попало исполняемое браузером: {VLOZHENIYA & risuemye}"
    )


def test_chuzhoe_vlozhenie_nelzya_i_snyat(manager_client):
    """Принадлежность сверяется и на разрушительном пути, а не только на чтении."""
    svoyo = manager_client.post(TASKS, json={"title": "Своё"}).json()
    chuzhoe = manager_client.post(TASKS, json={"title": "Чужое"}).json()
    zapis = manager_client.post(
        f"{TASKS}/{svoyo['id']}/files", files={"file": ("foto.png", png_bytes(), "image/png")}
    ).json()

    mimo = manager_client.delete(f"{TASKS}/{chuzhoe['id']}/files/{zapis['id']}")
    assert mimo.status_code == 404 and mimo.json()["error"]["code"] == "file_not_found"
    assert manager_client.get(zapis["download_url"]).status_code == 200, "файл снесли мимо владельца"


def test_stroki_vlozheniy_uhodyat_kaskadom(manager_client):
    """Файлы с диска снимает служба, а строки — база. Проверяем именно базу.

    Без этого проверка «файла на диске нет» оставалась бы зелёной и при
    неработающем `ON DELETE CASCADE`: строки в `task_files` осиротели бы, и
    никто бы об этом не сказал.
    """
    from sqlalchemy import func, select

    from database.models import TaskFile

    task = manager_client.post(TASKS, json={"title": "Каскад"}).json()
    manager_client.post(
        f"{TASKS}/{task['id']}/files", files={"file": ("foto.png", png_bytes(), "image/png")}
    )
    with SessionLocal() as svoya:
        bylo = svoya.scalar(select(func.count(TaskFile.id)).where(TaskFile.task_id == task["id"]))
    assert bylo == 1

    assert manager_client.delete(f"{TASKS}/{task['id']}").status_code == 200
    with SessionLocal() as svoya:
        stalo = svoya.scalar(select(func.count(TaskFile.id)).where(TaskFile.task_id == task["id"]))
    assert stalo == 0, "строки вложений пережили напоминание"
