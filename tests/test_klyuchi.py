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


def _pokazov(root_client, key_id: int) -> int:
    zapisi = root_client.get(f"{API}/audit", params={"action": "key.shown"}).json()["items"]
    return sum(1 for z in zapisi if z["entity_id"] == key_id)


def test_povtornye_pokazy_shodyatsya_v_odnu_zapis(root_client):
    """Открытый экран пересобирает код каждые тридцать секунд.

    Пиши мы каждую пересборку — один экран за час дал бы сто двадцать записей,
    и журнал, ради которого всё затевалось, стало бы невозможно читать.
    """
    klyuch = zavesti(root_client, title="Журнал показов")
    bylo = _pokazov(root_client, klyuch["id"])
    for _ in range(3):
        assert root_client.post(f"{KLYUCHI}/{klyuch['id']}/code").status_code == 200
    assert _pokazov(root_client, klyuch["id"]) == bylo + 1, "пересборка кода засоряет журнал"


def test_pokaz_v_zhurnal_snaruzhi_ne_vyklyuchaetsya(root_client, monkeypatch):
    """**Выключателя записи снаружи нет, и это главное свойство журнала.**

    Прежде решал параметр запроса `silent`, то есть сам вызывающий: строка
    `?silent=1` из консоли браузера снимала чужой код месяцами, не оставив ни
    строки. Схлопывание повторов решает сервер окном — здесь оно сведено к нулю,
    и тогда каждый показ обязан быть записан.
    """
    klyuch = zavesti(root_client, title="Журнал без выключателя")
    monkeypatch.setattr(klyuchi_service, "POKAZ_OKNO_SEKUND", 0)
    bylo = _pokazov(root_client, klyuch["id"])
    for parametry in ({}, {"silent": True}, {"silent": 1}):
        assert (
            root_client.post(f"{KLYUCHI}/{klyuch['id']}/code", params=parametry).status_code == 200
        )
    assert _pokazov(root_client, klyuch["id"]) == bylo + 3, (
        "показ кода удалось снять мимо журнала"
    )


def test_vycherknutyy_zapasnoy_pishetsya_v_zhurnal(root_client):
    """«Кто потратил четыре кода из восьми» спрашивают, когда войти уже нечем."""
    klyuch = zavesti(root_client, title="Запасные в журнале", backup_codes=["aaa-111", "bbb-222"])
    assert root_client.post(f"{KLYUCHI}/{klyuch['id']}/backup-codes/0/spend").status_code == 200
    zapisi = root_client.get(
        f"{API}/audit", params={"action": "key.backup_spent"}
    ).json()["items"]
    nashi = [z for z in zapisi if z["entity_id"] == klyuch["id"]]
    assert nashi, "вычеркнутый запасной код не оставил следа в журнале"
    assert "aaa-111" not in root_client.get(
        f"{API}/audit", params={"entity_type": "twofactor"}
    ).text, "сам запасной код уехал в журнал"


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


# --- тревога и категории -----------------------------------------------------


def test_trevoga_schitaet_po_vsem_klyucham_a_ne_po_polke(root_client):
    """Число в шапке — одно на весь раздел.

    Считай его по показанной полке — оно менялось бы от выбранной категории и
    читалось бы как «здесь просрочено столько», а тревога обязана быть общей.
    """
    klyuch = zavesti(root_client, title="Просит обновления", category="Полка тревоги")
    zadacha = root_client.post(
        f"{API}/tasks",
        json={"title": "Сменить ключ: проба", "due_at": "2020-01-01T10:00:00Z"},
    )
    assert zadacha.status_code == 201, zadacha.text
    assert root_client.patch(
        f"{KLYUCHI}/{klyuch['id']}", json={"task_id": zadacha.json()["id"]}
    ).status_code == 200

    vsyo = root_client.get(KLYUCHI).json()
    assert vsyo["prosyat"] >= 1, "просроченное напоминание не подняло тревогу"

    # Другая полка — то же число: оно про раздел, а не про полку.
    drugaya = root_client.get(KLYUCHI, params={"mine": True}).json()
    assert drugaya["prosyat"] == vsyo["prosyat"]

    root_client.delete(f"{API}/tasks/{zadacha.json()['id']}")


def test_zakrytaya_kategoriya_zavoditsya_i_ne_pokazyvaet_soderzhimoe(root_client, sotrudnik):
    """Ради этого категории и заводят: личные ключи сотрудника.

    Категория видна всем по имени и числу — спрячь её целиком, и рядом заведут
    вторую такую же; но содержимого чужому не видно.
    """
    kat = root_client.post(
        f"{KLYUCHI}/categories", json={"name": "Личные root", "zakrytaya": True}
    )
    assert kat.status_code == 201, kat.text
    assert kat.json()["zakrytaya"] is True
    klyuch = zavesti(root_client, title="Личный ключ root", category="Личные root")

    chuzhoy, _ = sotrudnik("klyuchi.zakrytaya.vidit@test.local")
    vidno = chuzhoy.get(KLYUCHI).json()
    nasha = next(k for k in vidno["categories"] if k["name"] == "Личные root")
    assert nasha["zakryta_dlya_menya"] is True, "чужая закрытая категория объявлена открытой"
    assert all(k["id"] != klyuch["id"] for k in vidno["items"]), "содержимое закрытой видно чужому"


def test_kategoriya_s_tem_zhe_imenem_ne_zavoditsya_dvazhdy(root_client):
    """Две «Бухгалтерии» — это две полки, на которых ищут одно и то же."""
    imya = {"name": "Бухгалтерия дважды"}
    assert root_client.post(f"{KLYUCHI}/categories", json=imya).status_code == 201
    otkaz = root_client.post(f"{KLYUCHI}/categories", json=imya)
    assert otkaz.status_code == 409, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_category_taken"


def test_kategoriyu_ubirayut_a_klyuchi_ostayutsya(root_client):
    """Категория — полка, а не коробка: убрали полку — ключи не пропали.

    Иначе «убрать категорию» однажды унесло бы вместе с ней десяток секретов,
    восстановить которые нечем.
    """
    kat = root_client.post(f"{KLYUCHI}/categories", json={"name": "Полка на снос"})
    assert kat.status_code == 201, kat.text
    klyuch = zavesti(root_client, title="Переживёт полку", category="Полка на снос")
    assert klyuch["category_id"] == kat.json()["id"]

    assert root_client.delete(f"{KLYUCHI}/categories/{kat.json()['id']}").status_code == 200

    spisok = root_client.get(KLYUCHI).json()
    nash = next((k for k in spisok["items"] if k["id"] == klyuch["id"]), None)
    assert nash is not None, "ключ исчез вместе с категорией"
    assert nash["category_id"] is None, "ключ остался в снесённой категории"
    assert all(k["name"] != "Полка на снос" for k in spisok["categories"])


def test_chuzhuyu_kategoriyu_ne_uberyot_kto_popalo(root_client, sotrudnik):
    """Убрать полку может тот, кто её завёл, и root — больше никто."""
    kat = root_client.post(f"{KLYUCHI}/categories", json={"name": "Не твоя полка"})
    assert kat.status_code == 201, kat.text
    chuzhoy, _ = sotrudnik("klyuchi.chuzhaya.kategoriya@test.local", ("keys.view", "keys.manage"))
    otkaz = chuzhoy.delete(f"{KLYUCHI}/categories/{kat.json()['id']}")
    assert otkaz.status_code == 403, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_category_not_owner"
    root_client.delete(f"{KLYUCHI}/categories/{kat.json()['id']}")


def test_chuzhaya_zakrytaya_kategoriya_klyucha_ne_prinimaet(root_client, sotrudnik):
    """**Полем категории нельзя отдать свой ключ чужому человеку.**

    Имя закрытой категории видно всем в левой колонке, поле свободного ввода, а
    условие видимости пускает хозяина закрытой категории к ЛЮБОМУ ключу внутри.
    То есть достаточно было набрать чужое имя — и второй фактор уезжал чужому
    молча, без отказа и без следа.
    """
    kat = root_client.post(
        f"{KLYUCHI}/categories", json={"name": "Личное root", "zakrytaya": True}
    )
    assert kat.status_code == 201, kat.text
    chuzhoy, _ = sotrudnik(
        "klyuchi.chuzhaya.zakrytaya@test.local", ("keys.view", "keys.create", "keys.edit")
    )

    otkaz = chuzhoy.post(
        KLYUCHI, json={"secret": STROKA, "title": "Мой банк", "category": "Личное root"}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_category_foreign_closed"

    # И переложить уже заведённый — тем же путём, тем же отказом.
    svoy = chuzhoy.post(KLYUCHI, json={"secret": STROKA, "title": "Мой банк"})
    assert svoy.status_code == 201, svoy.text
    perekladka = chuzhoy.patch(f"{KLYUCHI}/{svoy.json()['id']}", json={"category": "Личное root"})
    assert perekladka.status_code == 422, perekladka.text
    assert perekladka.json()["error"]["code"] == "key_category_foreign_closed"

    # Ключ чужого в закрытой полке root не появился.
    vidno = root_client.get(KLYUCHI, params={"category_id": kat.json()["id"]}).json()
    assert vidno["items"] == [], "чужой ключ всё же лёг в закрытую категорию"

    chuzhoy.delete(f"{KLYUCHI}/{svoy.json()['id']}")
    root_client.delete(f"{KLYUCHI}/categories/{kat.json()['id']}")


def test_svoyu_zakrytuyu_imenem_brat_mozhno(root_client):
    """Отказ выше — про ЧУЖУЮ. Своя закрытая полка остаётся полкой."""
    kat = root_client.post(
        f"{KLYUCHI}/categories", json={"name": "Своя закрытая", "zakrytaya": True}
    )
    assert kat.status_code == 201, kat.text
    klyuch = zavesti(root_client, title="В своей закрытой", category="Своя закрытая")
    assert klyuch["category_id"] == kat.json()["id"]
    root_client.delete(f"{KLYUCHI}/{klyuch['id']}")
    root_client.delete(f"{KLYUCHI}/categories/{kat.json()['id']}")


# --- напоминание -------------------------------------------------------------


def test_chuzhoe_napominanie_ne_privyazyvaetsya_bez_prava(root_client, sotrudnik):
    """**Перебором `task_id` вычитывался весь список напоминаний фирмы.**

    Карточка ключа отдаёт название и срок привязанного напоминания. Без проверки
    достаточно было своего ключа и сотни запросов, чтобы прочитать чужие
    напоминания мимо блока, который их охраняет.
    """
    zadacha = root_client.post(f"{API}/tasks", json={"title": "Отвезти документы в банк"})
    assert zadacha.status_code == 201, zadacha.text
    nomer = zadacha.json()["id"]

    bez_prava, _ = sotrudnik(
        "klyuchi.chuzhaya.zadacha@test.local", ("keys.view", "keys.create", "keys.edit")
    )
    svoy = bez_prava.post(KLYUCHI, json={"secret": STROKA, "title": "Свой ключ"})
    assert svoy.status_code == 201, svoy.text
    otkaz = bez_prava.patch(f"{KLYUCHI}/{svoy.json()['id']}", json={"task_id": nomer})
    assert otkaz.status_code == 403, otkaz.text
    assert "Отвезти документы" not in otkaz.text, "название чужого напоминания уехало в отказ"

    s_pravom, _ = sotrudnik(
        "klyuchi.svoya.zadacha@test.local",
        ("keys.view", "keys.create", "keys.edit", "tasks.view"),
    )
    ego = s_pravom.post(KLYUCHI, json={"secret": STROKA, "title": "Ключ с правом"})
    assert ego.status_code == 201, ego.text
    horosho = s_pravom.patch(f"{KLYUCHI}/{ego.json()['id']}", json={"task_id": nomer})
    assert horosho.status_code == 200, horosho.text
    assert horosho.json()["task"]["title"] == "Отвезти документы в банк"

    bez_prava.delete(f"{KLYUCHI}/{svoy.json()['id']}")
    s_pravom.delete(f"{KLYUCHI}/{ego.json()['id']}")
    root_client.delete(f"{API}/tasks/{nomer}")


def test_nesushchestvuyushchee_napominanie_ne_privyazyvaetsya(root_client):
    """Номер, за которым ничего нет, — отказ, а не молча пустая ссылка."""
    klyuch = zavesti(root_client, title="Ключ без напоминания")
    otkaz = root_client.patch(f"{KLYUCHI}/{klyuch['id']}", json={"task_id": 10_000_000})
    assert otkaz.status_code == 404, otkaz.text
    root_client.delete(f"{KLYUCHI}/{klyuch['id']}")


def test_vyklyuchennye_napominaniya_uhodyat_s_kartochki(root_client):
    """Выключенный блок исчезает ЦЕЛИКОМ — и с чужих экранов тоже (§3 CLAUDE.md).

    Иначе на карточке остаётся срок и ссылка `/tasks?id=…` на страницу, которой
    в выключенной системе нет, а в шапке горит тревога по разделу, которого нет.
    """
    klyuch = zavesti(root_client, title="Со сроком", category="Полка со сроком")
    zadacha = root_client.post(
        f"{API}/tasks",
        json={"title": "Сменить ключ: срок", "due_at": "2020-01-01T10:00:00Z"},
    )
    assert zadacha.status_code == 201, zadacha.text
    assert (
        root_client.patch(
            f"{KLYUCHI}/{klyuch['id']}", json={"task_id": zadacha.json()["id"]}
        ).status_code
        == 200
    )
    s_blokom = root_client.get(KLYUCHI).json()
    assert next(k for k in s_blokom["items"] if k["id"] == klyuch["id"])["task"] is not None
    assert s_blokom["prosyat"] >= 1

    root_client.post(f"{API}/modules/tasks", json={"enabled": False})
    try:
        bez_bloka = root_client.get(KLYUCHI).json()
        nash = next(k for k in bez_bloka["items"] if k["id"] == klyuch["id"])
        assert nash["task"] is None, "срок остался на карточке при выключенном блоке"
        assert bez_bloka["prosyat"] == 0, "тревога горит по выключенному блоку"
    finally:
        root_client.post(f"{API}/modules/tasks", json={"enabled": True})

    root_client.delete(f"{API}/tasks/{zadacha.json()['id']}")
    root_client.delete(f"{KLYUCHI}/{klyuch['id']}")


def test_otbor_trevogi_beryot_ves_razdel(root_client):
    """Число тревоги и список под ним берутся из ОДНОГО источника.

    Отбор по показанной полке давал под ненулевым числом пустой список: тревога
    считается по всему разделу, а полка — это полка.
    """
    klyuch = zavesti(root_client, title="Просрочен и в полке", category="Полка отбора")
    zadacha = root_client.post(
        f"{API}/tasks",
        json={"title": "Сменить ключ: отбор", "due_at": "2020-01-01T10:00:00Z"},
    )
    assert zadacha.status_code == 201, zadacha.text
    assert (
        root_client.patch(
            f"{KLYUCHI}/{klyuch['id']}", json={"task_id": zadacha.json()["id"]}
        ).status_code
        == 200
    )

    trevozhnye = root_client.get(KLYUCHI, params={"alarm": True}).json()
    assert len(trevozhnye["items"]) == trevozhnye["prosyat"], (
        "под числом тревоги показан не тот набор, по которому оно посчитано"
    )
    assert any(k["id"] == klyuch["id"] for k in trevozhnye["items"])
    # Не просроченный в отбор не попадает.
    tihiy = zavesti(root_client, title="Без напоминания")
    trevozhnye = root_client.get(KLYUCHI, params={"alarm": True}).json()
    assert all(k["id"] != tihiy["id"] for k in trevozhnye["items"])

    root_client.delete(f"{KLYUCHI}/{tihiy['id']}")
    root_client.delete(f"{API}/tasks/{zadacha.json()['id']}")
    root_client.delete(f"{KLYUCHI}/{klyuch['id']}")


# --- доступ в категорию ------------------------------------------------------


def test_v_kategoriyu_puskayut_i_ona_otkryvaet_vse_klyuchi_v_ney(root_client, sotrudnik):
    """Ради этого категория и описана: пустить бухгалтера в «Бухгалтерию», а не
    в восемь ключей по отдельности и потом в девятый.

    Таблица, миграция и условие запроса были, а возможности не было вовсе:
    строк в `key_category_access` не заводил никто.
    """
    kat = root_client.post(f"{KLYUCHI}/categories", json={"name": "Бухгалтерия"})
    assert kat.status_code == 201, kat.text
    nomer_kat = kat.json()["id"]
    pervyy = zavesti(root_client, title="Банк-клиент", category="Бухгалтерия")
    buhgalter, kto = sotrudnik("klyuchi.buhgalter@test.local")

    assert all(k["id"] != pervyy["id"] for k in buhgalter.get(KLYUCHI).json()["items"])

    otkryli = root_client.post(
        f"{KLYUCHI}/categories/{nomer_kat}/access", json={"user_id": kto, "otkryt": True}
    )
    assert otkryli.status_code == 200, otkryli.text
    assert next(c for c in otkryli.json()["people"] if c["id"] == kto)["otkryt"] is True

    vidno = buhgalter.get(KLYUCHI).json()
    assert any(k["id"] == pervyy["id"] for k in vidno["items"]), "доступ в категорию не открыл ключ"

    # И тот, что положат туда завтра, — тоже: доступ у полки, а не у ключа.
    vtoroy = zavesti(root_client, title="Налоговая", category="Бухгалтерия")
    assert any(k["id"] == vtoroy["id"] for k in buhgalter.get(KLYUCHI).json()["items"])

    zakryli = root_client.post(
        f"{KLYUCHI}/categories/{nomer_kat}/access", json={"user_id": kto, "otkryt": False}
    )
    assert zakryli.status_code == 200, zakryli.text
    assert all(k["id"] != pervyy["id"] for k in buhgalter.get(KLYUCHI).json()["items"])

    root_client.delete(f"{KLYUCHI}/{pervyy['id']}")
    root_client.delete(f"{KLYUCHI}/{vtoroy['id']}")
    root_client.delete(f"{KLYUCHI}/categories/{nomer_kat}")


def test_zakrytaya_kategoriya_spiska_dostupa_ne_prinimaet(root_client, sotrudnik):
    """Закрытая списков не слушает — это её определение.

    Записать в неё доступ значило бы завести строку, которая никогда ничего не
    откроет: условие видимости требует незакрытой категории.
    """
    kat = root_client.post(
        f"{KLYUCHI}/categories", json={"name": "Закрытая для списка", "zakrytaya": True}
    )
    assert kat.status_code == 201, kat.text
    _, kto = sotrudnik("klyuchi.zakrytaya.spisok@test.local")
    otkaz = root_client.post(
        f"{KLYUCHI}/categories/{kat.json()['id']}/access", json={"user_id": kto, "otkryt": True}
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_category_closed"
    root_client.delete(f"{KLYUCHI}/categories/{kat.json()['id']}")


def test_chuzhuyu_kategoriyu_ne_otkroet_kto_popalo(root_client, sotrudnik):
    """Распоряжается полкой тот, кто её завёл, и root — больше никто.

    Иначе доступ к чужим ключам раздавал бы всякий, у кого есть право на раздел.
    """
    kat = root_client.post(f"{KLYUCHI}/categories", json={"name": "Не раздавай"})
    assert kat.status_code == 201, kat.text
    chuzhoy, kto = sotrudnik("klyuchi.ne.razdavay@test.local", ("keys.view", "keys.manage"))
    otkaz = chuzhoy.post(
        f"{KLYUCHI}/categories/{kat.json()['id']}/access", json={"user_id": kto, "otkryt": True}
    )
    assert otkaz.status_code == 403, otkaz.text
    assert otkaz.json()["error"]["code"] == "key_category_not_owner"
    root_client.delete(f"{KLYUCHI}/categories/{kat.json()['id']}")


def test_nechitaemye_zapasnye_ne_vydayut_sebya_za_nezavedyonnye(root_client, db):
    """НАЙДЕНО РАЗБОРОМ: «их не заводили» вместо «лежат, но не открываются».

    Так выглядит сменившийся `OPENCRM_SECRET_KEY`: шифротекст на месте, ключа к
    нему нет. Ответ «запасных не заводили» уводит от восстановления, которое ещё
    возможно, — а старый ключ ищут, пока старая машина жива. Почта на то же
    состояние отвечает `mail_password_undecryptable`, и здесь должно быть так же.
    """
    from database.models import TwoFactorKey

    klyuch = zavesti(root_client, title="Нечитаемые запасные", backup_codes=["aaa-111", "bbb-222"])
    try:
        # Порча одного байта неотличима от чужого ключа — тот же `SecretBoxError`.
        # Пишем с фиксацией: беду обязан увидеть сервер, а не наша сессия.
        v_baze = db.get(TwoFactorKey, klyuch["id"])
        v_baze.backup_codes_encrypted = "ne-shifrotekst-vovse"
        db.commit()

        kartochka = next(
            k for k in root_client.get(KLYUCHI).json()["items"] if k["id"] == klyuch["id"]
        )
        assert kartochka["backup_zakryty"] is True, "нечитаемый список выдан за незаведённый"

        okno = root_client.get(f"{KLYUCHI}/{klyuch['id']}/backup-codes")
        assert okno.status_code == 200, okno.text
        assert okno.json()["zakryty"] is True

        # И затереть его нельзя: старый ключ ещё может найтись.
        otkaz = root_client.patch(f"{KLYUCHI}/{klyuch['id']}", json={"backup_codes": ["новый-1"]})
        assert otkaz.status_code == 422, otkaz.text
        assert otkaz.json()["error"]["code"] == "key_backup_undecryptable"
        db.expire_all()
        assert db.get(TwoFactorKey, klyuch["id"]).backup_codes_encrypted == "ne-shifrotekst-vovse", (
            "нечитаемый шифротекст всё же затёрли — восстанавливать больше нечего"
        )
    finally:
        # За собой убираем сами: фикстура `db` откатывает свою сессию, а
        # испорченную строку мы зафиксировали.
        root_client.delete(f"{KLYUCHI}/{klyuch['id']}")
        root_client.delete(f"{KLYUCHI}/{klyuch['id']}/forever")
