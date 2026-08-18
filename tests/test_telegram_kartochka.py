"""Карточка клиента заводится и дополняется прямо из переписки.

**Зачем это вообще.** До этих ручек менеджер, которому написал незнакомый
человек, уходил в раздел клиентов, заводил карточку руками, возвращался в
мессенджер и выбирал её в выпадающем списке. Три перехода посреди разговора —
столько не делают: карточку заводят «потом», а «потом» не наступает. Переписка
при этом остаётся ничьей, и всё, ради чего канал внесли в CRM — лента клиента,
заявка из диалога, история общения, — не работает вовсе.

**Чего здесь боятся.** Ровно двух вещей, и они тянут в разные стороны.

Первая — размножение карточек. Кнопка «завести» под рукой у каждого разговора
означает, что одному человеку заведут вторую, третью и пятую: с телефона, с
почты, из телеграма. От этого страдает всякая CRM, и лечится оно только тем,
что второй карточке неоткуда взяться.

Вторая — потеря внесённого руками. «Обновить из телеграма» звучит как
«дополнить», а сделать может «затереть»: в карточке лежит имя с отчеством и
номер, продиктованный по телефону, а в телеграме — самоназвание, которое
человек меняет когда вздумается.

Проверки ниже держат обе границы: заведение — одним действием и ровно один раз,
перенос — только в пустое поле.

Номера телеграм-чатов здесь начинаются с 530000: база у набора одна на весь
прогон, и диалог, заведённый соседней проверкой, остаётся в ней. Про то, чем
кончается общий номер, написано в `tests/test_telegram.py`.
"""

from tests.conftest import API

# Приспособления канала переиспользуются, а не переписываются: приём вебхука
# устроен так, что «послать сообщение боту» — это секрет в заголовке, форма
# обновления телеграма и разбор ответа. Своя копия этих двадцати строк разошлась
# бы с оригиналом на первой же правке приёма.
from tests.test_telegram import (  # noqa: F401 — фикстуры берутся по имени
    TG,
    _dialog,
    _obnovlenie,
    _poslat,
    bot_nastroen,
    telegram_on,
)


def _napisal(root_client, sekret, chat_id: int, imya: str, **pole) -> dict:
    """Незнакомый человек написал боту. Возвращает строку его диалога.

    Имя задаётся проверкой, а не берётся общее: карточки здесь заводятся
    настоящие и остаются в базе до конца прогона, а «сколько карточек с таким
    именем» — самый прямой способ показать, что вторая не завелась.
    """
    telo = _obnovlenie(chat_id, 1, **pole)
    telo["message"]["from"]["first_name"] = imya
    otvet = _poslat(root_client, sekret, telo)
    assert otvet.status_code == 200, otvet.text
    return _dialog(root_client, chat_id)


def _podelilsya_nomerom(root_client, sekret, chat_id: int, imya: str, nomer: str) -> dict:
    """Клиент нажал «поделиться контактом» — единственная точная примета.

    Именно кнопкой, а не числом в тексте: «позвоните маме на 067…» — это не его
    телефон, и привязка по такому номеру и есть тот самый оплаченный урок про
    чужую карточку.
    """
    telo = _obnovlenie(chat_id, 1, text="")
    telo["message"]["from"]["first_name"] = imya
    telo["message"]["contact"] = {"phone_number": nomer, "first_name": imya}
    otvet = _poslat(root_client, sekret, telo)
    assert otvet.status_code == 200, otvet.text
    return _dialog(root_client, chat_id)


# --- завести ------------------------------------------------------------------


def test_kartochka_zavoditsya_i_dialog_privyazyvaetsya_odnim_deystviem(
    root_client, bot_nastroen
):
    """Одно нажатие — есть карточка И диалог к ней привязан.

    Разделить эти два шага нельзя, и это главное в проверке. Заведи ручка
    карточку, не привязав диалог, — и менеджер получил бы ровно ту работу, от
    которой уходили: вернуться в список и выбрать её руками. Хуже того, вторым
    нажатием он завёл бы вторую такую же, потому что первая «не сработала».

    Заодно проверяется, что в карточку попало то, по чему потом видно, откуда
    взялся клиент: логин собеседника и источник «telegram». Без них через месяц
    на вопрос «а этот откуда» не ответит никто.
    """
    dialog = _napisal(
        root_client, bot_nastroen, 530100, "Первый из диалога", text="почём ремонт?"
    )
    assert dialog["client_id"] is None, "диалог привязался сам — так нельзя"

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/client")
    assert otvet.status_code == 201, otvet.text
    telo = otvet.json()
    assert telo["created"] is True
    assert telo["name"] == "Первый из диалога"
    assert telo["messenger"] == "@petr", "логин собеседника не попал в карточку"
    assert telo["source"] == "telegram", "по карточке не видно, откуда пришёл клиент"

    assert _dialog(root_client, 530100)["client_id"] == telo["id"], (
        "карточка завелась, а диалог остался ничьим — привязывать пришлось бы руками"
    )
    # Карточка настоящая, а не запись в чужой таблице: она открывается в разделе
    # клиентов и живёт там по общим правилам.
    v_razdele = root_client.get(f"{API}/clients/{telo['id']}")
    assert v_razdele.status_code == 200, v_razdele.text
    assert v_razdele.json()["name"] == "Первый из диалога"


def test_metka_ssylki_stanovitsya_istochnikom_klienta(root_client, bot_nastroen):
    """Пришёл по ссылке с меткой — метка и есть ответ «откуда».

    `t.me/бот?start=naklejka` отвечает на тот же вопрос, что и поле «источник» в
    карточке, и отвечает точнее слова «телеграм»: наклейка на квитанции и кнопка
    на сайте — разные каналы, и владелец заводит метки именно затем, чтобы их
    различать. Потеряй мы метку здесь — отчёт по источникам показал бы один
    сплошной «telegram», то есть ровно то, чего метки должны были избежать.
    """
    dialog = _napisal(
        root_client, bot_nastroen, 530200, "Пришёл с наклейки", text="/start naklejka"
    )
    assert dialog["source"] == "naklejka"

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/client")
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["source"] == "naklejka", "метка ссылки потерялась по дороге"


def test_vtoraya_kartochka_tomu_zhe_dialogu_otvergaetsya(root_client, bot_nastroen):
    """Диалог уже привязан — отказ, а не вторая карточка тому же человеку.

    Это и есть размножение клиентов, от которого страдают все CRM: две карточки
    одного человека делят его историю пополам, и ни одна из половин не отвечает
    на вопрос «что у нас с ним было». Кнопка под рукой у каждого разговора — как
    раз тот случай, когда нажмут дважды: первый раз не заметили, второй раз для
    верности.

    Отказ обязан быть узнаваемым кодом, а не общей четырёхсоткой: по нему экран
    скажет «карточка уже есть», а не «что-то пошло не так».
    """
    dialog = _napisal(root_client, bot_nastroen, 530300, "Неразмножаемый", text="здравствуйте")

    pervaya = root_client.post(f"{TG}/chats/{dialog['id']}/client")
    assert pervaya.status_code == 201, pervaya.text

    vtoraya = root_client.post(f"{TG}/chats/{dialog['id']}/client")
    assert vtoraya.status_code == 409, vtoraya.text
    assert vtoraya.json()["error"]["code"] == "telegram_chat_already_linked"

    assert _dialog(root_client, 530300)["client_id"] == pervaya.json()["id"], (
        "второе нажатие переставило привязку"
    )
    nayd = root_client.get(f"{API}/clients?search=Неразмножаемый").json()["items"]
    assert len(nayd) == 1, f"карточек с этим именем стало {len(nayd)}: {nayd}"


def test_pri_sovpavshem_nomere_privyazyvaemsya_a_ne_zavodim_dubl(root_client, bot_nastroen):
    """Номер уже стоит в чьей-то карточке — привязываем к ней, а не заводим вторую.

    Порядок событий здесь житейский: человек поделился контактом, когда его
    карточки ещё не было, а завели её через час с другого конца — из звонка или
    руками. Нажми менеджер после этого «завести карточку», и без проверки в базе
    оказались бы двое с одним телефоном.

    Решать это молча можно ровно по одной примете — точному совпадению
    нормализованного номера, которым клиент поделился сам. Совпадение имени
    по-прежнему не значит ничего: в заказах привязка по частичному имени уводила
    деньги и товар на чужую карточку.
    """
    dialog = _podelilsya_nomerom(
        root_client, bot_nastroen, 530400, "С номером наперёд", "+380675304001"
    )
    assert dialog["client_id"] is None, "привязалось к карточке, которой ещё нет"

    svoya = root_client.post(
        f"{API}/clients", json={"name": "Заведён отдельно", "phone": "+380675304001"}
    ).json()

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/client")
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["created"] is False, "завели дубль тому, кто уже есть в базе"
    assert otvet.json()["id"] == svoya["id"]
    assert _dialog(root_client, 530400)["client_id"] == svoya["id"]


# --- обновить -----------------------------------------------------------------


def test_telefon_iz_telegrama_perenositsya_v_pustoe_pole(root_client, bot_nastroen):
    """Клиент поделился номером — «Обновить из телеграма» кладёт его в карточку.

    Ради этого половина кнопки и существует. Карточку часто заводят раньше
    переписки и без телефона: имя записали, а номер спросить забыли. Клиент
    потом делится им сам, одним нажатием в телеграме, — и без переноса этот
    номер остаётся лежать в переписке, где его не видит ни звонилка, ни поиск.

    Поэтому проверяется не только поле в ответе, но и то, что карточка стала
    НАХОДИТЬСЯ по номеру: перенос мимо `phone_norm` означал бы, что звонок с
    этого телефона по-прежнему приходит мимо карточки.
    """
    dialog = _podelilsya_nomerom(
        root_client, bot_nastroen, 530500, "Дополним номером", "+380675305001"
    )
    klient = root_client.post(
        f"{API}/clients", json={"name": "Заведён без номера"}
    ).json()
    privyazka = root_client.patch(
        f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]}
    )
    assert privyazka.status_code == 200, privyazka.text

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/client/refresh")
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["phone"] == "+380675305001"
    assert "phone" in otvet.json()["updated"], otvet.text

    v_kartochke = root_client.get(f"{API}/clients/{klient['id']}").json()
    assert v_kartochke["phone"] == "+380675305001", "ответ ручки красив, а в базе пусто"

    po_nomeru = root_client.get(f"{API}/clients?search=380675305001").json()["items"]
    assert [k["id"] for k in po_nomeru] == [klient["id"]], (
        "карточка не находится по перенесённому номеру — значит `phone_norm` не "
        "пересчитан, и звонок с этого телефона придёт мимо неё"
    )


def test_nepustoe_pole_kartochki_ne_zatiraetsya(root_client, bot_nastroen):
    """Кнопка дополняет, а не переписывает: занятое поле остаётся как есть.

    Самая дорогая ошибка этой затеи выглядела бы разумно: «телеграм свежее,
    значит он и прав». В карточке лежит внесённое руками — имя с отчеством,
    номер, продиктованный по телефону, уточнённый логин. В телеграме лежит
    самоназвание, которое человек меняет когда вздумается, и второй номер,
    которым он поделился с другого телефона.

    Перенос поверх непустого означал бы, что смена ника у клиента переименовывает
    карточку в CRM — молча и ровно в ту секунду, когда менеджер нажал «обновить»,
    думая, что дополняет. Отсюда следствие, которое проверяется отдельно: имя
    карточки этой кнопкой не меняется никогда, потому что пустым оно не бывает.
    """
    dialog = _podelilsya_nomerom(
        root_client, bot_nastroen, 530600, "🔥Ваня🔥", "+380675306001"
    )
    klient = root_client.post(
        f"{API}/clients",
        json={
            "name": "Иван Петрович Сидоров",
            "phone": "+380445556677",
            "messenger": "@svoy_login",
        },
    ).json()
    root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]})

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/client/refresh")
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    assert telo["phone"] == "+380445556677", "номер из телеграма затёр внесённый руками"
    assert telo["name"] == "Иван Петрович Сидоров", "имя карточки переписано ником из телеграма"
    assert telo["messenger"] == "@svoy_login", "логин из телеграма затёр уточнённый"
    assert "phone" not in telo["updated"] and "name" not in telo["updated"], telo["updated"]

    v_kartochke = root_client.get(f"{API}/clients/{klient['id']}").json()
    assert v_kartochke["name"] == "Иван Петрович Сидоров"
    assert v_kartochke["phone"] == "+380445556677"


def test_obnovlenie_nichego_ne_menyaet_vtorym_nazhatiem(root_client, bot_nastroen):
    """Нажали дважды — второй раз переносить нечего, и это не отказ.

    Кнопку нажимают повторно всегда: не поняли, сработало ли, или просто на
    всякий случай. Ответь ручка отказом — человек решил бы, что первое нажатие
    тоже не сработало, и пошёл бы править карточку руками. Поэтому пустой список
    перенесённого законен и означает ровно «всё уже на месте».
    """
    dialog = _podelilsya_nomerom(
        root_client, bot_nastroen, 530700, "Дважды", "+380675307001"
    )
    klient = root_client.post(f"{API}/clients", json={"name": "Дважды нажатый"}).json()
    root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]})

    pervoe = root_client.post(f"{TG}/chats/{dialog['id']}/client/refresh")
    assert pervoe.status_code == 200, pervoe.text
    assert pervoe.json()["updated"], "первое нажатие ничего не перенесло"

    vtoroe = root_client.post(f"{TG}/chats/{dialog['id']}/client/refresh")
    assert vtoroe.status_code == 200, vtoroe.text
    assert vtoroe.json()["updated"] == [], vtoroe.text
    assert vtoroe.json()["phone"] == pervoe.json()["phone"]


def test_obnovlenie_nepriviazannogo_dialoga_govorit_pochemu(root_client, bot_nastroen):
    """Диалог ничей — обновлять нечего, и отказ это объясняет.

    Тем же кодом, что и заявка из непривязанного диалога: беда одна и та же —
    неизвестно, чью карточку править, — и два разных слова про одно означали бы,
    что экран учит их порознь.
    """
    dialog = _napisal(root_client, bot_nastroen, 530800, "Ничей", text="а это кто")

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/client/refresh")
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_chat_not_linked"


# --- границы ------------------------------------------------------------------


def test_nesushchestvuyushchiy_dialog_otvergaetsya(root_client, bot_nastroen):
    """Номера диалога нет — 404, а не карточка неизвестно кому.

    Номер приходит из адреса, то есть его называет запрос, а не мы. Промахнись
    ручка мимо проверки — и на любой набранный номер завелась бы карточка
    «Telegram 0», привязанная в никуда; чинить это пришлось бы вычищая мусор
    руками. Обе ручки проверяются одинаково: границу легко поставить в одной и
    забыть во второй.
    """
    for adres in ("client", "client/refresh"):
        otvet = root_client.post(f"{TG}/chats/999999/{adres}")
        assert otvet.status_code == 404, f"{adres}: {otvet.text}"
        assert otvet.json()["error"]["code"] == "telegram_chat_not_found", adres


def test_vyklyuchennyy_blok_zakryvaet_obe_ruchki(root_client, bot_nastroen):
    """Выключили блок — новых ручек нет, как и всего раздела.

    Правило проекта: выключенный блок исчезает целиком, а не «в основном».
    Спрятать пункт меню недостаточно — адрес остаётся рабочим, его помнит
    браузер и он лежит в закладках. Новая ручка попадает под то же правило, и
    проверяется это здесь, потому что забывают ровно про свежие.
    """
    dialog = _napisal(root_client, bot_nastroen, 530900, "Пока выключено", text="привет")

    otklyuchen = root_client.post(f"{API}/modules/telegram", json={"enabled": False})
    assert otklyuchen.status_code == 200, otklyuchen.text
    try:
        for adres in ("client", "client/refresh"):
            otvet = root_client.post(f"{TG}/chats/{dialog['id']}/{adres}")
            assert otvet.status_code == 403, f"{adres}: {otvet.status_code}"
            assert otvet.json()["error"]["code"] == "module_disabled", adres
    finally:
        # Обратно в любом случае: иначе упавшая проверка гасит канал соседним.
        root_client.post(f"{API}/modules/telegram", json={"enabled": True})
