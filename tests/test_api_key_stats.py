"""Статистика обращений по ключам сайта: считается на каждом запросе, лежит в базе.

Просьба владельца 05.09.2026: сколько запросов в день, неделю, месяц, среднее,
нагрузка, по видам, графики живьём. Хранится строкой на (ключ, час, область);
отказ по потолку — тоже обращение; старое убирает ночная уборка.
"""

from datetime import timedelta

import pytest

from core.services import api_key_service, api_stats_service
from core.utils import now_utc
from database.repositories import api_key_hits as repo
from database.session import SessionLocal
from tests.conftest import API

SITE = f"{API}/site"
KEYS = f"{API}/settings/api-keys"
H = api_key_service.HEADER


@pytest.fixture(autouse=True)
def blocks_on(root_client):
    for key in ("documents", "warehouse", "orders", "waybills"):
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})


def klyuch(root_client, **extra) -> dict:
    r = root_client.post(KEYS, json={"name": "магазин", "scopes": ["catalog.read"], **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_obrashcheniya_schitayutsya_po_klyuchu_i_po_vidu(root_client):
    k = klyuch(root_client)
    for _ in range(3):
        assert root_client.get(f"{SITE}/catalog", headers={H: k["key"]}).status_code == 200

    svodka = root_client.get(f"{KEYS}/{k['id']}/stats").json()
    assert svodka["today"] == 3 and svodka["week"] == 3 and svodka["month"] == 3
    assert svodka["rejected_month"] == 0
    assert svodka["by_category"] == [{"category": "catalog.read", "count": 3, "share": 1.0}]
    assert len(svodka["by_day"]) == 30 and svodka["by_day"][-1]["count"] == 3
    assert len(svodka["by_hour"]) == 24 and svodka["by_hour"][-1]["count"] == 3
    assert svodka["peak_hour"] == 3 and svodka["rate_per_min"] == k["rate_per_min"]

    stroka = next(i for i in root_client.get(KEYS).json()["items"] if i["id"] == k["id"])
    assert stroka["hits_30d"] == 3, "число за месяц стоит прямо в списке ключей"


def test_otkaz_po_potolku_tozhe_obrashchenie(root_client):
    k = klyuch(root_client, rate_per_min=1)
    assert root_client.get(f"{SITE}/catalog", headers={H: k["key"]}).status_code == 200
    assert root_client.get(f"{SITE}/catalog", headers={H: k["key"]}).status_code == 429
    svodka = root_client.get(f"{KEYS}/{k['id']}/stats").json()
    assert svodka["month"] == 1, "отказанный не считается выполненным"
    assert svodka["rejected_month"] == 1, "но и не пропадает: по нему видно, что потолок мал"


def test_chuzhoy_klyuch_ne_schitaet_i_svodka_chuzhogo_ne_otdayotsya(root_client):
    k = klyuch(root_client)
    assert root_client.get(f"{SITE}/catalog", headers={H: "opencrm_nikakoy"}).status_code == 401
    assert root_client.get(f"{KEYS}/{k['id']}/stats").json()["month"] == 0
    assert root_client.get(f"{KEYS}/999999/stats").status_code == 404


def test_uborka_ubiraet_starye_chasy_i_ostavlyaet_svezhie(root_client):
    k = klyuch(root_client)
    assert root_client.get(f"{SITE}/catalog", headers={H: k["key"]}).status_code == 200
    staryy = (now_utc() - timedelta(days=api_stats_service.HRANIT_DNEY + 1)).replace(
        minute=0, second=0, microsecond=0, tzinfo=None
    )
    with SessionLocal() as db:
        repo.zapisat(db, k["id"], staryy, "catalog.read", rejected=False)
        db.commit()
        assert len(repo.stroki(db, k["id"], staryy)) == 2
        assert api_stats_service.ubrat_starye(db) == 1
        db.commit()
        ostalis = repo.stroki(db, k["id"], staryy)
    assert [s.count for s in ostalis] == [1], "свежий час на месте, старый убран"
