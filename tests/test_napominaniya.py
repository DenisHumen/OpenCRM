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
