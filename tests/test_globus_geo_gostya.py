"""Гость витрины на глобусе: часовой пояс, тумблер доски, ограничитель.

Гео гостя — единственное место блока, куда попадают данные извне, и правил у
него три: без разрешения у клиента, только при включённом тумблере доски, и
один заход — одна отметка. Разбор — `docs/bloki/25-globus.md` §5.2.
"""
import pytest
from fastapi.testclient import TestClient

from core.geo.dannye import POYASA
from tests.conftest import API, png_bytes
from web.main import app

GLOBE = f"{API}/globe"


def _blok(client, key: str, enabled: bool) -> None:
    assert client.post(f"{API}/modules/{key}", json={"enabled": enabled}).status_code == 200


@pytest.fixture(autouse=True)
def bloki(root_client):
    _blok(root_client, "globe", True)
    _blok(root_client, "boards", True)
    yield
    _blok(root_client, "globe", False)


def _doska_so_ssylkoy(client, title: str):
    doska = client.post(f"{API}/boards", json={"title": title, "description": "Гео"}).json()
    zagruzka = client.post(
        f"{API}/boards/{doska['id']}/works",
        files={"file": ("work.png", png_bytes(), "image/png")},
    )
    assert zagruzka.status_code == 202
    client.patch(f"{API}/boards/{doska['id']}", json={"is_published": True})
    ssylka = client.post(f"{API}/boards/{doska['id']}/shares", json={})
    assert ssylka.status_code == 201, ssylka.text
    return doska, ssylka.json()


def _gost(kartina: dict, poyas: str) -> dict | None:
    imya = poyas.split("/")[-1].replace("_", " ")
    for tochka in kartina["points"]:
        if tochka["vid"] == "visitor" and tochka["imya"] == imya:
            return tochka
    return None


def test_poyas_gostya_stavit_tochku(root_client):
    """Браузер называет пояс — на планете появляется точка гостя, а связь
    ведёт к клиенту, чью доску смотрели."""
    klient = root_client.post(
        f"{API}/clients", json={"name": "Гео клиент", "country": "UA", "city": "Киев"}
    ).json()
    doska, ssylka = _doska_so_ssylkoy(root_client, "Гео доска")
    root_client.patch(f"{API}/boards/{doska['id']}", json={"client_id": klient["id"]})

    gost = TestClient(app)
    stranica = gost.get(f"/b/{ssylka['token']}")
    assert stranica.status_code == 200
    assert "/geo?tz=" in stranica.text, "маячок не поставлен на включённой доске"

    otvet = gost.post(f"/b/{ssylka['token']}/geo", params={"tz": "Europe/Berlin"})
    assert otvet.status_code == 204

    kartina = root_client.get(GLOBE).json()
    tochka = _gost(kartina, "Europe/Berlin")
    assert tochka is not None, "гость не попал на планету"
    _, shirota, dolgota = POYASA["Europe/Berlin"]
    assert round(tochka["lat"], 4) == round(shirota / 1e7, 4)
    assert round(tochka["lon"], 4) == round(dolgota / 1e7, 4)
    assert tochka["board"] == "Гео доска"

    svyaz = [s for s in kartina["links"] if s["ot"] == f"visitor:{tochka['id']}"]
    assert svyaz and svyaz[0]["k"] == f"client:{klient['id']}" and svyaz[0]["vid"] == "prosmotr"


def test_tumbler_doski_gasit_sbor(root_client):
    """Выключенный тумблер — отказ, а не тишина: страница не должна гадать."""
    doska, ssylka = _doska_so_ssylkoy(root_client, "Гео выключено")
    assert root_client.patch(
        f"{API}/boards/{doska['id']}", json={"geo_enabled": False}
    ).json()["geo_enabled"] is False

    gost = TestClient(app)
    stranica = gost.get(f"/b/{ssylka['token']}")
    assert "/geo?tz=" not in stranica.text, "маячок остался на выключенной доске"

    otvet = gost.post(f"/b/{ssylka['token']}/geo", params={"tz": "Asia/Tokyo"})
    assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "geo_off"
    assert _gost(root_client.get(GLOBE).json(), "Asia/Tokyo") is None


def test_vyklyuchennyy_globus_ne_sobiraet_poyasa(root_client):
    """Блок выключен — собирать некуда, и страница молчит."""
    doska, ssylka = _doska_so_ssylkoy(root_client, "Гео без блока")
    assert doska["geo_enabled"] is True
    _blok(root_client, "globe", False)
    try:
        gost = TestClient(app)
        assert "/geo?tz=" not in gost.get(f"/b/{ssylka['token']}").text
        otvet = gost.post(f"/b/{ssylka['token']}/geo", params={"tz": "Asia/Tokyo"})
        assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "geo_off"
    finally:
        _blok(root_client, "globe", True)


def test_neznakomyy_poyas_ne_zapisyvaetsya(root_client):
    """Браузер назвал пояс, которого нет в таблице: гость посчитан, но точки
    ему взять неоткуда — выдумывать не будем."""
    _doska, ssylka = _doska_so_ssylkoy(root_client, "Гео незнакомый")
    gost = TestClient(app)
    gost.get(f"/b/{ssylka['token']}")
    assert gost.post(f"/b/{ssylka['token']}/geo", params={"tz": "Mars/Olympus"}).status_code == 204

    prosmotry = root_client.get(f"{API}/shares/{ssylka['id']}/views").json()
    assert prosmotry["total"] == 1
    assert _gost(root_client.get(GLOBE).json(), "Mars/Olympus") is None


def test_geo_ne_zavodit_lishniy_prosmotr(root_client):
    """Пояс дописывается последнему просмотру: иначе один заход считался бы
    дважды и счётчик витрины врал бы там, где гео включено."""
    _doska, ssylka = _doska_so_ssylkoy(root_client, "Гео счётчик")
    gost = TestClient(app)
    gost.get(f"/b/{ssylka['token']}")
    bylo = root_client.get(f"{API}/shares/{ssylka['id']}/views").json()["total"]
    for _ in range(3):
        gost.post(f"/b/{ssylka['token']}/geo", params={"tz": "Europe/Kyiv"})
    stalo = root_client.get(f"{API}/shares/{ssylka['id']}/views").json()["total"]
    assert stalo == bylo == 1


def test_gost_bez_prosmotra_nikuda_ne_pishet(root_client):
    """Пояс без просмотра писать некуда: ручка отвечает молча и ничего не
    заводит — иначе ею можно было бы насыпать точек, не открывая витрину."""
    _doska, ssylka = _doska_so_ssylkoy(root_client, "Гео без просмотра")
    gost = TestClient(app)
    assert gost.post(f"/b/{ssylka['token']}/geo", params={"tz": "Europe/Kyiv"}).status_code == 204
    assert root_client.get(f"{API}/shares/{ssylka['id']}/views").json()["total"] == 0


def test_chuzhoy_token_ne_otkryvaet_nichego():
    gost = TestClient(app)
    assert gost.post("/b/net-takogo-tokena/geo", params={"tz": "Europe/Kyiv"}).status_code == 204
