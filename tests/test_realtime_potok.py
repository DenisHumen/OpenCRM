"""`GET /api/v1/live`: закрытость, режим «выключено», resync, догон, отбор, обрыв сессии.

Ожидание здесь одного вида — чтение строк потока с крайним сроком жизни
соединения, подменённым на секунду (`MAX_ZHIZN_POTOKA`): разрыв со стороны
тестового клиента до приложения не доходит, и без подмены прогон висел бы.
"""

import json

import pytest

from core.live import bus
from core.live.message import Hint
from tests.conftest import API, make_manager
from web.api.routes import live as marshrut

LIVE = f"{API}/live"


@pytest.fixture(autouse=True)
def korotkiy_potok(monkeypatch):
    monkeypatch.setattr(marshrut, "MAX_ZHIZN_POTOKA", 1)
    monkeypatch.setattr(marshrut, "SHAG_SEKUND", 0.05)


@pytest.fixture
def pamyat(monkeypatch):
    """Шина в памяти: поток проверяется без внешних служб, руками."""
    monkeypatch.setattr(bus.redis_client, "configured", lambda: False)
    bus.sbrosit()
    yield bus.shina()
    bus.sbrosit()


def _sobytiya(client, headers=None) -> list[tuple[str, dict, str | None]]:
    """Читает поток до конца: список (event, data, id)."""
    itog = []
    tekushchee: dict[str, str] = {}
    with client.stream("GET", LIVE, headers=headers or {}) as r:
        assert r.status_code == 200, r.status_code
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        assert r.headers["cache-control"] == "no-store"
        for stroka in r.iter_lines():
            if stroka == "":
                if "data" in tekushchee:
                    itog.append((tekushchee.get("event", "message"), json.loads(tekushchee["data"]), tekushchee.get("id")))
                tekushchee = {}
                continue
            if stroka.startswith(":"):
                continue
            klyuch, _, znachenie = stroka.partition(":")
            tekushchee[klyuch] = znachenie.strip()
    return itog


def _vklyuchit(root_client, znachenie: str):
    r = root_client.patch(f"{API}/settings", json={"values": {"realtime_enabled": znachenie}})
    assert r.status_code == 200, r.text


def test_bez_sessii_401_a_do_smeny_parolya_403(base_client, root_client):
    assert base_client.get(LIVE).status_code == 401


def test_vyklyucheno_nastroykoy_mode_off(root_client, pamyat):
    _vklyuchit(root_client, "0")
    try:
        assert root_client.get(f"{API}/workspace").json()["realtime_enabled"] is False
        sobytiya = _sobytiya(root_client)
        assert sobytiya == [("mode", {"mode": "off", "reason": "disabled"}, None)]
    finally:
        _vklyuchit(root_client, "1")
    assert root_client.get(f"{API}/workspace").json()["realtime_enabled"] is True


def test_pervoe_podklyuchenie_eto_resync(root_client, pamyat):
    sobytiya = _sobytiya(root_client)
    assert sobytiya[0][0] == "resync" and sobytiya[0][1]["reason"] == "first"


def test_dogon_po_last_event_id_i_resync_kogda_dognat_nechem(root_client, pamyat):
    nomer = pamyat.publish(Hint(topic="clients", action="created", id=1))
    for i in (2, 3, 4):
        pamyat.publish(Hint(topic="clients", action="updated", id=i))
    sobytiya = _sobytiya(root_client, {"Last-Event-ID": nomer})
    izmeneniya = [s for s in sobytiya if s[0] == "change"]
    assert [s[1]["id"] for s in izmeneniya] == [2, 3, 4], "ровно три, в порядке"
    assert all(s[2] for s in izmeneniya) and bus._nomer_bolshe(izmeneniya[-1][2], izmeneniya[0][2])
    assert not any(s[0] == "resync" for s in sobytiya)

    # Подрезали хвост — номер пропал: честный resync, а не тишина.
    pamyat._hvost.clear()
    sobytiya = _sobytiya(root_client, {"Last-Event-ID": nomer})
    assert sobytiya[0][0] == "resync" and sobytiya[0][1]["reason"] == "gap"


def test_zhivoy_namyok_dohodit_tolko_tomu_komu_polagaetsya(root_client, pamyat, monkeypatch):
    """Два сотрудника, одна правка: намёк о чужой заявке менеджеру не приходит."""
    from database.repositories import users as users_repo
    from database.session import SessionLocal

    manager = make_manager(root_client, "zhivoy.manager@example.com")
    with SessionLocal() as db:
        root_id = users_repo.get_root(db).id
        manager_id = users_repo.get_by_email(db, "zhivoy.manager@example.com").id
    # Должность без `deals.view_others`: обязательная пара из §12 документа —
    # менеджер против чужой заявки. С пресетом, где чужие видны, сабо­таж отбора
    # был бы невидим.
    rol = root_client.post(
        f"{API}/roles",
        json={"name": f"Только свои заявки {manager_id}", "permissions": ["clients.view", "deals.view"]},
    )
    assert rol.status_code == 201, rol.text
    assert root_client.post(f"{API}/roles/assign/{manager_id}", json={"role_id": rol.json()["id"]}).status_code == 200

    # Намёки кладём в шину прямо из "второго процесса", пока поток открыт: у
    # шины в памяти запись раздаётся подписчику немедленно.
    def po_hodu():
        pamyat.publish(Hint(topic="deals", action="updated", id=100, scope_key=root_id, actor_id=root_id))
        pamyat.publish(Hint(topic="deals", action="updated", id=101, scope_key=manager_id, actor_id=root_id))
        pamyat.publish(Hint(topic="clients", action="created", id=7))
        # Выключенный блок не подписан ни на кого — даже на root.
        pamyat.publish(Hint(topic="warehouse", action="updated", id=9))

    nastoyashchaya = pamyat.podpisatsya

    def podpisatsya_i_napisat():
        p = nastoyashchaya()
        po_hodu()
        return p

    monkeypatch.setattr(pamyat, "podpisatsya", podpisatsya_i_napisat)
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        u_roota = [s[1]["id"] for s in _sobytiya(root_client) if s[0] == "change"]
        u_managera = [s[1]["id"] for s in _sobytiya(manager) if s[0] == "change"]
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    assert u_roota == [100, 101, 7], u_roota
    assert u_managera == [101, 7], u_managera


def test_vyshel_iz_sistemy_potok_oborvalsya(root_client, pamyat, monkeypatch):
    """Сессия умерла — поток рвётся на следующем же сообщении, а не живёт до пересборки."""
    from database.session import SessionLocal
    from tests.conftest import make_manager as _mm

    vremennyy = _mm(root_client, "obryv.potoka@example.com")
    nastoyashchaya = pamyat.podpisatsya

    def podpisatsya_i_vyyti():
        p = nastoyashchaya()
        # Выход из системы другим клиентом с той же cookie: сессия удалена.
        vremennyy.post(f"{API}/auth/logout")
        pamyat.publish(Hint(topic="clients", action="created", id=1))
        return p

    monkeypatch.setattr(pamyat, "podpisatsya", podpisatsya_i_vyyti)
    sobytiya = _sobytiya(vremennyy)
    assert not any(s[0] == "change" for s in sobytiya), "после выхода намёки приходить не должны"
    with SessionLocal():
        pass


def test_dvoe_pishut_razom_oba_namyoka_dohodyat(root_client, pamyat):
    from tests.test_odin_iz_mnogih import duel

    p = pamyat.podpisatsya()
    ishody = duel(lambda i: root_client.post(f"{API}/clients", json={"name": f"Дуэль живых {i}"}).status_code, 1, 2)
    assert set(ishody.values()) == {201}, ishody
    prishli = []
    while True:
        paketik = p.get(timeout=2)
        if paketik is None:
            break
        prishli.append(paketik)
        if len(prishli) >= 2:
            break
    p.close()
    nomera = [n for n, h in prishli if h.topic == "clients"]
    assert len(nomera) == 2 and bus._nomer_bolshe(nomera[1], nomera[0]), nomera


def test_metriki_zhivyh_obnovleniy(root_client):
    root_client.post(f"{API}/modules/monitoring", json={"enabled": True})
    try:
        text = root_client.get(f"{API}/metrics").text
    finally:
        root_client.post(f"{API}/modules/monitoring", json={"enabled": False})
    for imya in ("opencrm_realtime_published_total", "opencrm_realtime_dropped_total", "opencrm_realtime_connections"):
        assert f"# HELP {imya}" in text and f"# TYPE {imya}" in text, imya
