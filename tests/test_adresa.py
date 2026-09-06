"""Подсказки адреса: молчание вместо отказа, потолок частоты, кэш, точка.

**Ни одна проверка не ходит в интернет.** Сетевой вызов подменяется, как у
звёзд GitHub: служба принимает `opener`, а ручка проверяется подменой
`urllib.request.urlopen`. Проверка, ходящая наружу, краснеет от чужого сбоя и
зеленеет от чужого кэша — то есть не проверяет ничего.

Стережём здесь то, что делает поход наружу исключением, а не дырой: выключено
по умолчанию, без сети пусто, чаще раза в секунду не спрашиваем, спрошенное
помним, и подсказка кладёт в карточку адрес вместе с точкой. Разбор —
`docs/bloki/26-adresa.md`.
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from core.services import adresa_service
from tests.conftest import API, make_manager

SUGGEST = f"{API}/clients/address/suggest"

#: Ответ Photon на «Khreshchatyk 22»: дом, город и запись без координат.
OTVET = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [30.5234, 50.4501]},
            "properties": {
                "countrycode": "UA",
                "country": "Ukraine",
                "city": "Kyiv",
                "postcode": "01001",
                "street": "Khreshchatyk",
                "housenumber": "22",
                "type": "house",
            },
        },
        {
            "geometry": {"type": "Point", "coordinates": [21.0122, 52.2297]},
            # Кода страны нет намеренно: его отдаёт не всякая установка Photon,
            # и разбор обязан достать код из названия.
            "properties": {"country": "Poland", "name": "Warszawa", "type": "city"},
        },
        {"properties": {"name": "Без координат", "country": "Ukraine"}},
    ]
}


class _Otvet:
    """Подделка ответа urllib: тот же протокол менеджера контекста."""

    def __init__(self, telo):
        self._telo = json.dumps(telo).encode("utf-8")

    def read(self, skolko=None):
        return self._telo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _opener(schyot: list, telo=OTVET):
    def otkryt(zapros, timeout=None):
        schyot.append(zapros)
        return _Otvet(telo)

    return otkryt


def _bez_seti(schyot: list):
    def otkryt(zapros, timeout=None):
        schyot.append(zapros)
        raise urllib.error.URLError("нет сети")

    return otkryt


def _nastroyka(root_client, znachenie: str) -> None:
    otvet = root_client.patch(
        f"{API}/settings", json={"values": {adresa_service.KLYUCH: znachenie}}
    )
    assert otvet.status_code == 200, otvet.text


@pytest.fixture(autouse=True)
def chistaya_pamyat():
    """Кэш и отдых — на весь процесс, а проверки идут в обоих порядках."""
    adresa_service.zabyt()
    yield
    adresa_service.zabyt()


@pytest.fixture
def vklyucheny(root_client):
    """Подсказки выключены по умолчанию — большинству проверок они нужны."""
    _nastroyka(root_client, "1")
    yield
    _nastroyka(root_client, "0")


def _klient(root_client, **polya) -> dict:
    otvet = root_client.post(f"{API}/clients", json={"name": "Адрес клиент", **polya})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


# --- выключено ----------------------------------------------------------------


def test_vyklyuchennye_podskazki_ne_hodyat_naruzhu(db):
    """Умолчание — тишина. Иначе установка отдавала бы адреса клиентов чужому
    серверу, ни у кого не спросив."""
    schyot: list = []
    otvet = adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener(schyot))

    assert otvet["enabled"] is False and otvet["items"] == []
    assert schyot == [], f"сходили наружу при выключенных подсказках: {schyot}"


def test_ruchka_pri_vyklyuchennyh_otvechaet_pustym_a_ne_otkazom(root_client):
    """Пустой список — состояние, а не беда: поле дозаполняют руками."""
    otvet = root_client.post(SUGGEST, json={"q": "Khreshchatyk 22"})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["items"] == [] and otvet.json()["enabled"] is False


def test_vyklyuchenie_ubiraet_zapomnennoe(root_client, db):
    """«Выключил, а оно всё равно лежит» — это не выключил: с тумблером уходят
    и запомненные ответы, то есть чужие адреса."""
    _nastroyka(root_client, "1")
    try:
        assert adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener([]))["items"]
        assert adresa_service._KESH
    finally:
        _nastroyka(root_client, "0")
    # Своя транзакция у сессии проверки: без этого она читала бы прежний снимок.
    db.rollback()

    assert adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener([]))["items"] == []
    assert adresa_service._KESH == {}


def test_korotkiy_zapros_naruzhu_ne_uhodit(vklyucheny, db):
    """Две буквы — это вопрос про всю планету, и чужому серверу он не нужен."""
    schyot: list = []
    assert adresa_service.podskazki(db, "ки", opener=_opener(schyot))["items"] == []
    assert schyot == []


# --- сети нет -----------------------------------------------------------------


def test_bez_seti_spisok_pust_i_nichego_ne_padaet(vklyucheny, db):
    """Отказ сети — состояние. Он не выходит наружу исключением и не мешает
    заполнить поле руками."""
    schyot: list = []
    otvet = adresa_service.podskazki(db, "Khreshchatyk 22", opener=_bez_seti(schyot))

    assert otvet["items"] == [] and otvet["enabled"] is True
    assert otvet["error"], "отказ сети не назван вовсе — разбирать будет нечего"
    assert len(schyot) == 1


def test_otdyh_posle_otkaza_dolshe_obychnoy_pauzy(vklyucheny, db):
    """После отказа молчим минуту, а не секунду: сеть пропала или нас
    притормозили — долбить дальше бессмысленно в обоих случаях."""
    schyot: list = []
    adresa_service.podskazki(db, "Khreshchatyk 22", opener=_bez_seti(schyot))

    ostalos = adresa_service._POKOY["do"] - time.monotonic()
    assert ostalos > adresa_service.PAUZA_SEKUND, (
        f"после отказа отдыхаем {ostalos:.2f} с — это обычная пауза, а не отдых"
    )
    # И следующий вопрос, даже другой, наружу уже не уходит.
    adresa_service.podskazki(db, "Warszawa", opener=_opener(schyot))
    assert len(schyot) == 1


# --- потолок частоты и кэш ----------------------------------------------------


def test_potolok_chastoty_derzhit_vtoroy_vopros(vklyucheny, db):
    """Чаще раза в секунду наружу не ходим: очередь запросов на одно слово
    чужой сервер видит обстрелом, а не работой."""
    schyot: list = []
    pervyy = adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener(schyot))
    vtoroy = adresa_service.podskazki(db, "Warszawa", opener=_opener(schyot))

    assert pervyy["items"], pervyy
    assert len(schyot) == 1, f"потолок частоты не сработал: {schyot}"
    assert vtoroy["items"] == [], "придержанный вопрос выдал чужой ответ"


def test_kesh_otvechaet_bez_pohoda_naruzhu(vklyucheny, db):
    """Тот же вопрос — тот же ответ из памяти. Кэш спрашивается ДО потолка
    частоты, иначе набор по буквам упирался бы в него на каждом слове."""
    schyot: list = []
    bylo = adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener(schyot))
    stalo = adresa_service.podskazki(db, "  KHRESHCHATYK   22 ", opener=_opener(schyot))

    assert len(schyot) == 1, f"второй раз сходили наружу: {schyot}"
    assert stalo["items"] == bylo["items"] and stalo["items"]


def test_kesh_ne_rastyot_bez_konca(vklyucheny, db):
    """Потолок памяти: строка поиска — чужой адрес, и держать их тысячами
    незачем."""
    for nomer in range(adresa_service.POTOLOK_KESHA + 20):
        adresa_service._v_kesh(f"zapros {nomer}", [])
    assert len(adresa_service._KESH) == adresa_service.POTOLOK_KESHA


# --- разбор ответа ------------------------------------------------------------


def test_razbor_otveta_photon():
    """Из ответа берём ровно то, что кладётся в карточку, и ни поля больше."""
    najdeno = adresa_service._razobrat(OTVET)

    assert len(najdeno) == 2, "запись без координат попала в подсказки"
    dom, gorod = najdeno
    assert dom["country_code"] == "UA" and dom["city"] == "Kyiv"
    assert dom["postcode"] == "01001" and dom["street"] == "Khreshchatyk 22"
    assert round(dom["lat"], 4) == 50.4501 and round(dom["lon"], 4) == 30.5234
    assert dom["label"].startswith("Khreshchatyk 22, Kyiv, 01001")

    # У города название лежит в `name`, а `city` пусто; код страны — из
    # названия, потому что `countrycode` отдаёт не всякая установка Photon.
    assert gorod["city"] == "Warszawa" and gorod["street"] == ""
    assert gorod["country_code"] == "PL"


# --- ручка и права ------------------------------------------------------------


def test_ruchka_otdaet_podskazki(root_client, vklyucheny, monkeypatch):
    """Ручка отвечает тем же, что служба, и наружу ходит ровно раз."""
    schyot: list = []
    monkeypatch.setattr(urllib.request, "urlopen", _opener(schyot))

    otvet = root_client.post(SUGGEST, json={"q": "Khreshchatyk 22"})
    assert otvet.status_code == 200, otvet.text
    dannye = otvet.json()
    assert dannye["enabled"] is True and len(dannye["items"]) == 2
    assert len(schyot) == 1
    # Наружу ушло набранное как есть, и мы назвались своим именем: безымянных
    # общие серверы режут первыми, и «нет сети» оказалось бы враньём.
    assert "q=Khreshchatyk+22" in schyot[0].full_url
    assert "OpenCRM" in (schyot[0].get_header("User-agent") or "")


def test_podskazki_zakryty_pravom_na_pravku(root_client, vklyucheny):
    """Кто карточку только смотрит, тот не заполняет адрес — и не гоняет ради
    этого чужой сервер."""
    rol = root_client.post(
        f"{API}/roles",
        json={"name": "Адрес только смотрит", "permissions": ["clients.view"]},
    )
    assert rol.status_code == 201, rol.text
    pochta = "adresa.smotritel@test.local"
    smotritel = make_manager(root_client, pochta)
    lyudi = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in lyudi if u["email"] == pochta)
    assert root_client.post(
        f"{API}/roles/assign/{user_id}", json={"role_id": rol.json()["id"]}
    ).status_code == 200

    try:
        klient = _klient(root_client, name="Адрес права")
        assert smotritel.get(f"{API}/clients/{klient['id']}").status_code == 200
        assert smotritel.post(SUGGEST, json={"q": "Khreshchatyk 22"}).status_code == 403
        zapis = smotritel.patch(
            f"{API}/clients/{klient['id']}/address",
            json={"country_code": "UA", "city": "Kyiv"},
        )
        assert zapis.status_code == 403, zapis.text
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{API}/roles/{rol.json()['id']}")


# --- выбор кладёт адрес и точку -----------------------------------------------


def test_vybor_pishet_adres_i_tochku_razom(root_client):
    """Выбрали вариант — в карточке и адрес, и точка. Точка нужна планете, и
    ставить её вторым запросом значит однажды не поставить вовсе."""
    klient = _klient(root_client, name="Адрес выбор")
    otvet = root_client.patch(
        f"{API}/clients/{klient['id']}/address",
        json={
            "country_code": "ua",
            "city": "Kyiv",
            "postcode": "01001",
            "street": "Khreshchatyk 22",
            "lat": 50.4501,
            "lon": 30.5234,
        },
    )
    assert otvet.status_code == 200, otvet.text
    stalo = otvet.json()
    assert stalo["country"] == "UA" and stalo["city"] == "Kyiv"
    assert stalo["zip_code"] == "01001" and stalo["address"] == "Khreshchatyk 22"
    assert round(stalo["lat"], 4) == 50.4501 and round(stalo["lon"], 4) == 30.5234

    # Карточка отдаёт то же самое: точка легла в базу, а не в ответ.
    snova = root_client.get(f"{API}/clients/{klient['id']}").json()
    assert round(snova["lat"], 4) == 50.4501 and snova["zip_code"] == "01001"


def test_vybor_zamenyaet_adres_tselikom(root_client):
    """Индекс от прежнего адреса — это два адреса в одной карточке, и на
    конверт уедет их смесь."""
    klient = _klient(
        root_client, name="Адрес замена", country="UA", city="Kyiv", zip_code="01001"
    )
    otvet = root_client.patch(
        f"{API}/clients/{klient['id']}/address",
        json={"country_code": "PL", "city": "Warszawa", "lat": 52.2297, "lon": 21.0122},
    )
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["zip_code"] == "", "индекс остался от прежнего адреса"
    assert otvet.json()["country"] == "PL"


def test_adres_ne_zavisit_ot_globusa(root_client):
    """Глобус выключен (умолчание), а адрес заполняется и точка пишется:
    подсказки — про карточку клиента, а не про планету."""
    assert root_client.get(f"{API}/globe").status_code == 403

    klient = _klient(root_client, name="Адрес без глобуса")
    otvet = root_client.patch(
        f"{API}/clients/{klient['id']}/address",
        json={"country_code": "UA", "city": "Kyiv", "lat": 50.45, "lon": 30.52},
    )
    assert otvet.status_code == 200, otvet.text
    assert round(otvet.json()["lat"], 2) == 50.45
    assert root_client.post(SUGGEST, json={"q": "Khreshchatyk"}).status_code == 200


# --- чужой ответ не наш договор -----------------------------------------------


@pytest.mark.parametrize(
    "telo",
    [
        [],
        "abc",
        5,
        {"features": ["x"]},
        {"features": [{"properties": [1], "geometry": {"coordinates": [1, 2]}}]},
        {"features": [{"properties": {"name": 1}, "geometry": {"coordinates": ["a", "b"]}}]},
        {"features": [{"properties": {"name": "Дом"}, "geometry": "нет"}]},
    ],
)
def test_musor_ot_chuzhogo_servera_ne_ronyaet_ruchku(root_client, vklyucheny, monkeypatch, telo):
    """Пустой список обещан в трёх местах — значит и на мусор он пустой.

    Без этого `AttributeError` уходил бы пятисоткой, а отдыха не было бы вовсе:
    следующая буква повторила бы запрос, и человек получил бы поток отказов
    вместо обещанной тишины.
    """
    monkeypatch.setattr(urllib.request, "urlopen", _opener([], telo))
    adresa_service.zabyt()

    otvet = root_client.post(SUGGEST, json={"q": "Khreshchatyk 22"})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["items"] == []


def test_potolok_variantov_derzhim_my(vklyucheny, db, monkeypatch):
    """`limit` — просьба к чужому серверу, а не обещание.

    Свой Photon или изменившийся общий вправе прислать сколько угодно; всё
    присланное уехало бы на экран и легло в ячейку памяти.
    """
    mnogo = {
        "features": [
            {
                "properties": {"name": f"Улица {i}", "housenumber": "1", "city": "Киев"},
                "geometry": {"coordinates": [30.5 + i / 1000, 50.4]},
            }
            for i in range(50)
        ]
    }
    otvet = adresa_service.podskazki(db, "Улица", opener=_opener([], mnogo))
    assert len(otvet["items"]) == adresa_service.PREDEL


def test_priderzhannyy_vopros_otlichim_ot_pustogo(vklyucheny, db):
    """«Придержали» и «не нашли» — разные ответы.

    Слей их — и человек, которому подсказка не пришла из-за потолка частоты,
    сделает вывод «такого адреса нет» и допишет адрес руками с опечаткой.
    """
    pervyy = adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener([]))
    vtoroy = adresa_service.podskazki(db, "Warszawa", opener=_opener([]))
    assert pervyy["items"] and pervyy["held"] is False
    assert vtoroy["items"] == [] and vtoroy["held"] is True


def test_vyklyuchennye_ne_hodyat_naruzhu_dazhe_s_polnoy_pamyatyu(root_client, db):
    """Выключили при непустой памяти — наружу всё равно ни одного запроса."""
    schyot: list = []
    _nastroyka(root_client, "1")
    try:
        assert adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener(schyot))["items"]
    finally:
        _nastroyka(root_client, "0")
    db.rollback()

    assert adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener(schyot))["items"] == []
    assert len(schyot) == 1, "выключенные подсказки сходили наружу"


def test_vytesnyaetsya_samoe_davnee(vklyucheny):
    """Потолок памяти выбрасывает старое, а не свежее."""
    adresa_service._v_kesh("самый старый", [{"label": "старый"}])
    for i in range(adresa_service.POTOLOK_KESHA):
        adresa_service._v_kesh(f"запрос {i}", [{"label": str(i)}])

    assert adresa_service._iz_kesha("самый старый") is None, "выбросили свежее вместо старого"
    assert adresa_service._iz_kesha(f"запрос {adresa_service.POTOLOK_KESHA - 1}") is not None


def test_svoy_server_beryotsya_iz_nastroyki(root_client, db, vklyucheny):
    """Своё зеркало Photon указывается настройкой, а не правкой строки в коде.

    Правкой нельзя: обновлятор отказывается работать с правленым рабочим
    деревом, то есть «подставьте свой адрес здесь» означало бы «откажитесь от
    обновлений» (docs/bloki/26-adresa.md §3).
    """
    schyot: list = []
    assert root_client.patch(
        f"{API}/settings", json={"values": {"address_source": "https://photon.svoy.local/api"}}
    ).status_code == 200
    db.rollback()
    try:
        adresa_service.podskazki(db, "Khreshchatyk 22", opener=_opener(schyot))
        assert schyot and schyot[0].full_url.startswith("https://photon.svoy.local/api")
    finally:
        root_client.patch(f"{API}/settings", json={"values": {"address_source": ""}})


def test_vybor_ili_ves_adres_ili_nichego(root_client):
    """Отказ на середине не оставляет половину адреса.

    Ручка обещает «адрес и точку разом»; проверяем не удачный путь, а отказ:
    негодная широта обязана откатить и запись адреса.
    """
    klient = _klient(root_client, name="Адрес откат", city="Kyiv", country="UA")
    otkaz = root_client.patch(
        f"{API}/clients/{klient['id']}/address",
        json={"country_code": "PL", "city": "Warszawa", "lat": 999, "lon": 21.0},
    )
    assert otkaz.status_code == 422, otkaz.text

    stalo = root_client.get(f"{API}/clients/{klient['id']}").json()
    assert stalo["city"] == "Kyiv", "город поменялся, а точка — нет: полправки"


def test_pravka_goroda_rukami_snimaet_prezhnyuyu_tochku(root_client):
    """Переехал город — прежняя точка врёт, и её нет.

    Оставь её, и карточка покажет «Варшава» на киевских координатах, а ссылка
    в карты уведёт не туда. Улица и дом точку не трогают: город от них не
    меняется.
    """
    klient = _klient(root_client, name="Адрес переезд")
    root_client.patch(
        f"{API}/clients/{klient['id']}/address",
        json={"country_code": "UA", "city": "Kyiv", "street": "Хрещатик 22",
              "lat": 50.4501, "lon": 30.5234},
    )
    dom = root_client.patch(f"{API}/clients/{klient['id']}", json={"address": "Хрещатик 24"})
    assert dom.status_code == 200 and dom.json()["lat"] is not None, "дом снял точку"

    pereezd = root_client.patch(f"{API}/clients/{klient['id']}", json={"city": "Warszawa"})
    assert pereezd.status_code == 200, pereezd.text
    assert pereezd.json()["lat"] is None and pereezd.json()["lon"] is None
