"""Рост переписки и уборка старого: что именно уходит, а что обязано остаться.

Проверки здесь сторожат РЕШЕНИЕ, а не работоспособность кода. Решение такое:
молчаливого удаления переписки не бывает. Отсюда четыре свойства, каждое из
которых ломается незаметно, а замечается через полгода на чужом разбирательстве:

1. **По умолчанию не удаляется ничего.** Пока владелец не назвал срок, уборка
   выключена. Сломайся это — и переписка исчезала бы у тех, кто такого решения
   не принимал; узнали бы об этом тогда, когда она понадобилась.
2. **Уходит ровно то, что старше названной границы.** Ошибка на сутки в одну
   сторону оставляет мусор, в другую — уносит то, что обещали хранить.
3. **Файлы уходят вместе со строками, а лишнего не трогается.** Аватар лежит в
   том же каталоге и уборке не подлежит; путь вне каталога канала не стирается
   вовсе.
4. **Записи в ленте клиента остаются.** Это отдельное решение, и оно
   противоположно первому впечатлению: убирая переписку, историю карточки не
   переписывают.

Плюс два свойства самого прогона: он ограничен по времени (иначе миллион строк
одной транзакцией кладёт базу) и оставляет след числами (иначе это и есть
молчаливое удаление, только по расписанию).

Всё это гоняется против настоящей MySQL: условная сумма по пяти срокам в одном
запросе (`ves_perepiski`) и удаление по списку идентификаторов — ровно те места,
которые на другом движке пишутся иначе и расходятся молча.
"""

from datetime import datetime, timedelta

import pytest

from config.settings import get_settings
from core import exceptions as errors
from core.services import telegram_uborka
from database.models import Client, ClientNote, TelegramChat, TelegramMessage
from database.models.telegram import DIRECTION_IN, KIND_PHOTO, KIND_TEXT
from database.repositories import settings as settings_repo
from database.repositories import telegram as telegram_repo
from database.session import SessionLocal

#: Приметный номер чата: свои строки ищутся по нему, а не счётом всей таблицы.
#: База у набора общая, и «сколько всего сообщений» — вопрос не к этому файлу.
NASH_CHAT = 777000111222333


def _sessiya():
    return SessionLocal()


@pytest.fixture()
def stend():
    """Диалог со своими сообщениями и файлами. Убирается за собой в любом исходе.

    Убирается руками, а не откатом: служба уборки фиксирует транзакции сама (в
    этом и смысл пачек), и фикстура с откатом здесь ничего бы не откатила.
    """
    hranilishche = get_settings().storage_dir / "telegram" / str(NASH_CHAT)
    hranilishche.mkdir(parents=True, exist_ok=True)

    with _sessiya() as db:
        _snesti(db)
        dialog = TelegramChat(chat_id=NASH_CHAT, title="Уборка", username="uborka")
        db.add(dialog)
        db.flush()
        dialog_id = dialog.id
        db.commit()

    yield dialog_id

    with _sessiya() as db:
        _snesti(db)
        settings_repo.write(db, telegram_uborka.SETTING_SROK, "")
        settings_repo.write(db, telegram_uborka.SETTING_PROGON, "")
        db.commit()
    # Каталог мог унести сам прогон, опустошив его: `_ubrat_pustoy_katalog`
    # сносит опустевшие. Поэтому уборка за собой терпит его отсутствие.
    if hranilishche.exists():
        for fayl in sorted(hranilishche.glob("*")):
            fayl.unlink(missing_ok=True)
        hranilishche.rmdir()


def _snesti(db) -> None:
    """Снести свой диалог со всей перепиской. Только свой — по приметному номеру."""
    for dialog in db.query(TelegramChat).filter(TelegramChat.chat_id == NASH_CHAT).all():
        db.query(TelegramMessage).filter(TelegramMessage.chat_id == dialog.id).delete()
        db.delete(dialog)


def _polozhit(db, dialog_id: int, kogda: datetime, *, fayl: str = "") -> int:
    """Сообщение в диалоге с назначенным временем. Возвращает идентификатор.

    `file_size` берётся с диска, а не выдумывается. Числа на экране считаются по
    этой колонке, а освобождённые уборкой байты меряются по файлу — разойдись
    они в подставных данных, проверка сторожила бы не то, что показывают.
    """
    na_diske = get_settings().storage_dir / fayl if fayl else None
    razmer = na_diske.stat().st_size if na_diske is not None and na_diske.exists() else None
    stroka = TelegramMessage(
        chat_id=dialog_id,
        direction=DIRECTION_IN,
        kind=KIND_PHOTO if fayl else KIND_TEXT,
        body="проверка",
        file_path=fayl,
        file_name="photo.jpg" if fayl else "",
        file_size=razmer,
        happened_at=kogda,
    )
    db.add(stroka)
    db.flush()
    return stroka.id


def _fayl(imya: str, soderzhimoe: bytes = b"kartinka") -> str:
    """Положить файл в каталог нашего диалога и вернуть относительный путь."""
    koren = get_settings().storage_dir / "telegram" / str(NASH_CHAT)
    koren.mkdir(parents=True, exist_ok=True)
    (koren / imya).write_bytes(soderzhimoe)
    return f"telegram/{NASH_CHAT}/{imya}"


def _est(db, message_id: int) -> bool:
    return telegram_repo.po_id(db, message_id) is not None


# --- умолчание ---------------------------------------------------------------


def test_po_umolchaniyu_hranim_vechno(stend):
    """Никто ничего не выбирал — уборка выключена и не удаляет ни строки.

    Это ГЛАВНАЯ проверка файла. Всё остальное описывает, как уборка работает;
    эта — что она не работает сама по себе. Переписка с клиентом бывает
    доказательством, и включиться уборка обязана только по названному сроку.
    """
    with _sessiya() as db:
        drevnee = _polozhit(db, stend, datetime(2019, 1, 1, 12, 0, 0))
        db.commit()

    with _sessiya() as db:
        assert telegram_uborka.srok(db) == telegram_uborka.BEZ_UBORKI
        itog = telegram_uborka.ubrat(db)

    assert itog["status"] == "off", "уборка пошла, хотя срок никто не называл"
    assert itog["messages"] == 0

    with _sessiya() as db:
        assert _est(db, drevnee), "переписка 2019 года исчезла при невыбранном сроке"


def test_vykluchennaya_uborka_ne_zatiraet_sled_progona(stend):
    """Прогон при выключенной уборке не стирает отчёт о прошлой настоящей.

    След прогона — единственное место, где владелец читает, что и когда
    убиралось. Затирать его пустышкой каждую ночь после того, как уборку
    выключили, значило бы терять ответ на вопрос «а что вы удалили тогда».
    """
    with _sessiya() as db:
        settings_repo.write(db, telegram_uborka.SETTING_SROK, "12")
        db.commit()
    with _sessiya() as db:
        _polozhit(db, stend, datetime(2019, 1, 1, 12, 0, 0))
        db.commit()
    with _sessiya() as db:
        telegram_uborka.ubrat(db)

    with _sessiya() as db:
        byl = telegram_uborka.posledniy_progon(db)
        assert byl is not None

    with _sessiya() as db:
        telegram_uborka.zadat_srok(db, 0)
        db.commit()
    with _sessiya() as db:
        telegram_uborka.ubrat(db)

    with _sessiya() as db:
        stal = telegram_uborka.posledniy_progon(db)
    assert stal == byl, "выключенная уборка затёрла отчёт о настоящем прогоне"


def test_nepodhodyashchiy_srok_otvergaetsya_ponyatno():
    """Срок берётся из списка, а не набирается числом.

    Список нужен потому, что экран показывает возле каждого срока, сколько он
    удалит. Свободное число означало бы выбор вслепую — ровно то, чего эта
    затея не допускает.
    """
    for horoshee, ozhidaem in (("", 0), ("0", 0), (None, 0), ("12", 12), (36, 36)):
        assert telegram_uborka.proverit_srok(horoshee) == ozhidaem

    for plohoe in ("7", "-12", "год", "12.5", "1200"):
        with pytest.raises(errors.ValidationError) as beda:
            telegram_uborka.proverit_srok(plohoe)
        assert beda.value.code == "bad_retention"


def test_neponyatnyy_srok_v_baze_chitaetsya_kak_vechno(stend):
    """Мусор в настройке означает «хранить вечно», а не удаление по случайности.

    Настройки правят руками при переносах, и опечатка в сроке не должна ни
    ронять экран, ни — тем более — обернуться уборкой по выдуманному числу.
    """
    with _sessiya() as db:
        settings_repo.write(db, telegram_uborka.SETTING_SROK, "полгода")
        db.commit()
    with _sessiya() as db:
        assert telegram_uborka.srok(db) == telegram_uborka.BEZ_UBORKI
        assert telegram_uborka.ubrat(db)["status"] == "off"


# --- граница -----------------------------------------------------------------


def test_granitsa_schitaetsya_kalendaryom_a_ne_tridtsatyu_sutkami():
    """Двенадцать месяцев — это год, а не триста шестьдесят суток.

    Разница в пять дней, и это те самые пять дней, за которые убранное
    расходится с обещанным.
    """
    ot = datetime(2026, 8, 18, 4, 30, 0)
    assert telegram_uborka.granitsa(12, ot) == datetime(2025, 8, 18, 4, 30, 0)
    assert telegram_uborka.granitsa(3, ot) == datetime(2026, 5, 18, 4, 30, 0)
    assert telegram_uborka.granitsa(36, ot) == datetime(2023, 8, 18, 4, 30, 0)
    assert telegram_uborka.granitsa(0, ot) is None

    # День подрезается по длине месяца: 31 марта минус месяц — конец февраля.
    assert telegram_uborka.granitsa(1, datetime(2026, 3, 31, 9, 0, 0)) == datetime(
        2026, 2, 28, 9, 0, 0
    )


def test_uhodit_tolko_starshe_granitsy(stend):
    """Старое уходит, свежее остаётся, а само стоящее на границе — остаётся.

    Граница закрыта справа намеренно: «хранить 12 месяцев» означает, что всё за
    последние двенадцать месяцев на месте. Сообщение ровно на границе входит в
    обещанное.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    do = telegram_uborka.granitsa(12, seychas)

    with _sessiya() as db:
        drevnee = _polozhit(db, stend, do - timedelta(days=40))
        na_granitse = _polozhit(db, stend, do)
        svezhee = _polozhit(db, stend, seychas - timedelta(days=3))
        db.commit()

    with _sessiya() as db:
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        itog = telegram_uborka.ubrat(db, seychas=seychas)

    assert itog["status"] == "done"
    assert itog["months"] == 12
    with _sessiya() as db:
        assert not _est(db, drevnee), "сообщение старше границы осталось"
        assert _est(db, na_granitse), "сообщение РОВНО на границе удалено — обещали хранить"
        assert _est(db, svezhee), "удалено свежее сообщение"


def test_dialog_i_privyazka_perezhivayut_uborku(stend):
    """Опустевший диалог остаётся — вместе с именем, меткой и привязкой.

    Диалог помнит работу человека: чей он, откуда пришёл клиент, какой у него
    номер. Снести его вместе с последним сообщением значило бы стереть то, чего
    удалять не просили, — и восстановить это было бы неоткуда.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    with _sessiya() as db:
        dialog = telegram_repo.get_chat(db, stend)
        dialog.source = "naklejka"
        _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0))
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        telegram_uborka.ubrat(db, seychas=seychas)

    with _sessiya() as db:
        dialog = telegram_repo.get_chat(db, stend)
        assert dialog is not None, "уборка снесла диалог вместе с перепиской"
        assert dialog.source == "naklejka"
        assert telegram_repo.lenta(db, stend) == []


# --- файлы -------------------------------------------------------------------


def test_fayly_uhodyat_vmeste_so_strokami_a_avatar_ostayotsya(stend):
    """Место на диске занимают файлы — значит они и уходят. Но только они.

    Аватар собеседника лежит в том же каталоге и сообщением не является: он у
    диалога один, обновляется сам и к сроку хранения переписки отношения не
    имеет. Уборка, сносящая каталог целиком, унесла бы и его.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    koren = get_settings().storage_dir / "telegram" / str(NASH_CHAT)
    put_avatara = _fayl("avatar.jpg", b"avatarnye-bayty")
    put_starogo = _fayl("staroe.jpg", b"a" * 4096)
    put_svezhego = _fayl("svezhee.jpg", b"b" * 2048)

    with _sessiya() as db:
        dialog = telegram_repo.get_chat(db, stend)
        dialog.avatar_path = put_avatara
        _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0), fayl=put_starogo)
        _polozhit(db, stend, seychas - timedelta(days=2), fayl=put_svezhego)
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        itog = telegram_uborka.ubrat(db, seychas=seychas)

    assert itog["files"] == 1, "убран не один файл"
    assert itog["bytes"] == 4096, f"освобождённые байты посчитаны неверно: {itog['bytes']}"
    assert not (koren / "staroe.jpg").exists(), "файл старого сообщения остался на диске"
    assert (koren / "svezhee.jpg").exists(), "убран файл свежего сообщения"
    assert (koren / "avatar.jpg").exists(), "уборка унесла аватар собеседника"


def test_put_vne_kataloga_kanala_ne_stiraetsya(stend, tmp_path):
    """Строка в базе с чужим путём не превращается в стёртый файл где угодно.

    Путь пишем мы сами — и всё же проверяем. Это место стирает файлы ПО СТРОКЕ
    ИЗ БАЗЫ, а строку однажды поправят руками при переносе; цена проверки — одно
    сравнение, цена её отсутствия — стёртое что угодно на диске.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    chuzhoy = tmp_path / "chuzhoy.txt"
    chuzhoy.write_bytes("не трогать".encode())

    with _sessiya() as db:
        _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0), fayl="../../../" + chuzhoy.name)
        _polozhit(db, stend, datetime(2019, 5, 6, 10, 0, 0), fayl=str(chuzhoy))
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        itog = telegram_uborka.ubrat(db, seychas=seychas)

    assert chuzhoy.exists(), "уборка стёрла файл за пределами каталога канала"
    assert itog["messages"] == 2, "строки при этом обязаны были уйти"
    assert itog["files"] == 0


# --- лента клиента -----------------------------------------------------------


def test_zapisi_v_lente_klienta_ostayutsya(stend):
    """Уборка переписки не переписывает историю карточки клиента.

    Решение принято сознательно и противоположно первому впечатлению. Лента
    принадлежит КАРТОЧКЕ: там же лежат звонки и письма, и «прибрать мессенджер»
    не то же самое, что «стереть след разговора у клиента». Ссылки на
    `telegram_messages` в записи нет вовсе, значит после уборки ничего не
    повисает — запись самодостаточна и читается как была.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    with _sessiya() as db:
        klient = Client(name="Уборка Перепискина", phone="+380670000777")
        db.add(klient)
        db.flush()
        zapis = ClientNote(
            client_id=klient.id,
            kind="telegram",
            direction=DIRECTION_IN,
            body="клиент писал в телеграм",
            happened_at=datetime(2019, 5, 5, 10, 0, 0),
        )
        db.add(zapis)
        db.flush()
        klient_id, zapis_id = klient.id, zapis.id

        dialog = telegram_repo.get_chat(db, stend)
        dialog.client_id = klient_id
        _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0))
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    try:
        with _sessiya() as db:
            telegram_uborka.ubrat(db, seychas=seychas)

        with _sessiya() as db:
            ostalos = db.get(ClientNote, zapis_id)
            assert ostalos is not None, (
                "уборка переписки стёрла запись из ленты клиента — это другое "
                "решение, и владелец его не принимал"
            )
            assert ostalos.body == "клиент писал в телеграм"
    finally:
        with _sessiya() as db:
            zapis = db.get(ClientNote, zapis_id)
            if zapis is not None:
                db.delete(zapis)
            klient = db.get(Client, klient_id)
            if klient is not None:
                db.delete(klient)
            db.commit()


# --- прогон: пачки, время, след ----------------------------------------------


def test_progon_ogranichen_po_vremeni_i_dodelyvaetsya_sleduyushchim(stend):
    """Прогон уперся в отпущенное время — недоделанное достаётся следующему.

    Пачки берутся с самого старого края, поэтому каждый прогон продвигает
    границу вперёд, а не топчется по всей таблице. Без этого свойства обрыв
    посреди работы означал бы, что уборка не кончится никогда.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    with _sessiya() as db:
        for den in range(5):
            _polozhit(db, stend, datetime(2019, 5, 1 + den, 10, 0, 0))
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        pervyy = telegram_uborka.ubrat(db, seychas=seychas, pachka=2, predel_sekund=0)

    assert pervyy["messages"] == 2, "прогон с нулевым запасом времени взял не одну пачку"
    assert pervyy["done"] is False
    assert pervyy["status"] == "partial"

    with _sessiya() as db:
        ostalos = telegram_repo.lenta(db, stend)
        assert len(ostalos) == 3
        # Ушли САМЫЕ СТАРЫЕ: граница за прогон продвинулась вперёд.
        assert min(s.happened_at for s in ostalos) == datetime(2019, 5, 3, 10, 0, 0)

    with _sessiya() as db:
        vtoroy = telegram_uborka.ubrat(db, seychas=seychas, pachka=2, predel_sekund=30)

    assert vtoroy["messages"] == 3
    assert vtoroy["done"] is True
    with _sessiya() as db:
        assert telegram_repo.lenta(db, stend) == []


def test_progon_ostavlyaet_sled_chislami(stend):
    """Каждый прогон отчитывается: когда, за какую границу, сколько и сколько места.

    Молча удалённая переписка — ровно то, чего эта затея не допускает. След
    лежит в настройке и показывается на том же экране, где выбирали срок: журнал
    службы читает тот, у кого есть доступ к машине, а решение принимал владелец
    в браузере.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    put = _fayl("otchet.jpg", b"z" * 1024)
    with _sessiya() as db:
        _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0), fayl=put)
        _polozhit(db, stend, datetime(2019, 5, 6, 10, 0, 0))
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        telegram_uborka.ubrat(db, seychas=seychas)

    with _sessiya() as db:
        sled = telegram_uborka.posledniy_progon(db)

    assert sled is not None, "прогон не оставил следа — это и есть удаление молча"
    assert sled["messages"] == 2
    assert sled["files"] == 1
    assert sled["bytes"] == 1024
    assert sled["months"] == 12
    assert sled["done"] is True
    assert sled["at"].startswith("2026-08-18")
    assert sled["cutoff"].startswith("2025-08-18")


def test_sukhoy_progon_schitaet_i_ne_udalyaet(stend):
    """«Сколько уйдёт» отвечают, ничего не тронув.

    Сухой прогон — то, чем пользуются перед первым включением уборки: он даёт
    число, а число и есть довод.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    with _sessiya() as db:
        staroe = _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0))
        telegram_uborka.zadat_srok(db, 12)
        db.commit()

    with _sessiya() as db:
        itog = telegram_uborka.ubrat(db, seychas=seychas, na_sukho=True)

    assert itog["status"] == "dry"
    assert itog["messages"] >= 1
    with _sessiya() as db:
        assert _est(db, staroe), "сухой прогон удалил сообщение"
        assert telegram_uborka.posledniy_progon(db) is None, "сухой прогон оставил след прогона"


# --- цифры для экрана --------------------------------------------------------


def test_ves_perepiski_schitaetsya_odnim_zaprosom_na_vse_sroki(stend):
    """Вес переписки и цена каждого срока приезжают одним запросом на MySQL.

    Проверка кажется пустой и не такова: условная сумма (`CASE WHEN`) по пяти
    срокам в одном запросе — ровно то место, которое на другом движке пишется
    иначе, а собранное неверно падает не при сборке, а при выполнении, то есть
    на экране настроек у владельца.
    """
    seychas = datetime(2026, 8, 18, 4, 30, 0)
    put = _fayl("ves.jpg", b"y" * 3072)
    with _sessiya() as db:
        # Одно старше трёх лет, одно между годом и тремя, одно свежее.
        _polozhit(db, stend, datetime(2019, 5, 5, 10, 0, 0), fayl=put)
        _polozhit(db, stend, seychas - timedelta(days=400))
        _polozhit(db, stend, seychas - timedelta(days=2))
        db.commit()

    with _sessiya() as db:
        itog = telegram_uborka.ves(db, seychas=seychas)
        nashi = telegram_repo.lenta(db, stend)

    assert itog["messages"] >= 3
    assert itog["files"] >= 1
    assert itog["file_bytes"] >= 3072
    assert itog["oldest_at"] is not None
    assert itog["oldest_at"] <= datetime(2019, 5, 5, 10, 0, 0)
    assert isinstance(itog["disk_bytes"], int)

    po_srokam = itog["po_srokam"]
    assert set(po_srokam) == set(telegram_uborka.SROKI), "сроки разъехались со службой"
    # Чем длиннее срок, тем меньше под него попадает: свойство, ломающееся молча
    # при перепутанном знаке сравнения.
    poryadok = [po_srokam[m]["messages"] for m in sorted(po_srokam)]
    assert poryadok == sorted(poryadok, reverse=True), f"счёт по срокам не убывает: {poryadok}"
    # Наши три строки видны в числах: под 36 месяцев попадает одна, под 12 — две.
    assert po_srokam[36]["messages"] >= 1
    assert po_srokam[36]["bytes"] >= 3072
    assert po_srokam[12]["messages"] >= 2
    assert len(nashi) == 3
