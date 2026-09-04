"""Живые обновления, серверная половина: намёк, буфер, шина, отбор, карта.

Что стережётся (`docs/12-realtime.md` §12):

- намёк несёт только то, чего не прячет ни одно право, и отказывает лишнему полю;
- откат — ничего не отправлено; фиксация — ровно один намёк, склеенный по записи;
- упавшая отправка не роняет фиксацию;
- шина в памяти отдаёт подписчикам по порядку и молчит снятой подписке;
- поток Redis: `MAXLEN` соблюдён, ключ под приставкой, лежащий Redis — потеря,
  а не отказ и не переход на память;
- отбор: блок → право → строка, по отдельности на каждую причину;
- карта тем полна: каждая модель и каждое событие названы.
"""

import json

import pytest
from sqlalchemy import event

from core.live import access, bus, collector, topics
from core.live.message import Hint
from database.models import Client, Deal
from database.session import Base, SessionLocal
from tests.conftest import API


# --- намёк -------------------------------------------------------------------


def test_namyok_sobiraetsya_i_razbiraetsya_a_lishnee_pole_otvergaetsya():
    hint = Hint(topic="deals", action="updated", id=42, scope_key=7, actor_id=3, module="deals")
    assert json.loads(hint.to_json()) == {
        "topic": "deals", "action": "updated", "id": 42, "scope_key": 7, "actor_id": 3, "module": "deals",
    }
    assert Hint.from_json(hint.to_json()) == hint
    with pytest.raises(ValueError, match="must not"):
        Hint.from_json('{"topic":"deals","action":"updated","id":1,"amount":100}')
    with pytest.raises(ValueError):
        Hint(topic="deals", action="exploded")


# --- буфер и фиксация ------------------------------------------------------------


@pytest.fixture
def ushedshie(monkeypatch):
    """Что ушло в шину. Подмена `bus.publish` — сама шина здесь не нужна."""
    ushlo: list[Hint] = []
    monkeypatch.setattr(bus, "publish", lambda hint: ushlo.append(hint) or "1-0")
    return ushlo


def test_otkat_ne_otpravlyaet_a_fiksatsiya_otpravlyaet_odin_namyok(root_client, ushedshie):
    with SessionLocal() as db:
        db.add(Client(name="Живой клиент — откат"))
        db.flush()
        db.rollback()
        assert ushedshie == []
        # Та же сессия живёт дальше и фиксирует другое: намёк об откаченном не
        # должен уехать «прицепом» — буфер обязан опустеть на откате.
        klient = Client(name="Живой клиент — фиксация")
        db.add(klient)
        db.flush()
        # Поправили до фиксации — всё равно одно «создано», а не два намёка.
        klient.company = "ООО"
        db.flush()
        db.commit()
    po_klientu = [h for h in ushedshie if h.topic == "clients"]
    assert len(po_klientu) == 1, po_klientu
    assert po_klientu[0].action == "created" and po_klientu[0].id == klient.id


def test_upavshiy_podpischik_ne_ronyaet_fiksatsiyu(root_client, monkeypatch):
    def padaet(_hint):
        raise RuntimeError("шина сломана")

    monkeypatch.setattr(bus, "publish", padaet)
    with SessionLocal() as db:
        db.add(Client(name="Клиент при сломанной шине"))
        db.commit()
        # Фиксация прошла: запись на месте.
        assert db.get(Client, db.scalar(__import__("sqlalchemy").select(Client.id).where(Client.name == "Клиент при сломанной шине")))


def test_namyok_zayavki_nesyot_otvetstvennogo_i_avtora(root_client, ushedshie):
    from database.models.client import Client as _C

    with SessionLocal() as db:
        klient = _C(name="Клиент под заявку")
        db.add(klient)
        db.flush()
        db.info[collector.ACTOR] = 1
        db.add(Deal(title="Заявка живая", client_id=klient.id, manager_id=1, stage="new"))
        db.commit()
    zayavki = [h for h in ushedshie if h.topic == "deals"]
    assert zayavki and zayavki[-1].scope_key == 1 and zayavki[-1].actor_id == 1
    assert zayavki[-1].module == "deals"


def test_otmetka_prisutstviya_namyoka_ne_rozhdaet(root_client, ushedshie):
    """Сердцебиение пишет `last_seen_at` каждому сотруднику раз в минуту; намёк на
    это гнал бы всех на перечитку штата ради точки «в сети». Поймано воротами:
    в догон приехал лишний намёк `staff` о том, кто сам и подключился."""
    from core.utils import now_utc
    from database.repositories import users as users_repo

    with SessionLocal() as db:
        root = users_repo.get_root(db)
        root.last_seen_at = now_utc().replace(tzinfo=None)
        db.commit()
        assert [h for h in ushedshie if h.topic == "staff"] == []
        root.name = root.name  # без изменений — тоже тишина
        db.commit()
        assert [h for h in ushedshie if h.topic == "staff"] == []
        root.locale = "ru" if root.locale != "ru" else "en"
        db.commit()
        assert [h.id for h in ushedshie if h.topic == "staff"] == [root.id], "настоящая правка карточки — намёк"
        root.locale = "ru" if root.locale != "ru" else "en"
        db.commit()


def test_yavnyy_announce_uezzhaet_posle_fiksatsii(root_client, ushedshie):
    with SessionLocal() as db:
        collector.announce(db, Hint(topic="warehouse", action="updated", id=5))
        assert ushedshie == []
        db.commit()
    assert Hint(topic="warehouse", action="updated", id=5) in ushedshie


# --- шина в памяти --------------------------------------------------------------


@pytest.fixture
def pamyat(monkeypatch):
    monkeypatch.setattr(bus.redis_client, "configured", lambda: False)
    bus.sbrosit()
    yield bus.shina()
    bus.sbrosit()


def test_shina_v_pamyati_razdayot_po_poryadku_i_molchit_snyatoy_podpiske(pamyat):
    assert isinstance(pamyat, bus._Pamyat)
    a, b = pamyat.podpisatsya(), pamyat.podpisatsya()
    n1 = pamyat.publish(Hint(topic="clients", action="created", id=1))
    n2 = pamyat.publish(Hint(topic="clients", action="updated", id=1))
    assert bus._nomer_bolshe(n2, n1)
    assert [x[1].action for x in (a.get(), a.get())] == ["created", "updated"]
    assert [x[1].action for x in (b.get(), b.get())] == ["created", "updated"]
    a.close()
    pamyat.publish(Hint(topic="clients", action="deleted", id=1))
    assert a.get() is None and b.get()[1].action == "deleted"
    # Подписчиков может быть ноль — это не ошибка.
    b.close()
    assert pamyat.publish(Hint(topic="clients", action="created", id=2))


def test_dogon_v_pamyati_i_resync(pamyat):
    nomera = [pamyat.publish(Hint(topic="clients", action="updated", id=i)) for i in range(3)]
    assert [h.id for _, h in pamyat.catch_up(nomera[0])] == [1, 2]
    assert pamyat.catch_up("0-0") is None, "номера нет в хвосте — догнать нечем"
    assert pamyat.catch_up(nomera[-1]) == []


# --- поток Redis -------------------------------------------------------------------


class FakeStreamRedis:
    """Ровно те четыре команды, которыми пользуется шина: xadd, xrange, xread, ping."""

    def __init__(self):
        self.streams: dict[str, list[tuple[bytes, dict]]] = {}
        self.maxlens: list[int] = []
        self.schyot = 0

    def ping(self):
        return True

    def xadd(self, key, fields, maxlen=None, approximate=False):
        self.maxlens.append(maxlen)
        self.schyot += 1
        nomer = f"{1000 + self.schyot}-0".encode()
        self.streams.setdefault(key, []).append((nomer, {k.encode(): v.encode() for k, v in fields.items()}))
        if maxlen is not None:
            self.streams[key] = self.streams[key][-maxlen:]
        return nomer

    def xrange(self, key, min="-", max="+", count=None):
        zapisi = self.streams.get(key, [])
        if min not in ("-",):
            strogo = min.startswith("(")
            granitsa = (min[1:] if strogo else min).encode()
            zapisi = [z for z in zapisi if (z[0] > granitsa if strogo else z[0] >= granitsa)]
        return zapisi[:count] if count else zapisi

    def xread(self, streams, block=None, count=None):
        return []


class BrokenStreamRedis(FakeStreamRedis):
    def xadd(self, *a, **k):
        raise ConnectionError("redis down")

    def ping(self):
        return False


@pytest.fixture
def potok(monkeypatch):
    fake = FakeStreamRedis()
    monkeypatch.setattr(bus.redis_client, "configured", lambda: True)
    monkeypatch.setattr(bus.redis_client, "get_client", lambda: fake)
    bus.sbrosit()
    shina = bus.shina()
    yield shina, fake
    bus.sbrosit()


def test_potok_pishet_pod_pristavkoy_s_potolkom(potok):
    shina, fake = potok
    assert isinstance(shina, bus._Potok)
    nomer = shina.publish(Hint(topic="deals", action="updated", id=1, scope_key=2))
    assert nomer == "1001-0"
    assert list(fake.streams) == [f"{bus.redis_client.PREFIX}live:stream"]
    assert fake.maxlens == [bus.MAXLEN] and bus.MAXLEN <= 1000, "хвост длиннее тысячи вытесняет счётчики входа"
    hvost = shina.catch_up("1001-0")
    assert hvost == []
    shina.publish(Hint(topic="deals", action="deleted", id=1))
    assert [h.action for _, h in shina.catch_up("1001-0")] == ["deleted"]
    assert shina.catch_up("999-0") is None, "номер старше хвоста — resync, а не половина"


def test_redis_lyog_namyok_teryaetsya_a_zapis_prokhodit(monkeypatch, root_client):
    """Правило §11: тишина, а не отказ; и на память при заданном Redis не падаем."""
    monkeypatch.setattr(bus.redis_client, "configured", lambda: True)
    monkeypatch.setattr(bus.redis_client, "get_client", lambda: BrokenStreamRedis())
    bus.sbrosit()
    try:
        assert isinstance(bus.shina(), bus._Potok), "при заданном Redis шина в памяти незаконна"
        bylo = bus.dropped_total
        r = root_client.post(f"{API}/clients", json={"name": "Клиент при лежащем Redis"})
        assert r.status_code == 201, r.text
        assert bus.dropped_total > bylo
        assert bus.zhiva() is False
    finally:
        bus.sbrosit()


# --- отбор ---------------------------------------------------------------------


def test_otbor_tri_prichiny_otkaza_po_otdelnosti(root_client, manager_client):
    from core.services import permissions_service
    from database.repositories import users as users_repo

    with SessionLocal() as db:
        root = users_repo.get_root(db)
        manager = users_repo.get_by_email(db, manager_client.headers.get("x-test-email", "") or "manager@example.com")
        if manager is None:
            manager = next(u for u in users_repo.list_staff(db) if u.role != "root")
        svoya = Hint(topic="deals", action="updated", id=1, scope_key=manager.id)
        chuzhaya = Hint(topic="deals", action="updated", id=2, scope_key=root.id)
        bez_otvetstvennogo = Hint(topic="deals", action="updated", id=3, scope_key=None)
        assert access.delivers(db, root, chuzhaya)
        assert access.delivers(db, root, bez_otvetstvennogo)
        vidit_vse = permissions_service.deals_scope(db, manager) is None
        assert access.delivers(db, manager, svoya)
        assert access.delivers(db, manager, chuzhaya) is vidit_vse
        assert access.delivers(db, manager, bez_otvetstvennogo) is vidit_vse

        # Право: склад менеджеру не полагается, если у него нет warehouse.view
        # (и блок при этом обязан быть включён — он выключен по умолчанию).
        from core.services import modules_service

        sklad = Hint(topic="warehouse", action="updated", id=1)
        polozhen = modules_service.is_enabled(db, "warehouse") and permissions_service.has(db, manager, "warehouse", "view")
        assert access.delivers(db, manager, sklad) is bool(polozhen)
        # Тема без блока и права — всем.
        assert access.delivers(db, manager, Hint(topic="modules", action="updated", id=1))
        assert access.delivers(db, manager, Hint(topic="no_such_topic", action="updated", id=1)) is False

    # Блок: выключенный склад не доставляется никому, даже root.
    root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
    try:
        with SessionLocal() as db:
            root = users_repo.get_root(db)
            assert access.delivers(db, root, Hint(topic="warehouse", action="updated", id=1)) is False
    finally:
        root_client.post(f"{API}/modules/warehouse", json={"enabled": True})


# --- карта тем -----------------------------------------------------------------


def test_karta_tem_nazyvaet_kazhduyu_model():
    """Новая модель без строки в карте роняет набор, а не молчит."""
    klassy = {m.class_ for m in Base.registry.mappers}
    bez_otveta = sorted(k.__name__ for k in klassy if k not in topics.TOPICS)
    assert bez_otveta == [], f"в карте тем нет ответа за: {bez_otveta}"
    lishnie = sorted(k.__name__ for k in topics.TOPICS if k not in klassy)
    assert lishnie == [], f"в карте тем модели, которых нет: {lishnie}"


def test_temy_ssylayutsya_na_nastoyashchie_bloki_i_oblasti():
    from core import modules as core_modules
    from core import permissions as core_permissions

    for tema in topics.BY_NAME.values():
        if tema.module is not None:
            assert core_modules.get(tema.module) is not None, f"тема {tema.name}: блока {tema.module} нет"
        if tema.area is not None:
            assert core_permissions.exists(tema.area, "view"), f"тема {tema.name}: права {tema.area}.view нет"


def test_kazhdoe_sobytie_nazvano_v_karte():
    import re
    from pathlib import Path

    koren = Path(__file__).resolve().parent.parent
    imena = set()
    for put in (koren / "core" / "services").glob("*.py"):
        imena |= set(re.findall(r'^[A-Z_]+ = "([a-z_]+\.[a-z_]+)"', put.read_text(encoding="utf-8"), re.M))
    sobytiya = {i for i in imena if i.split(".")[0] in ("deal", "order", "act", "lead", "stock", "document", "waybill")}
    bez_otveta = sorted(sobytiya - set(topics.EVENT_TOPICS))
    assert bez_otveta == [], f"события без ответа в карте тем: {bez_otveta}"
    for tema in topics.EVENT_TOPICS.values():
        assert tema is None or tema in topics.BY_NAME
