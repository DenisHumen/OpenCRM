"""Счёт непрочитанного: почему он обязан быть по-диаложным.

Проверки здесь идут уровнем ниже HTTP — прямо к `telegram_repo.neprochitannye`.
Так нарочно: беда, ради которой файл заведён, живёт в ОДНОЙ строке отбора, и
через ручку списка её видно только как «значок показывает не то». Разбирать
такое приходится по вычитанию, а через репозиторий условие проверяется в лоб.

**Беда, которую эти проверки стерегут.** Граница «прочитано» приходит из Redis
и по каждому диалогу своя, а у диалога, который никогда не открывали (или у
которого ключ истёк — срок месяц), её нет вовсе. По замыслу отсутствие границы
означает «непрочитано ВСЁ входящее». Прежняя редакция отбирала строки одним
общим порогом (`min` по всем границам), и диалог без границы отсеивался целиком:
клиент написал пять сообщений, никто их не открывал, а значок показывал ноль.
Спрятанное сообщение клиента — худшая ошибка этого счётчика: ошибиться в сторону
«посмотри ещё раз» безвредно, в обратную — значит потерять разговор.

Номера телеграм-чатов взяты с запасом от занятых в `tests/test_telegram.py`
(там дело доходит до 512100): диалоги здесь заводятся напрямую, а `chat_id`
уникален на всю таблицу.
"""

from datetime import datetime, timedelta

from database.models.telegram import DIRECTION_IN, DIRECTION_OUT
from database.repositories import telegram as telegram_repo

#: Время событий. Одно на файл: счёт непрочитанного идёт по идентификаторам, а
#: не по времени, и разное время только запутывало бы разбор.
NACHALO = datetime(2026, 8, 16, 12, 0)


def _dialog(db, chat_id: int, nazvanie: str):
    """Диалог напрямую, без вебхука: проверяем счёт, а не приём сообщений."""
    return telegram_repo.create_chat(db, chat_id=chat_id, title=nazvanie)


def _soobshchenie(db, dialog, napravlenie: str, nomer: int, tekst: str = ""):
    """Сообщение в диалоге. Возвращает запись — её идентификатор и есть граница."""
    return telegram_repo.dobavit_soobshchenie(
        db,
        chat_id=dialog.id,
        direction=napravlenie,
        body=tekst or f"строка {nomer}",
        happened_at=NACHALO + timedelta(minutes=nomer),
    )


def test_dialog_bez_granitsy_pokazyvaet_vsyo_vhodyashchee(db):
    """Диалог, который не открывали, показывает ВСЕ свои входящие.

    Главная проверка файла, и она про спрятанные сообщения клиента. Соседний
    диалог открыли только что, поэтому его граница — свежий, то есть большой,
    идентификатор. Прежняя редакция брала общий порог по всем границам, и все
    сообщения нетронутого диалога оказывались НИЖЕ него: значок показывал ноль
    там, где клиента ждут пять непрочитанных фраз.

    Порядок заведения здесь существенный: сначала нетронутый диалог, потом
    открытый. Заведи мы их наоборот — идентификаторы оказались бы выше границы
    сами собой, и проверка зеленела бы на сломанном отборе.
    """
    zabytyy = _dialog(db, 520100, "Клиент, которого не открывали")
    for nomer in range(1, 6):
        _soobshchenie(db, zabytyy, DIRECTION_IN, nomer)

    otkrytyy = _dialog(db, 520110, "Диалог, открытый только что")
    _soobshchenie(db, otkrytyy, DIRECTION_IN, 10)
    poslednee = _soobshchenie(db, otkrytyy, DIRECTION_IN, 11)

    # Менеджер дочитал открытый диалог до конца — граница у него свежая и
    # большая. У забытого границы нет вовсе.
    itog = telegram_repo.neprochitannye(
        db, [zabytyy.id, otkrytyy.id], {otkrytyy.id: poslednee.id}
    )

    assert itog[zabytyy.id] == 5, (
        "диалог без границы показал не всё входящее — свежая граница соседа "
        f"отсеяла сообщения клиента: {itog}"
    )
    assert itog[otkrytyy.id] == 0, "дочитанный до конца диалог показал непрочитанное"


def test_dialog_s_granitsey_schitaet_tolko_prishedshee_posle(db):
    """Граница есть — считается только то, что пришло после неё.

    Парная к предыдущей и без неё опасная: починка «нет границы — считаем всё»
    зеленела бы и у счётчика, который считает всё ВСЕГДА, то есть показывает
    одно и то же число независимо от чтения. Такой значок перестают замечать
    вместе с настоящими сообщениями.
    """
    dialog = _dialog(db, 520200, "Дочитан до середины")
    pervoe = _soobshchenie(db, dialog, DIRECTION_IN, 1)
    _soobshchenie(db, dialog, DIRECTION_IN, 2)
    _soobshchenie(db, dialog, DIRECTION_IN, 3)

    itog = telegram_repo.neprochitannye(db, [dialog.id], {dialog.id: pervoe.id})

    assert itog[dialog.id] == 2, f"счёт идёт не от границы, а от начала диалога: {itog}"


def test_svoi_ishodyashchie_ne_schitayutsya_nikogda(db):
    """Собственные ответы непрочитанными не бывают — ни с границей, ни без неё.

    Проверяется именно диалог БЕЗ границы: там условие по направлению остаётся
    единственным отсевом, и потеряй его починка — значок загорался бы после
    каждого своего же ответа. Значок, который горит от собственных действий,
    перестают замечать.
    """
    dialog = _dialog(db, 520300, "Отвечали много")
    _soobshchenie(db, dialog, DIRECTION_IN, 1, "вопрос клиента")
    _soobshchenie(db, dialog, DIRECTION_OUT, 2, "наш ответ")
    _soobshchenie(db, dialog, DIRECTION_OUT, 3, "и ещё наш")
    _soobshchenie(db, dialog, DIRECTION_IN, 4, "второй вопрос")
    _soobshchenie(db, dialog, DIRECTION_OUT, 5, "и снова наш")

    itog = telegram_repo.neprochitannye(db, [dialog.id], {})

    assert itog[dialog.id] == 2, f"в непрочитанное попали собственные ответы: {itog}"


def test_pustoy_spisok_dialogov_ne_hodit_v_bazu():
    """Страница без диалогов не стоит ни одного запроса.

    Пустой список приходит буднично: отбор по метке, поиск, последняя страница.
    Без раннего выхода получился бы запрос с условием `IN ()` — то есть либо
    заведомо пустая выборка, либо, при неудачной сборке условия, проход по всей
    таблице сообщений. Обходится это одной строкой, и стеречь её надо, потому
    что убирается она незаметно.

    Сессия здесь подставная и кусачая: настоящая ответила бы на запрос молча, и
    проверка ничего бы не заметила.
    """

    class ZapreshchyonnayaBaza:
        def __getattr__(self, imya):
            raise AssertionError(
                f"пустой список диалогов полез в базу: db.{imya}(...) — "
                "запрос без диалогов не нужен вовсе"
            )

    assert telegram_repo.neprochitannye(ZapreshchyonnayaBaza(), [], {}) == {}


def test_dialog_bez_vhodyashchih_otvechaet_nulyom_a_ne_propadaet(db):
    """Диалог, где входящих нет, отвечает нулём и остаётся в ответе.

    Счёт идёт группировкой, а она не отдаёт строку по диалогу, где считать
    нечего. Спиши мы ответ прямо с неё — строка списка осталась бы вовсе без
    числа, и разбираться пришлось бы уже на фронте: «нет ключа» и «ноль» там
    выглядят по-разному, и второй раз это чинят в другом месте.

    Рядом заведён диалог с настоящим входящим: без него группировка вернула бы
    пусто целиком, и проверка зеленела бы на счёте, который не работает вовсе.
    """
    tikhiy = _dialog(db, 520400, "Только наши слова")
    _soobshchenie(db, tikhiy, DIRECTION_OUT, 1, "мы написали первыми")

    zhivoy = _dialog(db, 520410, "Обычный диалог")
    _soobshchenie(db, zhivoy, DIRECTION_IN, 2)

    itog = telegram_repo.neprochitannye(db, [tikhiy.id, zhivoy.id], {})

    assert tikhiy.id in itog, f"диалог без входящих пропал из ответа: {itog}"
    assert itog[tikhiy.id] == 0
    assert itog[zhivoy.id] == 1, "счёт не работает вовсе — проверка выше ничего не значит"


def test_schyot_ne_vyhodit_za_predely_svoey_stranitsy(db):
    """В ответе только диалоги страницы — даже если границ пришло больше.

    Условие отбора собирается из пар «диалог + его граница», и лишняя пара — это
    лишний диалог в выборке и лишний диапазон в запросе. Границы приходят из
    Redis по списку страницы, но список этот меняется живьём, и надеяться, что
    в словаре никогда не окажется постороннего, — ровно тот способ рассуждать,
    которым тихие беды и заводятся.
    """
    svoy = _dialog(db, 520500, "Диалог этой страницы")
    _soobshchenie(db, svoy, DIRECTION_IN, 1)

    chuzhoy = _dialog(db, 520510, "Диалог соседней страницы")
    _soobshchenie(db, chuzhoy, DIRECTION_IN, 2)
    _soobshchenie(db, chuzhoy, DIRECTION_IN, 3)

    itog = telegram_repo.neprochitannye(db, [svoy.id], {chuzhoy.id: 0})

    assert set(itog) == {svoy.id}, f"в ответ попал диалог не с этой страницы: {itog}"
    assert itog[svoy.id] == 1
