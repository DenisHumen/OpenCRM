"""Блок «Глобус»: где встаёт точка, откуда берутся связи, что гасит блок.

Проверяется не картинка, а её основания: порядок точности места, постоянство
разброса, живость бумаг за линией и исчезновение блока целиком. Разбор —
`docs/bloki/25-globus.md`.
"""
import pytest

from core.geo.dannye import CENTRY_STRAN, POYASA
from core.services import globus_service
from tests.conftest import API

GLOBE = f"{API}/globe"


def _blok(client, key: str, enabled: bool) -> None:
    assert client.post(f"{API}/modules/{key}", json={"enabled": enabled}).status_code == 200


@pytest.fixture(autouse=True)
def blok_vklyuchen(root_client):
    """Глобус выключен по умолчанию — тестам он нужен включённым."""
    _blok(root_client, "globe", True)
    yield
    _blok(root_client, "globe", False)


def _klient(client, **polya) -> dict:
    otvet = client.post(f"{API}/clients", json={"name": "Глобус клиент", **polya})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def _tochka(kartina: dict, client_id: int) -> dict | None:
    for tochka in kartina["points"]:
        if tochka["vid"] == "client" and tochka["id"] == client_id:
            return tochka
    return None


def test_mesto_klienta_po_strane_i_po_gorodu(root_client):
    """Порядок точности: город узнаём по совпадению с городом пояса, иначе
    показываем центр страны с разбросом."""
    po_strane = _klient(root_client, name="Глобус страна", country="UA")
    po_gorodu = _klient(root_client, name="Глобус город", country="UA", city="Киев")

    kartina = root_client.get(GLOBE).json()
    strana = _tochka(kartina, po_strane["id"])
    gorod = _tochka(kartina, po_gorodu["id"])
    assert strana and strana["tochnost"] == "strana"
    assert gorod and gorod["tochnost"] == "gorod"

    # Киев из таблицы поясов, а не выдумка.
    _, shirota, dolgota = POYASA["Europe/Kyiv"]
    assert round(gorod["lat"], 4) == round(shirota / 1e7, 4)
    assert round(gorod["lon"], 4) == round(dolgota / 1e7, 4)

    # Центр страны — тот же, что в справочнике, плюс разброс не дальше 2°.
    centr_lat, centr_lon, _ = CENTRY_STRAN["UA"]
    assert abs(strana["lat"] - centr_lat / 1e7) <= 2.001
    assert abs(strana["lon"] - centr_lon / 1e7) <= 2.001


def test_razbros_postoyanen_i_ne_slepliaet_tochki(root_client):
    """Разброс считается от номера записи: точка не прыгает между запросами и
    не ложится на соседнюю."""
    pervyy = _klient(root_client, name="Глобус разброс 1", country="PL")
    vtoroy = _klient(root_client, name="Глобус разброс 2", country="PL")

    bylo = root_client.get(GLOBE).json()
    stalo = root_client.get(GLOBE).json()
    a1, a2 = _tochka(bylo, pervyy["id"]), _tochka(stalo, pervyy["id"])
    assert (a1["lat"], a1["lon"]) == (a2["lat"], a2["lon"]), "точка прыгает между запросами"

    b = _tochka(bylo, vtoroy["id"])
    assert (a1["lat"], a1["lon"]) != (b["lat"], b["lon"]), "два клиента одной страны слиплись"


def test_tochka_rukoy_perebivaet_stranu_i_snimaetsya(root_client):
    """Поставленная рукой точка — старшая, и её можно снять обратно."""
    klient = _klient(root_client, name="Глобус рука", country="UA", city="Киев")
    otvet = root_client.patch(f"{API}/clients/{klient['id']}/geo", json={"lat": 49.8397, "lon": 24.0297})
    assert otvet.status_code == 200, otvet.text
    assert round(otvet.json()["lat"], 4) == 49.8397

    tochka = _tochka(root_client.get(GLOBE).json(), klient["id"])
    assert tochka["tochnost"] == "tochka"
    assert round(tochka["lat"], 4) == 49.8397 and round(tochka["lon"], 4) == 24.0297

    snyato = root_client.patch(f"{API}/clients/{klient['id']}/geo", json={"lat": None, "lon": None})
    assert snyato.status_code == 200 and snyato.json()["lat"] is None
    assert _tochka(root_client.get(GLOBE).json(), klient["id"])["tochnost"] == "gorod"


def test_koordinaty_vne_diapazona_otvergayutsya(root_client):
    klient = _klient(root_client, name="Глобус мимо", country="UA")
    otvet = root_client.patch(f"{API}/clients/{klient['id']}/geo", json={"lat": 100, "lon": 0})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "bad_coordinates"


def test_klient_bez_adresa_ne_vydumyvaetsya(root_client):
    """Клиента без страны на планете нет, но он посчитан: «нет места» —
    ответ, а «поставили наугад» — враньё."""
    klient = _klient(root_client, name="Глобус без адреса")
    kartina = root_client.get(GLOBE).json()
    assert _tochka(kartina, klient["id"]) is None
    assert kartina["totals"]["no_place"] >= 1


def test_svyaz_ot_bazy_tolko_po_zhivoy_rabote(root_client):
    """Линия поставки рисуется по открытой заявке и пропадает с её закрытием."""
    root_client.patch(f"{API}/settings", json={"values": {"default_country_code": "380"}})
    klient = _klient(root_client, name="Глобус связь", country="PL")
    zayavka = root_client.post(
        f"{API}/deals", json={"title": "Глобус связь заявка", "client_id": klient["id"]}
    ).json()

    kartina = root_client.get(GLOBE).json()
    assert kartina["base"] is not None, "база берётся из кода страны для номеров"
    svoi = [s for s in kartina["links"] if s["k"] == f"client:{klient['id']}"]
    assert svoi and svoi[0]["ot"] == "base" and svoi[0]["vid"] == "postavka"

    stages = {s["kind"]: s["key"] for s in root_client.get(f"{API}/pipeline/stages").json()["items"]}
    assert root_client.post(f"{API}/deals/{zayavka['id']}/move", json={"stage": stages["won"]}).status_code == 200
    posle = root_client.get(GLOBE).json()
    assert [s for s in posle["links"] if s["k"] == f"client:{klient['id']}"] == []


def test_sloi_idut_za_blokami(root_client):
    """Выключенный блок не оставляет ни слоя, ни точек его цвета."""
    _blok(root_client, "orders", True)
    try:
        assert "orders" in root_client.get(GLOBE).json()["layers"]
    finally:
        _blok(root_client, "orders", False)
    sloi = root_client.get(GLOBE).json()["layers"]
    assert "orders" not in sloi and "overdue" not in sloi
    assert "clients" in sloi and "links" in sloi


def test_vyklyuchennyy_blok_ubiraet_ruchku(root_client):
    _blok(root_client, "globe", False)
    try:
        otvet = root_client.get(GLOBE)
        assert otvet.status_code == 403 and otvet.json()["error"]["code"] == "module_disabled"
    finally:
        _blok(root_client, "globe", True)


def test_summy_tolko_s_pravom(root_client):
    """Сумма в работе — под правом на суммы, как везде на экранах.

    Должность заводится своя: у готового «менеджера» право на суммы есть, и на
    нём этого не проверить.
    """
    from tests.conftest import make_manager

    klient = _klient(root_client, name="Глобус деньги", country="UA")
    root_client.post(
        f"{API}/deals", json={"title": "Глобус деньги заявка", "client_id": klient["id"], "amount": 5000}
    )
    svoya = _tochka(root_client.get(GLOBE).json(), klient["id"])
    assert svoya["amount"] == 5000

    bez_summ = root_client.post(
        f"{API}/roles", json={"name": "Глобус без сумм", "permissions": ["globe.view", "clients.view"]}
    )
    assert bez_summ.status_code == 201, bez_summ.text
    smotritel = make_manager(root_client, "globus.bez.summ@test.local")
    kto = smotritel.get(f"{API}/auth/me").json()
    assert root_client.post(
        f"{API}/roles/assign/{kto['id']}", json={"role_id": bez_summ.json()["id"]}
    ).status_code == 200

    chuzhaya = _tochka(smotritel.get(GLOBE).json(), klient["id"])
    assert chuzhaya is not None and chuzhaya["amount"] is None


def test_neznakomaya_strana_ne_daet_tochki():
    """Страны, которой нет в справочнике центров, на планете нет."""
    assert globus_service.mesto_strany("ZZ") is None
    assert globus_service.mesto_poyasa("Nigde/Nikogda") is None


def test_gorod_uznayotsya_po_raznym_napisaniyam():
    """«Киев», «Kyiv» и «Kiev» — один город: адреса пишут как придётся."""
    klyuchi = {globus_service._klyuch_goroda(x) for x in ("Киев", "Kyiv", "Kiev", " kyiv ")}
    assert klyuchi == {"kyiv"}
    assert globus_service._klyuch_goroda("Warszawa") == "warsaw"


# --- улицы и дома -------------------------------------------------------------


def test_plitka_i_ee_granicy_shodyatsya():
    """Номер плитки и её прямоугольник — обратные действия.

    Разойдись они — запрос уходил бы за один квартал, а рисовалось бы в
    другом, и это не отказ, а тихо неверная карта.
    """
    from core.services import globus_ulitsy_service as ulicy

    for lon, lat in ((30.5234, 50.4501), (-74.006, 40.7128), (151.2093, -33.8688), (0.0, 0.0)):
        z, x, y = ulicy.nomer(lon, lat)
        yug, zapad, sever, vostok = ulicy.granicy(z, x, y)
        assert zapad <= lon <= vostok, (lon, zapad, vostok)
        assert yug <= lat <= sever, (lat, yug, sever)


def test_ekran_i_server_schitayut_plitki_odinakovo():
    """Арифметика плиток повторена на двух языках — числа обязаны совпадать.

    Держать её в одном месте нельзя: номер нужен и серверу (чтобы спросить
    Overpass), и экрану (чтобы знать, что просить). Значит остаётся сверять.
    """
    import re
    from pathlib import Path

    from core.services import globus_ulitsy_service as ulicy

    tekst = Path("web/frontend/crm/src/lib/globus/ulitsy.ts").read_text(encoding="utf-8")

    def chislo(imya: str) -> int:
        najdeno = re.search(rf"export const {imya} = ([0-9_]+)", tekst)
        assert najdeno, f"в ulitsy.ts не найдено {imya}"
        return int(najdeno.group(1).replace("_", ""))

    assert chislo("PLITKA_Z") == ulicy.PLITKA_Z, "уровень плитки разошёлся"
    assert chislo("TOCHNOST") == ulicy.TOCHNOST, "точность упаковки разошлась"


def _plitka_mvt(figury) -> bytes:
    """Собрать векторную плитку из фигур — чтобы читалку было на чём проверить.

    Пишем формат руками: своя запись рядом со своим чтением — единственный
    способ убедиться, что читалка разбирает именно то, что описано в
    спецификации, а не то, что случайно совпало на одной живой плитке.
    """

    def varint(n: int) -> bytes:
        itog = bytearray()
        while True:
            bayt = n & 0x7F
            n >>= 7
            itog.append(bayt | (0x80 if n else 0))
            if not n:
                return bytes(itog)

    def pole(nomer: int, telo: bytes) -> bytes:
        return varint((nomer << 3) | 2) + varint(len(telo)) + telo

    def chislo(nomer: int, znachenie: int) -> bytes:
        return varint(nomer << 3) + varint(znachenie)

    def zigzag(n: int) -> int:
        return (n << 1) ^ (n >> 31)

    sloi = bytearray()
    for imya, klyuchi, znacheniya, fichi in figury:
        telo = bytearray(pole(1, imya.encode()) + chislo(15, 2) + chislo(5, 4096))
        for k in klyuchi:
            telo += pole(3, k.encode())
        for v in znacheniya:
            telo += pole(4, pole(1, v.encode()))
        for vid, metki, tochki in fichi:
            geom = bytearray()
            geom += varint((1 << 3) | 1)  # MoveTo, одна точка
            x0, y0 = tochki[0]
            geom += varint(zigzag(x0)) + varint(zigzag(y0))
            if len(tochki) > 1:
                geom += varint(((len(tochki) - 1) << 3) | 2)  # LineTo
                px, py = x0, y0
                for x, y in tochki[1:]:
                    geom += varint(zigzag(x - px)) + varint(zigzag(y - py))
                    px, py = x, y
            pary = bytearray()
            for k, v in metki:
                pary += varint(k) + varint(v)
            fich = bytearray(chislo(3, vid))
            if pary:
                fich += pole(2, bytes(pary))
            fich += pole(4, bytes(geom))
            telo += pole(2, bytes(fich))
        sloi += pole(3, bytes(telo))
    return bytes(sloi)


def test_chitalka_plitki_beryot_liniyu_i_koltso():
    """Читалка векторной плитки разбирает то, что описано в спецификации."""
    from core.geo import mvt

    syroe = _plitka_mvt([
        ("streets", ["kind"], ["primary", "rail"],
         [(2, [(0, 0)], [(10, 20), (30, 40)]), (2, [(0, 1)], [(0, 0), (5, 5)])]),
        ("buildings", [], [], [(3, [], [(1, 1), (9, 1), (9, 9)])]),
        # Чужой слой не должен даже разбираться.
        ("water_polygons", [], [], [(3, [], [(0, 0), (4096, 4096)])]),
    ])
    razobrano = mvt.sloi(syroe, {"streets", "buildings"}, {"kind"})

    assert "water_polygons" not in razobrano
    ulicy = razobrano["streets"]
    assert [f.metki.get("kind") for f in ulicy] == ["primary", "rail"]
    assert ulicy[0].kolca == [[(10, 20), (30, 40)]], "разности точек прочитаны неверно"
    assert razobrano["buildings"][0].vid == mvt.MNOGOUGOLNIK
    assert razobrano["_ekstent"] == 4096


def test_razbor_plitki_delit_dorogi_i_doma():
    """Из плитки берём только дороги нужных видов и кольца домов."""
    from core.services import globus_ulitsy_service as ulicy

    syroe = _plitka_mvt([
        ("streets", ["kind"], ["primary", "service", "rail"],
         [(2, [(0, 0)], [(0, 0), (100, 100)]),
          (2, [(0, 1)], [(0, 0), (10, 10)]),
          # Рельсы дорогой не являются.
          (2, [(0, 2)], [(0, 0), (500, 500)])]),
        ("buildings", [], [], [(3, [], [(1, 1), (50, 1), (50, 50)])]),
    ])
    z, x, y = ulicy.nomer(30.5234, 50.4501)
    razobrano = ulicy._razobrat(syroe, z, x, y)

    assert [d[0] for d in razobrano["dorogi"]] == [3, 0], "рельсы попали в дороги"
    assert len(razobrano["doma"]) == 1

    # Точка плитки переведена в градусы и лежит внутри её же прямоугольника.
    yug, zapad, sever, vostok = ulicy.granicy(z, x, y)
    lon = razobrano["dorogi"][0][1] / ulicy.TOCHNOST
    lat = razobrano["dorogi"][0][2] / ulicy.TOCHNOST
    assert zapad <= lon <= vostok and yug <= lat <= sever, (lon, lat)


def test_bez_zhelaniya_nikuda_ne_hodim(tmp_path, monkeypatch):
    """Пока докачка выключена, наружу не уходит ни одного запроса.

    Это не про экономию: система без интернета обязана работать молча, и
    «выключил докачку, а он всё равно ходит» — это не выключил.
    """
    from core.services import globus_ulitsy_service as ulicy

    monkeypatch.setattr(ulicy, "_katalog", lambda: tmp_path)

    def ne_hodim(*args, **kwargs):
        raise AssertionError("ушёл запрос наружу при выключенной докачке")

    monkeypatch.setattr(ulicy, "_vzyat", ne_hodim)
    otvet = ulicy.plitka(ulicy.PLITKA_Z, 38324, 22098, hotim=False)
    assert otvet["gotovo"] is False and otvet["idet"] is False


def test_chuzhoy_uroven_plitki_ne_prinimaetsya():
    """Уровень задан сервером: чужой номер — это чужая арифметика."""
    from core.services import globus_ulitsy_service as ulicy

    otvet = ulicy.plitka(12, 1, 1, hotim=True)
    assert otvet["gotovo"] is False and otvet["oshibka"] == "tile_out_of_range"
