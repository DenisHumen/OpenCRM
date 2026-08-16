"""Цифры утренней сводки: считаются, и считаются те самые.

Сводка уходит владельцу раз в сутки, и правит она поведением: увидев «выручка
за сутки 0», он идёт разбираться. Значит ошибка в счёте здесь дороже, чем
ошибка на экране: экран человек перепроверит соседним разделом, а сводке
поверит.

Проверок две породы, и обе нужны. Первая — что запрос вообще выполняется на
MySQL: здесь есть `IF()` и `NOT EXISTS` с коррелирующим подзапросом, которые
на другом движке пишутся иначе, а разойтись с настоящей базой они могут молча.
Вторая — что окно ЗАКРЫТО слева и открыто справа: событие ровно на границе
обязано попасть в одни сутки, а не в двое или ни в одни.
"""

from datetime import datetime, timedelta

from database.models import Client, Task
from database.repositories import svodka
from database.session import SessionLocal


def _sessiya():
    return SessionLocal()


def test_zaprosy_svodki_vypolnyayutsya_na_mysql():
    """Каждый счёт доходит до базы и возвращает число.

    Проверка кажется пустой, но ловит ровно то, что здесь всего опаснее:
    выражение, собранное неверно, падает не при сборке, а при выполнении — то
    есть в шесть утра на боевом сервере, и вместо сводки не приходит ничего.
    """
    seychas = datetime.utcnow()
    ot, do = seychas - timedelta(days=1), seychas

    with _sessiya() as db:
        za_sutki = svodka.za_sutki(db, ot, do)
        s_sayta = svodka.zayavki_s_sayta(db, ot, do, "site")
        razgovory = svodka.zvonki(db, ot, do)
        prosrocheno = svodka.prosrocheno_napominaniy(db, seychas)
        v_minuse = svodka.tovary_v_minuse(db)

    for imya, znachenie in (
        ("novyh_klientov", za_sutki["novyh_klientov"]),
        ("novyh_zayavok", za_sutki["novyh_zayavok"]),
        ("zakryto", za_sutki["zakryto"]),
        ("vyruchka_minor", za_sutki["vyruchka_minor"]),
        ("s_sayta.vsego", s_sayta["vsego"]),
        ("s_sayta.netronutye", s_sayta["netronutye"]),
        ("zvonki.vsego", razgovory["vsego"]),
        ("zvonki.propushcheno", razgovory["propushcheno"]),
        ("prosrocheno", prosrocheno),
        ("tovary_v_minuse", v_minuse),
    ):
        assert isinstance(znachenie, int), f"{imya} вернул не число: {znachenie!r}"
        assert znachenie >= 0, f"{imya} отрицательный: {znachenie}"


def test_okno_zakryto_sleva_i_otkryto_sprava():
    """Событие ровно на границе попадает в одни сутки, а не в двое.

    Иначе сводка врёт дважды в один день: вчерашняя посчитала это событие, и
    сегодняшняя посчитает его же. Заметить такое по одному сообщению нельзя —
    только сложив два, чего никто не делает.

    Момент рождения задаётся ЯВНО и уводится в год, где никто больше ничего не
    заводит. Первая редакция брала «сейчас» и в одиночку проходила, а в полном
    наборе краснела: соседние проверки заводят клиентов в ту же секунду, и счёт
    ловил чужих. Проверка границы обязана смотреть на одну строку, а не на
    всех, кто оказался рядом.
    """
    mig = datetime(2031, 3, 3, 12, 0, 0)

    with _sessiya() as db:
        db.add(Client(name="Клиент ровно на границе", created_at=mig))
        db.commit()

        svoyo = svodka.za_sutki(db, mig, mig + timedelta(seconds=1))
        chuzhoe = svodka.za_sutki(db, mig - timedelta(seconds=1), mig)

    assert svoyo["novyh_klientov"] == 1, (
        f"событие не попало в своё окно: {svoyo['novyh_klientov']}"
    )
    assert chuzhoe["novyh_klientov"] == 0, (
        "событие сосчиталось и в предыдущих сутках — сводка посчитает его дважды"
    )


def test_prosrochennoe_napominanie_popadaet_v_trebuet_vnimaniya(root_client):
    """Просроченное напоминание видно в сводке — по всем сотрудникам сразу.

    Именно по всем: у сводки вопрос не «что должен я», а «что провисает в
    деле». Счётчик на экране считает своё, и переиспользовать его здесь значило
    бы показать владельцу чужую половину картины.
    """
    seychas = datetime.utcnow()

    with _sessiya() as db:
        bylo = svodka.prosrocheno_napominaniy(db, seychas)
        db.add(
            Task(
                title="Просроченное для сводки",
                due_at=seychas - timedelta(hours=3),
                assignee_id=None,
            )
        )
        db.commit()
        stalo = svodka.prosrocheno_napominaniy(db, seychas)

    assert stalo == bylo + 1, (
        f"просроченное напоминание не попало в счёт: было {bylo}, стало {stalo}"
    )


def test_netronutaya_zayavka_s_sayta_vidna(root_client):
    """Заявка с сайта, которой никто не занялся, попадает в «требует внимания».

    «Не занялся» — это отсутствие перехода, сделанного человеком, а не «стоит
    на первом». Заявку могли передвинуть и вернуть, и такая рукам менеджера уже
    досталась.

    Отметка о заведении в журнале есть у КАЖДОЙ заявки (правило проекта —
    «журнал заполняется всегда»), и отличается она пустым `from_stage`. Первая
    редакция счёта этого не учла и давала ноль нетронутых всегда; поймано этой
    самой проверкой.
    """
    from tests.conftest import API

    seychas = datetime.utcnow()
    ot, do = seychas - timedelta(minutes=5), seychas + timedelta(minutes=5)

    klient = root_client.post(
        f"{API}/clients", json={"name": "С сайта для сводки", "source": "site"}
    )
    assert klient.status_code == 201, klient.text
    zayavka = root_client.post(
        f"{API}/deals", json={"title": "Заявка с сайта", "client_id": klient.json()["id"]}
    )
    assert zayavka.status_code == 201, zayavka.text

    with _sessiya() as db:
        itog = svodka.zayavki_s_sayta(db, ot, do, "site")

    assert itog["vsego"] >= 1, "заявка с сайта не сосчиталась вовсе"
    assert itog["netronutye"] >= 1, (
        "заявка, которой никто не занимался, не попала в нетронутые"
    )


def test_zayavka_s_perekhodom_uhodit_iz_netronutyh(root_client):
    """Парная: заявку сдвинули — из «требует внимания» она уходит.

    Без этой проверки соседняя зеленела бы и на счёте, который считает
    нетронутыми ВСЕ заявки подряд.
    """
    from tests.conftest import API

    seychas = datetime.utcnow()
    ot, do = seychas - timedelta(minutes=5), seychas + timedelta(minutes=5)

    klient = root_client.post(
        f"{API}/clients", json={"name": "С сайта сдвинутая", "source": "site"}
    ).json()
    zayavka = root_client.post(
        f"{API}/deals", json={"title": "Сдвинутая заявка", "client_id": klient["id"]}
    ).json()

    with _sessiya() as db:
        do_sdviga = svodka.zayavki_s_sayta(db, ot, do, "site")["netronutye"]

    etapy = root_client.get(f"{API}/pipeline/stages").json()["items"]
    drugoy = next(e["key"] for e in etapy if e["key"] != zayavka["stage"])
    sdvig = root_client.patch(f"{API}/deals/{zayavka['id']}", json={"stage": drugoy})
    assert sdvig.status_code == 200, sdvig.text

    with _sessiya() as db:
        posle = svodka.zayavki_s_sayta(db, ot, do, "site")["netronutye"]

    assert posle == do_sdviga - 1, (
        f"после перехода этапа заявка осталась в нетронутых: было {do_sdviga}, стало {posle}"
    )
