"""Блок «Ключи»: хранилище вторых факторов.

Проверяется то, из-за чего хранилищем перестанут пользоваться или, хуже,
перестанут ему верить:

- **секрет не уходит с сервера** нигде, кроме окна переноса, и то только root;
- **чужой ключ не виден** — ни в списке, ни по прямому адресу, ни числом в
  счётчике;
- **код настоящий** — сверен с образцами RFC 6238, а не «шесть цифр вернулись»;
- **закрытая категория не открывается списком доступа**;
- **показ пишется в журнал**, а пересборка кода раз в тридцать секунд — нет.

Разбор — `docs/bloki/27-klyuchi.md`.
"""

import base64
import itertools

import pytest

from core.security import totp
from core.services import klyuchi_service
from tests.conftest import API, make_manager

KLYUCHI = f"{API}/keys"

#: Имя должности уникально (отказ `role_name_taken`), а должность,
#: отданная человеку, может не удалиться в уборке. Счётчик на весь прогон.
_SCHYOT = itertools.count(1)

#: Ключ из образцов RFC 6238: «12345678901234567890» в base32.
OBRAZTSOVYY = base64.b32encode(b"12345678901234567890").decode()
STROKA = f"otpauth://totp/GitHub:denis@studio.site?secret={OBRAZTSOVYY}&issuer=GitHub"


@pytest.fixture(autouse=True)
def blok_vklyuchen(root_client):
    """Блок выключен по умолчанию, и включённым его оставлять нельзя.

    Фикстура, забывшая выключить блок, роняет соседние наборы в обратном
    порядке ворот — на этом проект уже обжигался (`test_otchyot_prodazh.py`).
    """
    root_client.post(f"{API}/modules/keys", json={"enabled": True})
    yield
    root_client.post(f"{API}/modules/keys", json={"enabled": False})


def _nomer(root_client, pochta: str) -> int:
    lyudi = root_client.get(f"{API}/staff").json()["items"]
    return next(u["id"] for u in lyudi if u["email"] == pochta)


@pytest.fixture
def sotrudnik(root_client):
    """Менеджер с правом входить в раздел — и только с ним.

    Право на раздел здесь ничего не открывает: ЧТО именно видно, решает список
    доступа у каждого ключа. Ровно это и проверяется ниже.
    """
    roli = []

    def sdelat(pochta: str, prava=("keys.view",)):
        rol = root_client.post(
            f"{API}/roles", json={"name": f"Ключи-{next(_SCHYOT)}", "permissions": list(prava)}
        )
        assert rol.status_code == 201, rol.text
        roli.append(rol.json()["id"])
        klient = make_manager(root_client, pochta)
        nomer = _nomer(root_client, pochta)
        naznachenie = root_client.post(
            f"{API}/roles/assign/{nomer}", json={"role_id": rol.json()["id"]}
        )
        assert naznachenie.status_code == 200, naznachenie.text
        return klient, nomer

    yield sdelat
    for nomer_roli in roli:
        root_client.delete(f"{API}/roles/{nomer_roli}")


def zavesti(client, **pravki) -> dict:
    telo = {"secret": STROKA, "title": "GitHub — организация"}
    telo.update(pravki)
    otvet = client.post(KLYUCHI, json=telo)
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


# --- сам код -----------------------------------------------------------------


def test_kod_schitaetsya_po_obraztsam_rfc():
    """Не «вернулось шесть цифр», а те самые цифры.

    Свой TOTP без сверки с образцами — способ узнать о расхождении от
    сотрудника, которого сервис не пустил.
    """
    for kogda, ozhidaem in (
        (59, "287082"),
        (1111111109, "081804"),
        (1234567890, "005924"),
        (20000000000, "353130"),
    ):
        assert totp.kod(OBRAZTSOVYY, seychas=kogda, cifr=6, shag=30) == ozhidaem


def test_ostalos_nikogda_ne_nol():
    """Ноль секунд означал бы код, который уже не годится, но ещё показан."""
    assert totp.ostalos(0, 30) == 30
    assert totp.ostalos(29, 30) == 1


def test_stroka_po_schyotchiku_ne_prinimaetsya():
    """`otpauth://hotp/` — счётчик, а не время: верного кода он не даст никогда."""
    with pytest.raises(totp.NeTaStroka):
        totp.razobrat("otpauth://hotp/Servis:kto?secret=" + OBRAZTSOVYY)


def test_goliy_klyuch_prinimaetsya_probelami_i_v_nizhnem():
    """Половина сервисов показывает под QR-кодом голый ключ группами по четыре."""
    razbor = totp.razobrat("jbsw y3dp ehpk 3pxp")
    assert razbor["sekret"] == "JBSWY3DPEHPK3PXP"


# --- заведение ---------------------------------------------------------------


def test_razbor_pokazyvaet_chto_ponyato_do_sohraneniya(root_client):
    """Вставил не то — видно сразу, а не через тридцать секунд по коду."""
    otvet = root_client.post(f"{KLYUCHI}/parse", json={"secret": STROKA})
    assert otvet.status_code == 200, otvet.text
    razbor = otvet.json()
    assert razbor["issuer"] == "GitHub"
    assert razbor["account"] == "denis@studio.site"
    assert razbor["digits"] == 6 and razbor["period"] == 30
    assert razbor["znak"]["slug"] == "github", "фирменный знак сервиса не нашёлся"
    assert len(razbor["proverka"]) == 6, "проверочный код не посчитан"


def test_musor_vmesto_klyucha_otvergaetsya(root_client):
    otkaz = root_client.post(KLYUCHI, json={"secret": "не ключ вовсе", "title": "Мусор"})
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_bad_secret"


def test_klyuch_zavoditsya_i_daet_kod(root_client):
    klyuch = zavesti(root_client)
    assert klyuch["issuer"] == "GitHub"
    assert klyuch["account"] == "denis@studio.site"
    assert klyuch["znak"]["slug"] == "github"

    kod = root_client.post(f"{KLYUCHI}/{klyuch['id']}/code")
    assert kod.status_code == 200, kod.text
    assert len(kod.json()["code"]) == 6
    assert 1 <= kod.json()["ostalos"] <= 30, "остаток секунд обязан приходить с сервера"


def test_sekreta_net_ni_v_spiske_ni_v_kartochke(root_client):
    """Уйди секрет вместе с карточкой — он осел бы в кэше вкладки и в снимке
    экрана у всех, кто её открывал."""
    zavesti(root_client, title="Секрет не уезжает")
    spisok = root_client.get(KLYUCHI)
    assert spisok.status_code == 200, spisok.text
    assert OBRAZTSOVYY not in spisok.text, "секрет уехал в список"
    assert "secret" not in spisok.json()["items"][0], "в карточке появилось поле секрета"


# --- перенос на телефон ------------------------------------------------------


def test_sekret_otdayotsya_tolko_rootu_i_pishetsya_v_zhurnal(root_client):
    """Снятый код живёт своей жизнью и после закрытия доступа в CRM."""
    klyuch = zavesti(root_client, title="Ключ для переноса")
    otvet = root_client.post(f"{KLYUCHI}/{klyuch['id']}/secret")
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["secret"] == OBRAZTSOVYY
    assert otvet.json()["otpauth"].startswith("otpauth://totp/")

    zapisi = root_client.get(f"{API}/audit", params={"action": "key.secret_shown"}).json()["items"]
    assert any(z["action"] == "key.secret_shown" for z in zapisi), "снятие секрета не в журнале"


def test_ne_root_sekreta_ne_poluchaet(root_client, sotrudnik):
    klyuch = zavesti(root_client, title="Секрет мимо менеджера")
    chuzhoy, nomer = sotrudnik("klyuchi.ne.root@test.local")
    root_client.post(f"{KLYUCHI}/{klyuch['id']}/access", json={"user_id": nomer, "otkryt": True})
    otkaz = chuzhoy.post(f"{KLYUCHI}/{klyuch['id']}/secret")
    assert otkaz.status_code == 403, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_secret_root_only"


# --- кто что видит -----------------------------------------------------------


def test_chuzhoy_klyuch_ne_viden_ni_spiskom_ni_po_adresu(root_client, sotrudnik):
    """Отказ по правам сам по себе рассказал бы, что такой ключ есть."""
    klyuch = zavesti(root_client, title="Чужой ключ")
    chuzhoy, _ = sotrudnik("klyuchi.chuzhoy@test.local")

    spisok = chuzhoy.get(KLYUCHI)
    assert spisok.status_code == 200, spisok.text
    assert all(k["id"] != klyuch["id"] for k in spisok.json()["items"])
    assert chuzhoy.post(f"{KLYUCHI}/{klyuch['id']}/code").status_code == 404


def test_tochechnyy_dostup_otkryvaet_odin_klyuch(root_client, sotrudnik):
    klyuch = zavesti(root_client, title="Открываем точечно")
    smotritel, nomer = sotrudnik("klyuchi.tochechno@test.local")

    otvet = root_client.post(
        f"{KLYUCHI}/{klyuch['id']}/access", json={"user_id": nomer, "otkryt": True}
    )
    assert otvet.status_code == 200, otvet.text
    assert any(c["id"] == nomer and c["otkryt"] for c in otvet.json()["people"])

    vidno = smotritel.get(KLYUCHI).json()["items"]
    assert any(k["id"] == klyuch["id"] for k in vidno), "точечный доступ не открыл ключ"
    assert smotritel.post(f"{KLYUCHI}/{klyuch['id']}/code").status_code == 200

    root_client.post(f"{KLYUCHI}/{klyuch['id']}/access", json={"user_id": nomer, "otkryt": False})
    assert smotritel.post(f"{KLYUCHI}/{klyuch['id']}/code").status_code == 404


def test_sozdatel_ne_snimaetsya_so_svoego_klyucha(root_client):
    """Иначе человек закрыл бы себе доступ к тому, что сам и завёл."""
    klyuch = zavesti(root_client, title="Себя не снять")
    ya = _nomer(root_client, "root@test.local")
    otkaz = root_client.post(
        f"{KLYUCHI}/{klyuch['id']}/access", json={"user_id": ya, "otkryt": False}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_access_always"


def test_zakrytaya_kategoriya_spiskov_dostupa_ne_slushaet(root_client, sotrudnik):
    """«Личные Дениса» — категория, в которую нельзя пустить: в этом её смысл."""
    kat = root_client.post(
        f"{KLYUCHI}/categories", json={"name": "Личные тестовые", "zakrytaya": True}
    )
    assert kat.status_code == 201, kat.text
    klyuch = zavesti(root_client, title="В закрытой", category="Личные тестовые")

    _, nomer = sotrudnik("klyuchi.zakrytaya@test.local")
    otkaz = root_client.post(
        f"{KLYUCHI}/{klyuch['id']}/access", json={"user_id": nomer, "otkryt": True}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_category_closed"


# --- запасные коды -----------------------------------------------------------


def test_potrachennyy_zapasnoy_vycherkivaetsya_a_ne_udalyaetsya(root_client):
    """Дырка вместо зачёркнутой строки сдвинула бы остальные — человек прочёл
    бы не тот код."""
    klyuch = zavesti(
        root_client, title="С запасными", backup_codes=["4f9c-20a1", "8e13-77bd", "0a55-c194"]
    )
    assert klyuch["backup_total"] == 3 and klyuch["backup_left"] == 3

    potracheno = root_client.post(f"{KLYUCHI}/{klyuch['id']}/backup-codes/0/spend")
    assert potracheno.status_code == 200, potracheno.text
    itog = potracheno.json()
    assert itog["total"] == 3 and itog["left"] == 2
    assert itog["items"][0]["potrachen"] is True
    assert itog["items"][0]["kod"] == "4f9c-20a1", "код исчез вместо того, чтобы вычеркнуться"


# --- корзина -----------------------------------------------------------------


def test_klyuch_uhodit_v_korzinu_i_vozvrashchaetsya(root_client):
    klyuch = zavesti(root_client, title="Через корзину")
    assert root_client.delete(f"{KLYUCHI}/{klyuch['id']}").status_code == 200

    assert all(k["id"] != klyuch["id"] for k in root_client.get(KLYUCHI).json()["items"])
    v_korzine = root_client.get(KLYUCHI, params={"trash": True}).json()["items"]
    assert any(k["id"] == klyuch["id"] for k in v_korzine)
    # Из корзины код не показывают: ключ считается снятым с работы.
    assert root_client.post(f"{KLYUCHI}/{klyuch['id']}/code").status_code == 422

    assert root_client.post(f"{KLYUCHI}/{klyuch['id']}/restore").status_code == 200
    assert any(k["id"] == klyuch["id"] for k in root_client.get(KLYUCHI).json()["items"])


def test_nasovsem_tolko_iz_korziny(root_client):
    """Мимо корзины стирать нельзя: восстановить ключ нечем."""
    klyuch = zavesti(root_client, title="Мимо корзины не стереть")
    otkaz = root_client.delete(f"{KLYUCHI}/{klyuch['id']}/forever")
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_not_in_trash"

    root_client.delete(f"{KLYUCHI}/{klyuch['id']}")
    assert root_client.delete(f"{KLYUCHI}/{klyuch['id']}/forever").status_code == 200
    assert root_client.post(f"{KLYUCHI}/{klyuch['id']}/code").status_code == 404


# --- журнал ------------------------------------------------------------------


def test_pokaz_pishetsya_a_peresborka_net(root_client):
    """Открытый экран пересобирает код каждые тридцать секунд.

    Пиши мы каждую пересборку — один экран за час дал бы сто двадцать записей,
    и журнал, ради которого всё затевалось, стало бы невозможно читать.
    """
    klyuch = zavesti(root_client, title="Журнал показов")

    def skolko() -> int:
        zapisi = root_client.get(f"{API}/audit", params={"action": "key.shown"}).json()["items"]
        return sum(1 for z in zapisi if z["entity_id"] == klyuch["id"])

    bylo = skolko()
    root_client.post(f"{KLYUCHI}/{klyuch['id']}/code")
    assert skolko() == bylo + 1, "нажатие «показать» не попало в журнал"
    root_client.post(f"{KLYUCHI}/{klyuch['id']}/code", params={"silent": True})
    root_client.post(f"{KLYUCHI}/{klyuch['id']}/code", params={"silent": True})
    assert skolko() == bylo + 1, "пересборка кода засоряет журнал"


def test_sam_klyuch_v_zhurnal_ne_popadaet(root_client):
    """В журнал идут имя, время и что открывали — и ничего больше."""
    klyuch = zavesti(root_client, title="Журнал без секрета")
    root_client.post(f"{KLYUCHI}/{klyuch['id']}/code")
    root_client.post(f"{KLYUCHI}/{klyuch['id']}/secret")
    zhurnal = root_client.get(f"{API}/audit", params={"entity_type": "twofactor"})
    assert OBRAZTSOVYY not in zhurnal.text, "секрет уехал в журнал"


# --- блок выключается --------------------------------------------------------


def test_vyklyuchennyy_blok_ischezaet_tselikom(root_client):
    zavesti(root_client, title="Блок выключат")
    root_client.post(f"{API}/modules/keys", json={"enabled": False})
    try:
        otkaz = root_client.get(KLYUCHI)
        assert otkaz.status_code in (403, 404), otkaz.text
    finally:
        root_client.post(f"{API}/modules/keys", json={"enabled": True})


# --- знаки сервисов ----------------------------------------------------------


def test_nabor_znakov_doehal_i_ne_hodit_v_set(root_client):
    """Набор, не доехавший в образ, молча превратил бы все знаки в буквенные."""
    from core.services import znaki_service

    assert znaki_service.skolko() > 3000, "набор значков не доехал"
    assert znaki_service.nayti("Telegram")["slug"] == "telegram"
    # Чего в свободных наборах нет и не появится — буквенная плашка, не отказ.
    assert znaki_service.nayti("Amazon Web Services") is None
    assert znaki_service.nayti("") is None


def test_znak_otdayotsya_kartinkoy(root_client):
    otvet = root_client.get(f"{KLYUCHI}/znak/github.svg")
    assert otvet.status_code == 200, otvet.text
    assert otvet.headers["content-type"].startswith("image/svg+xml")
    assert otvet.text.startswith("<svg")
    assert root_client.get(f"{KLYUCHI}/znak/takogo-net.svg").status_code == 404


def test_znak_ne_hranitsya_v_baze(root_client):
    """Производное не хранят: сменится набор — знак обязан смениться сам."""
    klyuch = zavesti(root_client, title="Знак не хранится")
    assert klyuch["znak"]["slug"] == "github"
    root_client.patch(f"{KLYUCHI}/{klyuch['id']}", json={"issuer": "Telegram"})
    stalo = root_client.get(KLYUCHI).json()["items"]
    nash = next(k for k in stalo if k["id"] == klyuch["id"])
    assert nash["znak"]["slug"] == "telegram", "знак остался от прежнего сервиса"


# --- шифрование --------------------------------------------------------------


def test_sekret_lezhit_v_baze_zashifrovannym(root_client, db):
    """В дампе базы секрет обязан быть нечитаемым: копия уезжает наружу."""
    klyuch = zavesti(root_client, title="Шифрование в базе")
    from database.repositories import klyuchi as klyuchi_repo

    v_baze = klyuchi_repo.get(db, klyuch["id"])
    assert v_baze is not None
    assert OBRAZTSOVYY not in (v_baze.secret_encrypted or ""), "секрет лежит открытым"
    from core.security import secretbox

    assert (
        secretbox.decrypt(v_baze.secret_encrypted, klyuchi_service.SECRET_PURPOSE) == OBRAZTSOVYY
    )
