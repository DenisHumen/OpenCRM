"""Напоминания после перестройки 28.09.2026 (docs/bloki/29-napominaniya.md).

Видимость и права, люди, повторы, «весь день», звонки без двойного звонка,
привязки к карточкам, шаги и ссылки, календарь, общая полка.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core.services import task_service
from core.utils import now_utc
from database.models import TaskSignal
from database.session import SessionLocal
from tests.conftest import API
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры

TASKS = f"{API}/tasks"


def _moy_id(client) -> int:
    return client.get(f"{API}/auth/me").json()["id"]


def _cherez(**sdvig) -> str:
    return (datetime.now(timezone.utc) + timedelta(**sdvig)).isoformat()


def _zavesti(client, **pola) -> dict:
    otvet = client.post(TASKS, json={"title": "Проба", **pola})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def _tick(teper: datetime) -> int:
    with SessionLocal() as db:
        zvonkov = task_service.tick(db, teper)
        db.commit()
        return zvonkov


def _zvonki(task_id: int) -> list:
    with SessionLocal() as db:
        return [
            (z.user_id, z.vid, z.moment)
            for z in db.scalars(select(TaskSignal).where(TaskSignal.task_id == task_id))
        ]


# --- видимость и права --------------------------------------------------------


def test_lichnoe_napominanie_ne_vidno_drugim(root_client, role_maker, staff_maker):  # noqa: F811
    """Владелец 28.09.2026: «у каждого пользователя свои напоминания»."""
    rol = role_maker("Напоминания — свои", ["tasks.view", "tasks.create", "tasks.edit"])
    anna = staff_maker("napom-anna@test.local", rol["id"])
    boris = staff_maker("napom-boris@test.local", rol["id"])
    svoyo = _zavesti(anna, title="Личное Анны")

    assert svoyo["id"] not in [z["id"] for z in boris.get(TASKS).json()["items"]]
    assert boris.get(f"{TASKS}/{svoyo['id']}").status_code == 404
    assert boris.patch(f"{TASKS}/{svoyo['id']}", json={"title": "Взлом"}).status_code == 404
    # root видит все: у него все права, в том числе `tasks.view_others`.
    assert root_client.get(f"{TASKS}/{svoyo['id']}").status_code == 200


def test_stavit_drugim_nuzhno_pravo(role_maker, staff_maker):  # noqa: F811
    bez_prava = role_maker("Без «ставить другим»", ["tasks.view", "tasks.create"])
    s_pravom = role_maker("Со «ставить другим»", ["tasks.view", "tasks.create", "tasks.assign"])
    vera = staff_maker("napom-vera@test.local", bez_prava["id"])
    glava = staff_maker("napom-glava@test.local", s_pravom["id"])
    vera_id = _moy_id(vera)

    otkaz = vera.post(TASKS, json={"title": "Себе и другому", "poluchateli": [_moy_id(glava)]})
    assert otkaz.status_code == 403, otkaz.text

    postavleno = _zavesti(glava, title="Вере от главы", poluchateli=[vera_id])
    u_very = vera.get(TASKS, params={"kto": "moi"}).json()["items"]
    assert postavleno["id"] in [z["id"] for z in u_very]
    stroka = next(z for z in u_very if z["id"] == postavleno["id"])
    assert stroka["moyo"] is True
    # Поставивший — владелец, но не получатель: звонит Вере, а не ему.
    vladelets = next(c for c in postavleno["lyudi"] if c["vladelets"])
    assert vladelets["poluchaet"] is False
    assert postavleno["id"] in [z["id"] for z in glava.get(TASKS, params={"kto": "postavil"}).json()["items"]]


def test_poluchatel_ne_perepisyvaet_lyudey_chuzhogo(role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Ставят и правят", ["tasks.view", "tasks.create", "tasks.edit", "tasks.assign"])
    glava = staff_maker("napom-glava2@test.local", rol["id"])
    ispolnitel = staff_maker("napom-isp@test.local", rol["id"])
    postavleno = _zavesti(glava, title="Сделать отчёт", poluchateli=[_moy_id(ispolnitel)])

    otkaz = ispolnitel.patch(f"{TASKS}/{postavleno['id']}", json={"poluchateli": []})
    assert otkaz.status_code == 403, otkaz.text
    # Срок получатель двигать может — «перенести на завтра» делает тот, кто делает.
    assert ispolnitel.patch(f"{TASKS}/{postavleno['id']}", json={"due_at": _cherez(days=1)}).status_code == 200


def test_kazhdomu_svoyo(root_client, role_maker, staff_maker):  # noqa: F811
    """«Всем сдать отчёт»: одно на всех закрыл бы первый закрывший."""
    rol = role_maker("Получатели рассылки", ["tasks.view", "tasks.edit"])
    lyudi = [staff_maker(f"napom-vse{n}@test.local", rol["id"]) for n in range(2)]
    otvet = root_client.post(
        TASKS, json={"title": "Сдать отчёт", "poluchateli": [_moy_id(c) for c in lyudi], "kazhdomu": True}
    )
    assert otvet.status_code == 201, otvet.text
    items = otvet.json()["items"]
    assert len(items) == 2 and items[0]["id"] != items[1]["id"]
    lyudi[0].post(f"{TASKS}/{items[0]['id']}/done")
    assert root_client.get(f"{TASKS}/{items[1]['id']}").json()["is_done"] is False


def test_obshchaya_polka_vidna_vsem_i_beryotsya(root_client, role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Берут с полки", ["tasks.view", "tasks.edit"])
    kto = staff_maker("napom-polka@test.local", rol["id"])
    na_polke = _zavesti(root_client, title="Разобрать почту", obshchee=True)
    assert na_polke["id"] in [z["id"] for z in kto.get(TASKS, params={"kto": "polka"}).json()["items"]]

    vzyal = kto.post(f"{TASKS}/{na_polke['id']}/take")
    assert vzyal.status_code == 200, vzyal.text
    assert vzyal.json()["obshchee"] is False and vzyal.json()["moyo"] is True


# --- повторы -------------------------------------------------------------------


def test_zakrytoe_povtoryayushcheesya_uezzhaet_na_sleduyushchiy_raz(root_client):
    srok = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    task = _zavesti(root_client, title="Полить цветы", due_at=srok.isoformat(), povtor="FREQ=DAILY;INTERVAL=3")
    zakryto = root_client.post(f"{TASKS}/{task['id']}/done").json()
    assert zakryto["is_done"] is False
    novyy = datetime.fromisoformat(zakryto["due_at"])
    assert novyy - srok.replace(tzinfo=None) == timedelta(days=3) or (
        novyy.replace(tzinfo=timezone.utc) - srok == timedelta(days=3)
    )
    istoriya = root_client.get(f"{TASKS}/{task['id']}").json()["istoriya"]
    assert [e["vid"] for e in istoriya][:2] == ["done", "created"]


def test_povtor_s_count_zakryvaetsya_na_poslednem(root_client):
    task = _zavesti(root_client, title="Три раза", due_at=_cherez(hours=1), povtor="FREQ=DAILY;COUNT=2")
    assert root_client.post(f"{TASKS}/{task['id']}/done").json()["is_done"] is False
    assert root_client.post(f"{TASKS}/{task['id']}/done").json()["is_done"] is True


def test_propustit_ne_schitaetsya_sdelannym(root_client):
    task = _zavesti(root_client, title="Пропускаемое", due_at=_cherez(hours=1), povtor="FREQ=WEEKLY")
    otvet = root_client.post(f"{TASKS}/{task['id']}/skip").json()
    assert otvet["is_done"] is False and otvet["due_at"] != task["due_at"]
    vidy = [e["vid"] for e in root_client.get(f"{TASKS}/{task['id']}").json()["istoriya"]]
    assert "skipped" in vidy and "done" not in vidy


def test_povtor_bez_sroka_otvergaetsya(root_client):
    otvet = root_client.post(TASKS, json={"title": "Без срока", "povtor": "FREQ=DAILY"})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "povtor_bez_sroka"


def test_ves_den_zvonit_v_devyat_po_poyasu(root_client):
    task = _zavesti(
        root_client, title="Весь день", due_at="2027-03-10T15:00:00Z", ves_den=True, poyas="Europe/Kyiv"
    )
    # 10 марта 2027 в Киеве зима, UTC+2: 09:00 по местному — 07:00 UTC.
    assert task["ves_den"] is True and task["due_at"].startswith("2027-03-10T07:00")


@pytest.mark.parametrize(
    "pola",
    [
        {"opovesheniya": "0,-5"},
        {"opovesheniya": "0,1,2,3,4,5"},
        {"opovesheniya": "abc"},
        {"nastoychivo": 7},
        {"povtor": "FREQ=HOURLY"},
        {"poyas": "Mars/Olympus"},
    ],
)
def test_neponyatnye_nastroyki_zvonka_otvergayutsya(root_client, pola):
    otvet = root_client.post(TASKS, json={"title": "Кривое", "due_at": _cherez(hours=1), **pola})
    assert otvet.status_code == 422, otvet.text


# --- звонки --------------------------------------------------------------------


def test_zvonok_v_srok_odin_raz(root_client):
    """Два шага планировщика над одной минутой — один звонок: ключ его и держит."""
    srok = now_utc().replace(microsecond=0) + timedelta(minutes=2)
    task = _zavesti(root_client, title="Позвонить в срок", due_at=srok.replace(tzinfo=timezone.utc).isoformat())
    assert _tick(srok - timedelta(minutes=1)) >= 0
    assert [z for z in _zvonki(task["id"]) if z[1] == "due"] == []
    _tick(srok + timedelta(seconds=30))
    _tick(srok + timedelta(seconds=50))
    zvonki = [z for z in _zvonki(task["id"]) if z[1] == "due"]
    assert len(zvonki) == 1 and zvonki[0][0] == _moy_id(root_client)
    moi = root_client.get(f"{TASKS}/signals").json()["items"]
    assert task["id"] in [z["task_id"] for z in moi]


def test_zaranee_i_nastoychivo(root_client):
    srok = now_utc().replace(microsecond=0) + timedelta(minutes=30)
    task = _zavesti(
        root_client, title="Срочно-срочно", due_at=srok.replace(tzinfo=timezone.utc).isoformat(),
        opovesheniya="15,0", nastoychivo=5,
    )
    _tick(srok - timedelta(minutes=15) + timedelta(seconds=10))
    assert [z[1] for z in _zvonki(task["id"])] == ["early"]
    _tick(srok + timedelta(minutes=5, seconds=10))
    assert sorted(z[1] for z in _zvonki(task["id"])) == ["due", "early", "nag"]

    # Отозвался — настойчивость смолкает.
    ids = [z["id"] for z in root_client.get(f"{TASKS}/signals").json()["items"] if z["task_id"] == task["id"]]
    assert root_client.post(f"{TASKS}/signals/ack", json={"ids": ids}).status_code == 200
    _tick(srok + timedelta(minutes=10, seconds=10))
    assert sorted(z[1] for z in _zvonki(task["id"])).count("nag") == 1


def test_otlozhit_glushit_i_budit(root_client):
    srok = now_utc().replace(microsecond=0) + timedelta(minutes=1)
    task = _zavesti(root_client, title="Отложу", due_at=srok.replace(tzinfo=timezone.utc).isoformat(), nastoychivo=5)
    do = srok + timedelta(minutes=20)
    otvet = root_client.post(f"{TASKS}/{task['id']}/snooze", json={"do": do.replace(tzinfo=timezone.utc).isoformat()})
    assert otvet.status_code == 200, otvet.text
    _tick(srok + timedelta(minutes=5, seconds=10))
    assert [z for z in _zvonki(task["id"]) if z[1] in ("due", "nag")] == []
    _tick(do + timedelta(seconds=10))
    assert [z[1] for z in _zvonki(task["id"])] == ["snooze"]


def test_zakrytoe_ne_zvonit(root_client):
    srok = now_utc().replace(microsecond=0) + timedelta(minutes=1)
    task = _zavesti(root_client, title="Уже сделал", due_at=srok.replace(tzinfo=timezone.utc).isoformat())
    root_client.post(f"{TASKS}/{task['id']}/done")
    _tick(srok + timedelta(seconds=10))
    assert _zvonki(task["id"]) == []


def test_vyklyuchennyy_blok_ne_zvonit(root_client):
    srok = now_utc().replace(microsecond=0) + timedelta(minutes=1)
    task = _zavesti(root_client, title="Блок выключат", due_at=srok.replace(tzinfo=timezone.utc).isoformat())
    assert root_client.post(f"{API}/modules/tasks", json={"enabled": False}).status_code == 200
    try:
        assert _tick(srok + timedelta(seconds=10)) == 0
    finally:
        root_client.post(f"{API}/modules/tasks", json={"enabled": True})
    assert _zvonki(task["id"]) == []


# --- привязки, шаги, ссылки ---------------------------------------------------


def test_privyazka_k_vyklyuchennomu_bloku_otvergaetsya(root_client):
    """Выключен склад — товар в напоминание не привязать.

    Склад выключается здесь же, а не берётся «по умолчанию выключенным»: в
    обратном порядке шлюза его включает соседний файл.
    """
    bylo = next(m["enabled"] for m in root_client.get(f"{API}/modules").json()["items"] if m["key"] == "warehouse")
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        otvet = root_client.post(TASKS, json={"title": "Про товар", "product_id": 1})
        assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": bylo})


def test_imena_privyazok_tolko_iz_otkrytyh_razdelov(root_client, role_maker, staff_maker):  # noqa: F811
    """Разбор 28.09.2026: общее напоминание с полки показывало имя клиента и название
    заявки тому, у кого нет `clients.view` и `deals.view`."""
    klient = root_client.post(f"{API}/clients", json={"name": "Скрытый заказчик"}).json()
    zayavka = root_client.post(f"{API}/deals", json={"title": "Скрытая сделка", "client_id": klient["id"]}).json()
    task = _zavesti(root_client, title="С полки", obshchee=True, client_id=klient["id"], deal_id=zayavka["id"])
    rol = role_maker("Только напоминания", ["tasks.view"])
    chuzhoy = staff_maker("imena-privyazok@test.local", rol["id"])

    stroka = next(s for s in chuzhoy.get(TASKS, params={"scope": "open"}).json()["items"] if s["id"] == task["id"])
    assert stroka["client_name"] is None and stroka["deal_title"] is None
    assert stroka["client_id"] == klient["id"], "номер остаётся — он ничего не рассказывает"
    kartochka = chuzhoy.get(f"{TASKS}/{task['id']}").json()
    assert kartochka["client_name"] is None and kartochka["deal_title"] is None
    svoya = root_client.get(f"{TASKS}/{task['id']}").json()
    assert svoya["client_name"] == "Скрытый заказчик" and svoya["deal_title"] == "Скрытая сделка"


def test_shagi_i_ssylki(root_client):
    task = _zavesti(root_client, title="С шагами", shagi=["Позвонить", "Выставить счёт"],
                    ssylki=[{"url": "https://example.com/dogovor", "title": "Договор"}])
    assert task["shagi"] == [0, 2]
    kartochka = root_client.get(f"{TASKS}/{task['id']}").json()
    shag = kartochka["shagi_spisok"][0]
    assert root_client.patch(f"{TASKS}/{task['id']}/steps/{shag['id']}", json={"sdelan": True}).json()["sdelan"]
    assert root_client.get(TASKS, params={"scope": "open"}).status_code == 200
    assert [s["url"] for s in kartochka["ssylki"]] == ["https://example.com/dogovor"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x", "data:text/html,1", ""])
def test_ssylka_tolko_http(root_client, url):
    """Ссылку нажимает коллега: `javascript:` в ней — удар по нему."""
    task = _zavesti(root_client, title="Ссылки")
    otvet = root_client.post(f"{TASKS}/{task['id']}/links", json={"url": url})
    assert otvet.status_code == 422, otvet.text


# --- календарь -----------------------------------------------------------------


def test_kalendar_raskladyvaet_povtory(root_client):
    srok = datetime(2031, 5, 4, 7, 0, tzinfo=timezone.utc)
    task = _zavesti(root_client, title="Каждую неделю", due_at=srok.isoformat(), povtor="FREQ=WEEKLY")
    otvet = root_client.get(f"{TASKS}/calendar", params={"s": "2031-05-01T00:00:00Z", "po": "2031-06-01T00:00:00Z"})
    assert otvet.status_code == 200, otvet.text
    razy = [r["srok"][:10] for r in otvet.json()["items"] if r["task"]["id"] == task["id"]]
    assert razy == ["2031-05-04", "2031-05-11", "2031-05-18", "2031-05-25"]


def test_kalendar_ne_zabivaetsya_staroy_prosrochkoy(role_maker, staff_maker, monkeypatch):  # noqa: F811
    """Разбор 28.09.2026: окно грузило открытые «до конца окна» по сроку и резало на
    потолке — старая разовая просрочка вытесняла всё, что в окне."""
    rol = role_maker("Календарь с хвостами", ["tasks.view", "tasks.create", "tasks.edit"])
    anna = staff_maker("kalendar-hvosty@test.local", rol["id"])
    for n in range(6):
        _zavesti(anna, title=f"Хвост {n}", due_at=datetime(2030, 1, 1 + n, 9, tzinfo=timezone.utc).isoformat())
    v_okne = _zavesti(anna, title="В окне", due_at=datetime(2033, 3, 10, 9, tzinfo=timezone.utc).isoformat())
    monkeypatch.setattr(task_service, "KALENDAR_LIMIT", 5)
    otvet = anna.get(f"{TASKS}/calendar", params={"s": "2033-03-01T00:00:00Z", "po": "2033-04-01T00:00:00Z"})
    assert otvet.status_code == 200, otvet.text
    assert v_okne["id"] in [r["task"]["id"] for r in otvet.json()["items"]]


def test_kalendar_okno_ne_bolshe_dvuh_mesyatsev(root_client):
    otvet = root_client.get(f"{TASKS}/calendar", params={"s": "2031-01-01T00:00:00Z", "po": "2031-06-01T00:00:00Z"})
    assert otvet.status_code == 422


# --- миграция на населённой базе ----------------------------------------------


def test_migratsiya_perenosit_lyudey_i_otkatyvaetsya(chistaya_baza, nakatit, naselit):
    """Круг d4c19e7a5b62 на живых строках: исполнитель → люди → исполнитель.

    Откат найден пробой: он брал только чужого получателя, а прежний `create`
    без исполнителя ставил им самого автора — и откат обнулял исполнителя у
    каждого личного напоминания.
    """
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from sqlalchemy import create_engine, text

    nakatit(chistaya_baza, "b5e8d3f07c14")
    dvigatel = create_engine(chistaya_baza)
    try:
        with dvigatel.begin() as s:
            naselit(s, "users", [{"id": 1, "email": "a@m.test", "name": "Анна"}, {"id": 2, "email": "b@m.test", "name": "Борис"}])
            naselit(s, "tasks", [
                {"id": 1, "title": "Себе", "created_by": 1, "assignee_id": 1},
                {"id": 2, "title": "Борису", "created_by": 1, "assignee_id": 2},
                {"id": 3, "title": "С сайта", "created_by": None, "assignee_id": None},
            ])
        nakatit(chistaya_baza, "d4c19e7a5b62")
        with dvigatel.connect() as s:
            lyudi = {tuple(r) for r in s.execute(text("SELECT task_id, user_id, vladelets, poluchaet FROM task_members"))}
            assert lyudi == {(1, 1, 1, 1), (2, 1, 1, 0), (2, 2, 0, 1)}
            assert dict(s.execute(text("SELECT id, obshchee FROM tasks")).all()) == {1: 0, 2: 0, 3: 1}

        config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", chistaya_baza)
        command.downgrade(config, "b5e8d3f07c14")
        with dvigatel.connect() as s:
            assert dict(s.execute(text("SELECT id, assignee_id FROM tasks")).all()) == {1: 1, 2: 2, 3: None}
    finally:
        dvigatel.dispose()


def test_gotovo_so_starym_srokom_ne_zakryvaet_sleduyushchiy_raz(root_client):
    """Кнопка вчерашнего окошка не закрывает сегодняшний раз повторяющегося."""
    task = _zavesti(root_client, title="Каждый день", due_at=_cherez(minutes=5), povtor="FREQ=DAILY")
    videl = task["due_at"]
    assert root_client.post(f"{TASKS}/{task['id']}/done", json={"srok": videl}).status_code == 200
    for put in ("done", "skip"):
        otvet = root_client.post(f"{TASKS}/{task['id']}/{put}", json={"srok": videl})
        assert otvet.status_code == 409 and otvet.json()["error"]["code"] == "zvonok_ustarel", put
    # Без срока — как раньше: программы, которые его не шлют, не ломаются.
    assert root_client.post(f"{TASKS}/{task['id']}/done").status_code == 200


def test_otklyuchennomu_ne_zvonit(root_client, role_maker, staff_maker):  # noqa: F811
    """Уволенному звонок не идёт: push унёс бы названия и суммы на его личный телефон."""
    rol = role_maker("Звонки — уволенный", ["tasks.view", "tasks.edit"])
    anna = staff_maker("zvon-uvolen@test.local", rol["id"])
    anna_id = _moy_id(anna)
    srok = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=2)
    task = _zavesti(root_client, title="Обоим", due_at=srok.isoformat(), poluchateli=[_moy_id(root_client), anna_id])
    assert root_client.post(f"{API}/staff/{anna_id}/disable").status_code == 200

    _tick(srok.replace(tzinfo=None) + timedelta(seconds=5))

    komu = {user_id for user_id, _vid, _m in _zvonki(task["id"])}
    assert anna_id not in komu and _moy_id(root_client) in komu


def test_nezakrytyy_raz_povtora_ne_glushit_sleduyushchie(root_client):
    """Разбор 29.09.2026: «каждый день в 18:00 закрыть кассу» — один вечер никто не
    нажал «Готово», и срок остался вчерашним. Кандидаты на звонок — только со сроком
    в окне, и через 12 часов напоминание замолкало навсегда: ни сегодняшний раз, ни
    завтрашний уже не звонили. Теперь незакрытый раз уступает место следующему."""
    from database.models import Task, TaskEvent

    vchera = now_utc() - timedelta(hours=13)
    task = _zavesti(
        root_client, title="Закрыть кассу", due_at=vchera.replace(tzinfo=timezone.utc).isoformat(),
        povtor="FREQ=DAILY", opovesheniya="0",
    )
    teper = now_utc()
    _tick(teper)
    with SessionLocal() as db:
        stroka = db.get(Task, task["id"])
        assert stroka.due_at > teper - timedelta(minutes=15), f"срок остался в прошлом: {stroka.due_at}"
        novyy = stroka.due_at
        sobytiya = [e.vid for e in db.scalars(select(TaskEvent).where(TaskEvent.task_id == task["id"]))]
    assert "missed" in sobytiya, sobytiya

    assert _tick(novyy + timedelta(minutes=1)) >= 1, "следующий раз не позвонил"
    assert any(vid == "due" and moment == novyy for _u, vid, moment in _zvonki(task["id"]))
    # Второй проход ничего не двигает дважды.
    _tick(novyy + timedelta(minutes=2))
    with SessionLocal() as db:
        assert db.get(Task, task["id"]).due_at == novyy


def test_povtor_s_nesushchestvuyushchim_until_i_razdutyy_otvergayutsya(root_client):
    """Разбор 29.09.2026: `UNTIL=20260230` проходил регулярку формы, правило
    сохранялось — и календарь, «Готово» и пропуск падали `ValueError` у всех, кто
    видит напоминание. BYDAY из 90 × MO раздувал правило за колонку (255) — 500 уже
    при заведении."""
    for pravilo in ("FREQ=DAILY;UNTIL=20260230", "FREQ=DAILY;UNTIL=20261301T000000Z"):
        otvet = root_client.post(TASKS, json={"title": "Кривой повтор", "due_at": _cherez(hours=1), "povtor": pravilo})
        assert otvet.status_code == 422, f"{pravilo}: {otvet.status_code} {otvet.text[:200]}"
    razdutoe = "FREQ=WEEKLY;BYDAY=" + ",".join(["MO"] * 90)
    otvet = root_client.post(TASKS, json={"title": "Раздутый повтор", "due_at": _cherez(hours=1), "povtor": razdutoe})
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["povtor"] == "FREQ=WEEKLY;BYDAY=MO", otvet.json()["povtor"]
    assert root_client.get(f"{TASKS}/calendar", params={"from": _cherez(days=-1), "to": _cherez(days=30)}).status_code in (200, 422)


def test_zvonok_dogonyaet_prostoy_planirovshchika(root_client):
    """Разбор 29.09.2026: окно звонка — «последние 15 минут», без отметки прошлого
    шага. Простой дольше окна (обновление с миграцией, откат, перезагрузка) терял
    всё, что должно было прозвенеть за него, — молча и навсегда. Теперь окно
    начинается от последнего удачного шага."""
    from core import redis_client
    from core.services import zvonki_service

    task = _zavesti(root_client, title="Звонок за простой", due_at=_cherez(minutes=-40), opovesheniya="0")
    klient = redis_client.get_client()
    klient.set(zvonki_service.KLYUCH_OTMETKI, (now_utc() - timedelta(minutes=60)).isoformat())
    try:
        zvonki_service.shag()
    finally:
        klient.delete(zvonki_service.KLYUCH_OTMETKI)
    assert any(vid == "due" for _u, vid, _m in _zvonki(task["id"])), "звонок, пришедшийся на простой, потерян"

