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

Порода третья, снизу файла, — про сам факт прибытия, а не про цифры. Сводка
уходит каждое утро, и пока она приходит, известно, что живы сервер, база и
бот: тревоги при отвалившемся боте тоже не придут, и узнать об этом иначе
неоткуда. Значит **молчание сводки обязано быть заметным**, а для этого она не
имеет права молчать, когда сказать нечего: пустые сутки и отозванный токен
выглядят одинаково.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from database.models import Client, Task
from database.repositories import svodka
from database.session import SessionLocal
from tests.conftest import API


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
        ("vyigrano_minor", za_sutki["vyigrano_minor"]),
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


# --- окно суток идёт по настоящему поясу, а не по записанному числом смещению ---
#
# Беда, которую эти три проверки сторожат, была такой: `POYAS` стоял
# `timezone(timedelta(hours=3))`. Киев летом +3, зимой +2, и полгода — с
# последнего воскресенья октября по последнее воскресенье марта — окно «за
# прошедшие сутки» съезжало на час. Цифры оставались правдоподобными: они просто
# включали чужой вечерний час и теряли свой. Такое не замечают ни по одной
# сводке, ни по десяти.
#
# Проверяется не «какая константа записана», а поведение: границы окна в UTC
# обязаны стоять по-разному зимой и летом. На старом коде они совпадали.


def test_granitsy_okna_zimoy_i_letom_stoyat_po_raznomu():
    """Одно и то же время суток даёт РАЗНЫЕ границы в UTC зимой и летом.

    Ядро проверки — сравнение двух дат, а не сверка с числом: смещение, зашитое
    константой, даёт одинаковый час UTC круглый год, и именно это здесь
    запрещено. Оба ожидаемых значения выписаны рядом явно, чтобы поломка
    называла себя сама, а не оставляла «21 не равно 22».
    """
    from core.services import svodka_service

    # Полдень по Киеву в оба захода: 09:00 UTC зимой — это 11:00 местных,
    # летом — 12:00. День недели и месяц выбраны заведомо далеко от переходов.
    zima = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    leto = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)

    ot_z, do_z, den_z = svodka_service._sutki(zima)
    ot_l, do_l, den_l = svodka_service._sutki(leto)

    # Зимой Киев +2: местная полночь — это 22:00 UTC предыдущего дня.
    assert (ot_z, do_z) == (datetime(2026, 1, 13, 22, 0), datetime(2026, 1, 14, 22, 0)), (
        f"зимнее окно посчитано не по киевскому времени: {ot_z} .. {do_z}"
    )
    # Летом Киев +3: та же местная полночь — уже 21:00 UTC.
    assert (ot_l, do_l) == (datetime(2026, 7, 13, 21, 0), datetime(2026, 7, 14, 21, 0)), (
        f"летнее окно посчитано не по киевскому времени: {ot_l} .. {do_l}"
    )
    # И главное — они обязаны РАЗЛИЧАТЬСЯ. Зашитое числом смещение даёт здесь
    # один и тот же час UTC, и эта строка краснеет первой.
    assert do_z.time() != do_l.time(), (
        f"граница суток стоит в один и тот же час UTC зимой и летом ({do_z.time()}) — "
        "значит пояс записан числом, а не взят из базы поясов"
    )

    assert den_z == "14.01.2026", f"зимняя подпись не про вчера: {den_z}"
    assert den_l == "14.07.2026", f"летняя подпись не про вчера: {den_l}"


def test_v_noch_perevoda_strelok_okno_dlinoy_v_nastoyashchie_sutki():
    """Сутки перевода стрелок длятся 23 и 25 часов — окно обязано быть таким же.

    Проверка не про красоту, а про двойной счёт на стыке. Вычитай сервис ровно
    24 часа — весной окно захватило бы лишний час позапрошлых суток (его уже
    посчитала вчерашняя сводка), а осенью потеряло бы час своих. Оба раза цифры
    выглядят обычно, и разойтись они могут только у того, кто сложит две сводки
    подряд, — то есть ни у кого.

    Даты — настоящие переходы Europe/Kyiv в 2026-м: 29 марта стрелки идут
    вперёд, 25 октября — назад.
    """
    from core.services import svodka_service

    # «Сейчас» — утро СЛЕДУЮЩЕГО дня: сводка отчитывается за вчера, и вчера
    # здесь как раз день перевода.
    vesna_ot, vesna_do, vesna_den = svodka_service._sutki(
        datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc)
    )
    osen_ot, osen_do, osen_den = svodka_service._sutki(
        datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    )

    assert vesna_den == "29.03.2026", f"весной отчитались не за день перевода: {vesna_den}"
    assert osen_den == "25.10.2026", f"осенью отчитались не за день перевода: {osen_den}"

    assert vesna_do - vesna_ot == timedelta(hours=23), (
        f"сутки перевода вперёд посчитаны как {vesna_do - vesna_ot}, а длились 23 часа: "
        f"{vesna_ot} .. {vesna_do}"
    )
    assert osen_do - osen_ot == timedelta(hours=25), (
        f"сутки перевода назад посчитаны как {osen_do - osen_ot}, а длились 25 часов: "
        f"{osen_ot} .. {osen_do}"
    )


def test_baza_poyasov_dostupna_i_kiev_v_ney_perevodit_strelki():
    """Пояс берётся из базы IANA, и база эта на месте.

    Отдельной проверкой, потому что отказ здесь особого рода: `ZoneInfo` без
    базы поясов бросает `ZoneInfoNotFoundError` на ИМПОРТЕ `svodka_service`, то
    есть сводка не съезжает на час, а не собирается вовсе — и вместе с ней не
    поднимается всё, что этот модуль импортирует. На slim-образах и на Windows
    системной базы может не быть, поэтому пакет `tzdata` записан в зависимости
    явно; эта проверка сторожит, что его оттуда не вынули.
    """
    from core.services import svodka_service

    zimoy = datetime(2026, 1, 15, 12, 0, tzinfo=svodka_service.POYAS).utcoffset()
    letom = datetime(2026, 7, 15, 12, 0, tzinfo=svodka_service.POYAS).utcoffset()

    assert zimoy == timedelta(hours=2), f"зимнее смещение Киева не +2, а {zimoy}"
    assert letom == timedelta(hours=3), f"летнее смещение Киева не +3, а {letom}"


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


# --- молчание сводки обязано быть заметным ------------------------------------
#
# Ниже проверяется не содержание сводки, а то, что она вообще приходит и что
# её неприход виден. Свойство это невидимо в коде и проверяется только так:
# ошибка здесь не роняет ничего, она просто выключает единственный признак
# жизни канала, и обнаруживается это тогда, когда клиент не дождался ответа.

#: Форма настоящего токена: цифры, двоеточие, буквенно-цифровой хвост.
#: Свой, а не взятый из `test_telegram.py`: связывать два набора проверок общей
#: строкой значит ронять один правкой другого.
OBRAZETS_TOKENA = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

#: Куда сводка уходит в этих проверках.
CHAT_SVODKI = 123456789

#: Утро суток, в которых заведомо ничего не происходило.
#:
#: Год в будущем, потому что обнулить базу ради одной проверки нельзя —
#: соседние идут следом и по той же базе. Задано в UTC и с поясом явно: сводка
#: считает сутки по местному времени (`svodka_service.POYAS`), и наивное «сейчас»
#: означало бы окно, зависящее от пояса машины, где идёт прогон.
PUSTOY_DEN = datetime(2032, 5, 5, 6, 0, tzinfo=timezone.utc)

#: Какой день окажется в заголовке при `PUSTOY_DEN`: сводка отчитывается за
#: ВЧЕРА по местному поясу, а не за сегодня.
PUSTOY_DEN_V_ZAGOLOVKE = "04.05.2032"


@pytest.fixture()
def bot_so_svodkoy(root_client):
    """Блок включён, бот настроен, чат сводки назван — всё, что ей нужно.

    Гасим за собой: блок `telegram` выключен по умолчанию, и оставленный
    включённым он меняет ответы соседних проверок — набор гоняется в обоих
    порядках, и такая поломка приходит не туда, где её завели.
    """
    vklyuchen = root_client.post(f"{API}/modules/telegram", json={"enabled": True})
    assert vklyuchen.status_code == 200, vklyuchen.text
    nastroeno = root_client.put(
        f"{API}/telegram/settings",
        json={"token": OBRAZETS_TOKENA, "digest_chat": str(CHAT_SVODKI)},
    )
    assert nastroeno.status_code == 200, nastroeno.text
    yield
    # Настройки снимаются ДО выключения блока: у выключенного ручка настроек
    # отвечает `module_disabled`, и токен остался бы в базе до конца прогона.
    root_client.delete(f"{API}/telegram/settings")
    root_client.post(f"{API}/modules/telegram", json={"enabled": False})


def test_pustye_sutki_ne_otmenyayut_svodku(bot_so_svodkoy, monkeypatch):
    """В сутках не случилось ничего — сводка всё равно уходит и говорит это вслух.

    **Проверка ровно того, ради чего сводка существует.** Она — единственный
    признак, что канал жив; отозванный токен, упавший контейнер и снятый
    вебхук дают ту же тишину, что и спокойный день, и тревоги в этих случаях
    тоже не придут. Начни сводка пропускать пустые сутки — молчание перестало
    бы что-либо значить, и отвалившийся бот обнаружился бы не раньше, чем
    клиент не дождался ответа.

    Поэтому проверяется двойное: сообщение УШЛО и в нём НАЗВАНЫ нули. Одного
    первого мало — сводка, ушедшая с пустым телом, вызывает телеграм точно так
    же, а читается как «сегодня новостей нет», то есть как молчание.

    Сети здесь нет и быть не должно: настоящий бот означал бы настоящее
    сообщение настоящему человеку на каждый прогон.
    """
    from core.services import svodka_service, telegram_service

    ushlo = []
    monkeypatch.setattr(
        telegram_service,
        "poslat_tekst_razmetkoy",
        lambda kluch, chat_id, text, opener=None: ushlo.append((chat_id, text))
        or {"message_id": 1},
    )

    with _sessiya() as db:
        itog = svodka_service.otpravit(db, seychas=PUSTOY_DEN)

    assert itog["status"] == "sent", f"в пустые сутки сводка не ушла вовсе: {itog}"
    assert len(ushlo) == 1, f"сводка ушла не один раз: {ushlo}"

    chat_id, tekst = ushlo[0]
    assert chat_id == CHAT_SVODKI, f"сводка ушла не в тот чат: {chat_id}"
    assert f"Сводка за {PUSTOY_DEN_V_ZAGOLOVKE}" in tekst, (
        f"в сводке нет дня, за который она отчитывается:\n{tekst}"
    )
    # Нули названы, а не пропущены. «Ничего не произошло» — это сообщение,
    # отсутствие строки — нет.
    assert "Заявок новых: <b>0</b>" in tekst, f"пустые сутки промолчали о заявках:\n{tekst}"
    assert "закрыто: <b>0</b>" in tekst, f"пустые сутки промолчали о закрытых:\n{tekst}"
    # Слова «выручка» здесь нет намеренно: при выключенной кассе система не
    # ведёт полученных денег, и называть выручкой сумму выигранных заявок значит
    # выдавать одно за другое — ровно то, из-за чего главная показывала пустой
    # месяц при полной кассе.
    assert "выиграно на сумму: <b>0.00" in tekst, f"пустые сутки промолчали о деньгах:\n{tekst}"
    assert "выручка:" not in tekst, f"суммой заявок названа выручка:\n{tekst}"
    assert "Клиентов новых: <b>0</b>" in tekst, f"пустые сутки промолчали о клиентах:\n{tekst}"


def test_otkaz_otpravki_viden_v_otvete_i_v_zhurnale(bot_so_svodkoy, monkeypatch, caplog):
    """Телеграм отказал — это сказано ответом и записано в журнал, а не съедено.

    Отказ отправки — единственный случай, когда о неприходе сводки можно узнать
    ДО того, как её хватятся. Проглоти его сервис (вернув «ушла» или промолчав
    в журнале) — и разбирать пришлось бы по отсутствию сообщения, то есть по
    тому, чего нет: в журнале ровно ничего, у телеграма спросить нечего,
    отличить «отказали» от «расписание не сработало» нечем.

    Журнал проверяется отдельно от ответа намеренно: ответ читает человек,
    нажавший «отправить сейчас», а ночной запуск не читает никто — там журнал
    единственный след.
    """
    from core.services import svodka_service, telegram_service

    def otkazat(*args, **kwargs):
        raise telegram_service.TelegramOtkaz("chat not found")

    monkeypatch.setattr(telegram_service, "poslat_tekst_razmetkoy", otkazat)

    with caplog.at_level(logging.ERROR, logger="core.services.svodka_service"):
        with _sessiya() as db:
            itog = svodka_service.otpravit(db, seychas=PUSTOY_DEN)

    assert itog["status"] == "failed", f"отказ телеграма выдан за успех: {itog}"
    assert "chat not found" in itog.get("error", ""), (
        f"причина отказа потерялась по дороге: {itog}"
    )

    zapisi = [
        z
        for z in caplog.records
        if z.name == "core.services.svodka_service" and z.levelno >= logging.ERROR
    ]
    assert zapisi, "отказ отправки не оставил в журнале ни одной записи уровня ошибки"
    assert any("chat not found" in z.getMessage() for z in zapisi), (
        f"в журнале есть запись, но без причины отказа: {[z.getMessage() for z in zapisi]}"
    )


def test_skript_svodki_vozvrashchaet_edinitsu_pri_otkaze(monkeypatch, capsys):
    """Сводка не ушла — скрипт краснеет кодом возврата и называет причину.

    Расписание (`deploy/systemd/opencrm-svodka.timer` → `scripts/svodka.py`) не
    читает ни ответов, ни текста: оно видит ровно код возврата. Верни скрипт
    ноль на несостоявшейся отправке — systemd записал бы «успех», сводки бы не
    было, и в журнале службы стояло бы подтверждение, что всё хорошо. Это хуже
    молчания: молчание хотя бы ничего не обещает.

    Причина при этом уходит в поток ОШИБОК, а не в обычный вывод: `journalctl -p err`
    — первое, куда смотрят, и попасть туда сводка обязана сама.
    """
    from scripts import svodka as skript

    monkeypatch.setattr(
        skript.svodka_service,
        "otpravit",
        lambda db, *args, **kwargs: {"status": "failed", "error": "Unauthorized"},
    )

    kod = skript.main()
    vyvod = capsys.readouterr()

    assert kod == 1, "несостоявшаяся отправка вернула успех — расписание запишет «сводка ушла»"
    assert "Unauthorized" in vyvod.err, (
        f"причина отказа не попала в поток ошибок: out={vyvod.out!r} err={vyvod.err!r}"
    )


def test_skript_svodki_ne_krasneet_kogda_kanal_ne_nastroen(monkeypatch, capsys):
    """Телеграм не настроен — это не отказ: ноль и объяснение обычным выводом.

    Парная к предыдущей и не менее нужная. Верни скрипт единицу тому, кто
    телеграмом не пользуется, — расписание краснело бы каждое утро по замыслу,
    а красное по замыслу расписание перестают читать вместе с настоящими
    отказами. Тогда обе проверки выше становятся бесполезны: код возврата
    перестаёт что-либо значить.
    """
    from scripts import svodka as skript

    monkeypatch.setattr(
        skript.svodka_service,
        "otpravit",
        lambda db, *args, **kwargs: {"status": "skipped", "reason": "not_configured"},
    )

    kod = skript.main()
    vyvod = capsys.readouterr()

    assert kod == 0, "ненастроенный канал выдан за отказ — расписание будет краснеть всегда"
    assert "not_configured" in vyvod.out, (
        f"скрипт промолчал о том, почему сводка не отправлялась: {vyvod.out!r}"
    )
    assert vyvod.err == "", f"«не настроено» ушло в поток ошибок как отказ: {vyvod.err!r}"


# --- неприход сводки обязан быть заметен --------------------------------------


def test_uspeshnaya_svodka_ostavlyaet_otmetku_vremeni(root_client, bot_so_svodkoy, monkeypatch):
    """Ушла — отметились. Не ушла — отметки нет.

    Обе половины вместе, потому что порознь каждая зеленеет на неверном
    поведении: «отметка есть» верно и для отметки, которая ставится всегда, а
    «отметки нет» — для той, что не ставится никогда.

    Отметка — единственное, по чему видно, что сводка вообще уходила. Отказ
    отправки виден в журнале и в коде возврата, а несостоявшийся запуск (таймер
    сорвался, контейнер лежал) не оставляет ничего: телеграм молчит ровно так
    же, как в спокойный день.
    """
    from core.services import svodka_service
    from database.session import SessionLocal

    monkeypatch.setattr(
        svodka_service.telegram_service,
        "poslat_tekst_razmetkoy",
        lambda kluch, chat_id, text, opener=None: {"message_id": 1},
    )
    with SessionLocal() as db:
        itog = svodka_service.otpravit(db)
        assert itog["status"] == "sent", itog
        otmetka = svodka_service.kogda_poslednyaya(db)
    assert otmetka, "успешная сводка не оставила отметки времени"

    # Теперь отказ: отметка обязана остаться ПРЕЖНЕЙ, а не обновиться.
    monkeypatch.setattr(
        svodka_service.telegram_service,
        "poslat_tekst_razmetkoy",
        _otkaz_otpravki,
    )
    with SessionLocal() as db:
        itog = svodka_service.otpravit(db)
        assert itog["status"] == "failed", itog
        assert svodka_service.kogda_poslednyaya(db) == otmetka, (
            "неудавшаяся сводка обновила отметку — по ней всё выглядит живым"
        )


def _otkaz_otpravki(kluch, chat_id, text, opener=None):
    from core.services import telegram_service

    raise telegram_service.TelegramOtkaz("chat not found")


def test_metrika_svodki_poyavlyaetsya_tolko_posle_otpravki(root_client, bot_so_svodkoy, monkeypatch):
    """Ряда нет, пока сводка ни разу не уходила.

    Выдуманный ноль означал бы «последняя сводка в 1970 году» и тревогу на
    свежей установке — то есть тревогу, которую первым делом заглушат, а вместе
    с ней и настоящую.
    """
    from core.services import svodka_service
    from database.session import SessionLocal

    with SessionLocal() as db:
        svodka_service.settings_repo.write_many(db, {svodka_service.SETTING_POSLEDNYAYA: ""})
        db.commit()

    # Метрики закрыты своим блоком: без него ручка честно отвечает отказом, и
    # проверка читала бы его вместо рядов.
    root_client.post(f"{API}/modules/monitoring", json={"enabled": True})
    pusto = root_client.get(f"{API}/metrics").text
    assert "opencrm_digest_last_timestamp_seconds" not in pusto, (
        "метрика показывает время сводки, которой не было"
    )

    monkeypatch.setattr(
        svodka_service.telegram_service,
        "poslat_tekst_razmetkoy",
        lambda kluch, chat_id, text, opener=None: {"message_id": 1},
    )
    with SessionLocal() as db:
        assert svodka_service.otpravit(db)["status"] == "sent"

    posle = root_client.get(f"{API}/metrics").text
    root_client.post(f"{API}/modules/monitoring", json={"enabled": False})
    assert "opencrm_digest_last_timestamp_seconds" in posle, (
        "сводка ушла, а метрики о ней нет — тревога на молчание не сработает"
    )


def test_trevoga_na_molchanie_svodki_est_v_pravilah():
    """Метрика без тревоги — это график, на который никто не смотрит.

    Проверка сторожит связку: ряд отдаётся приложением, а замечает молчание
    Prometheus. Сломается любая половина — молчание снова станет неотличимым от
    спокойного дня.
    """
    import pathlib

    pravila = (
        pathlib.Path(__file__).resolve().parent.parent
        / "docker" / "monitoring" / "prometheus" / "rules" / "opencrm.yml"
    )
    if not pravila.exists():
        import pytest

        pytest.skip("правила Prometheus не рядом")

    text = pravila.read_text(encoding="utf-8")
    assert "opencrm_digest_last_timestamp_seconds" in text, (
        "тревоги на молчание сводки нет: ряд собирается, а смотреть на него некому"
    )
