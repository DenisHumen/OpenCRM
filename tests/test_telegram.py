"""Бот фирмы: подключение и его секреты.

Канал общения с клиентами настраивается в интерфейсе, а не в файле на сервере:
владелец заводит бота у @BotFather сам и меняет токен при отзыве, не спрашивая
того, у кого есть доступ к машине.

Отсюда главная опасность этого экрана и главная проверка здесь: **токен не
должен уезжать наружу**. Ответ ручки попадает в браузер, в историю запросов и в
отладчик — оттуда его достать проще, чем из базы.
"""

import pytest

from tests.conftest import API

TG = f"{API}/telegram"

#: Форма настоящего токена: цифры, двоеточие, буквенно-цифровой хвост.
OBRAZETS_TOKENA = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


@pytest.fixture()
def telegram_on(root_client):
    """Блок выключен по умолчанию — включаем на время проверок и гасим после."""
    vklyuchen = root_client.post(f"{API}/modules/telegram", json={"enabled": True})
    assert vklyuchen.status_code == 200, vklyuchen.text
    yield
    root_client.delete(f"{TG}/settings")
    root_client.post(f"{API}/modules/telegram", json={"enabled": False})


def test_token_ne_uezzhaet_naruzhu(root_client, telegram_on):
    """Записали токен — обратно приходит хвост, а не токен.

    Проверка буквальная: ищем сам токен во ВСЁМ теле ответа, а не в поле, где
    его быть не должно. Поле можно переименовать, а утечка останется.
    """
    zapis = root_client.put(f"{TG}/settings", json={"token": OBRAZETS_TOKENA})
    assert zapis.status_code == 200, zapis.text

    for otvet in (zapis, root_client.get(f"{TG}/settings")):
        assert OBRAZETS_TOKENA not in otvet.text, (
            "токен бота уехал в ответ ручки — оттуда он попадёт в историю "
            "браузера и в отладчик"
        )
        telo = otvet.json()
        assert telo["configured"] is True
        assert telo["token_tail"] == OBRAZETS_TOKENA[-4:], "хвост нужен, чтобы узнать токен"


def test_sekret_vebkhuka_zavoditsya_sam(root_client, telegram_on):
    """Секрет приёма не спрашивается у человека, а генерируется.

    Спросить — значит получить «12345» и открыть приём всякому, кто угадает
    адрес. Это единственное, чем приём отличает настоящий телеграм от чужого.
    """
    root_client.put(f"{TG}/settings", json={"token": OBRAZETS_TOKENA})
    telo = root_client.get(f"{TG}/settings").json()
    assert telo["webhook_secret_set"] is True, "секрет приёма не появился вместе с токеном"


def test_pustoy_token_ne_stiraet_nastroyku(root_client, telegram_on):
    """Сохранили форму, не трогая токен, — токен на месте.

    Экран показывает только хвост и вернуть настоящий токен не может. Значит
    пустое поле означает «не меняй», а не «сотри»: иначе всякое сохранение
    соседней настройки отключало бы канал.
    """
    root_client.put(f"{TG}/settings", json={"token": OBRAZETS_TOKENA})
    root_client.put(f"{TG}/settings", json={"digest_chat": "123456789"})

    telo = root_client.get(f"{TG}/settings").json()
    assert telo["configured"] is True, "сохранение соседнего поля отключило бота"
    assert telo["digest_chat"] == "123456789"


def test_krivoy_token_otvergaetsya_ponyatno(root_client, telegram_on):
    """Не токен — отказ с кодом, а не молчаливое сохранение.

    Молча сохранённая ерунда означает канал, который «настроен» и не работает,
    и разбираться в этом придётся тогда, когда клиент не дождётся ответа.
    """
    otvet = root_client.put(f"{TG}/settings", json={"token": "не токен"})
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "bad_bot_token"


def test_krivoy_chat_otvergaetsya_ponyatno(root_client, telegram_on):
    """Идентификатор чата — число. Отрицательное тоже: это группа."""
    otvet = root_client.put(f"{TG}/settings", json={"digest_chat": "@moy_kanal"})
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "bad_chat_id"

    gruppa = root_client.put(f"{TG}/settings", json={"digest_chat": "-1001234567890"})
    assert gruppa.status_code == 200, "идентификатор группы отвергнут, а он законный"


def test_otklyuchenie_snimaet_sekrety(root_client, telegram_on):
    """Отключили бота — токена и секрета нет, а настройка сводки осталась.

    Отключение — про связь, а не про данные. Тот же довод, что у выключенного
    блока системы: данные при выключении не стираются.
    """
    root_client.put(
        f"{TG}/settings", json={"token": OBRAZETS_TOKENA, "digest_chat": "123456789"}
    )
    otklyucheno = root_client.delete(f"{TG}/settings")
    assert otklyucheno.status_code == 200, otklyucheno.text

    telo = otklyucheno.json()
    assert telo["configured"] is False
    assert telo["webhook_secret_set"] is False
    assert telo["digest_chat"] == "123456789", (
        "отключение бота стёрло заодно настройку сводки — это разные вещи"
    )


def test_vyklyuchennyy_blok_zakryvaet_ruchku(root_client):
    """Блок выключен — адрес отвечает отказом, а не работает молча.

    Спрятать пункт меню недостаточно: адрес остаётся рабочим, его помнит
    браузер и он лежит в закладках.
    """
    root_client.post(f"{API}/modules/telegram", json={"enabled": False})
    otvet = root_client.get(f"{TG}/settings")
    assert otvet.status_code == 403, otvet.text
    assert otvet.json()["error"]["code"] == "module_disabled"


# --- приём входящих ----------------------------------------------------------

WEBHOOK = f"{API}/telegram/webhook"


def _sekret() -> str:
    """Секрет приёма у ручки не спросить — берём из базы, как это делает телеграм."""
    from core.services import telegram_service
    from database.session import SessionLocal

    with SessionLocal() as db:
        return telegram_service.webhook_secret(db)


def _obnovlenie(chat_id: int, message_id: int, **pole) -> dict:
    """Обновление в том виде, в каком его шлёт телеграм."""
    return {
        "update_id": message_id * 10,
        "message": {
            "message_id": message_id,
            "date": 1786000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Пётр", "username": "petr"},
            **pole,
        },
    }


@pytest.fixture()
def bot_nastroen(root_client, telegram_on):
    """Бот настроен. Без токена приём отвечает отказом — это отдельная проверка."""
    root_client.put(f"{TG}/settings", json={"token": OBRAZETS_TOKENA})
    yield _sekret()


def _poslat(root_client, sekret, telo):
    return root_client.post(
        WEBHOOK, json=telo, headers={"X-Telegram-Bot-Api-Secret-Token": sekret}
    )


def _dialog(root_client, chat_id: int) -> dict:
    nashli = [d for d in root_client.get(f"{TG}/chats").json()["items"] if d["chat_id"] == chat_id]
    assert len(nashli) == 1, f"диалогов с chat_id={chat_id} оказалось {len(nashli)}"
    return nashli[0]


def test_vhodyashchee_zavodit_dialog_i_soobshchenie(root_client, bot_nastroen):
    """Клиент написал — в CRM появился диалог и в нём его слова."""
    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(500100, 1, text="Сколько стоит?"))
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "accepted"

    dialog = _dialog(root_client, 500100)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["Сколько стоит?"]
    assert lenta[0]["direction"] == "in"


def test_povtornaya_dostavka_ne_dvoit(root_client, bot_nastroen):
    """Телеграм доставил то же сообщение дважды — в переписке оно одно.

    Повтор здесь не редкость, а устройство: телеграм повторяет, пока не получит
    200, и обрыв сети или наш перезапуск дают ровно это. Без защиты клиент
    увидел бы в CRM две копии своей фразы, а менеджер ответил бы дважды.
    """
    telo = _obnovlenie(500200, 7, text="Повторяю дважды")
    pervyy = _poslat(root_client, bot_nastroen, telo)
    vtoroy = _poslat(root_client, bot_nastroen, telo)
    assert pervyy.json()["status"] == "accepted"
    assert vtoroy.json()["status"] == "duplicate", vtoroy.text

    dialog = _dialog(root_client, 500200)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert len(lenta) == 1, f"сообщение задвоилось: {lenta}"


def test_chuzhoy_sekret_otvergaetsya(root_client, bot_nastroen):
    """Секрет — единственное, чем приём отличает телеграм от постороннего."""
    # Секрет латиницей, а не кириллицей: значение HTTP-заголовка — не юникод,
    # и кириллица здесь падает ещё в httpx, не доходя до проверки. Первая
    # редакция краснела именно так и проверяла не то, что собиралась.
    otvet = _poslat(root_client, "chuzhoy-sekret", _obnovlenie(500300, 1, text="я не телеграм"))
    assert otvet.status_code == 401, otvet.text
    assert otvet.json()["error"]["code"] == "bad_webhook_secret"


def test_gruppovoy_chat_propuskaetsya_molcha(root_client, bot_nastroen):
    """Групп у этого канала нет по устройству — общение один на один.

    Пропускаем молча и отвечаем 200, а не отказом: на отказ телеграм ответит
    повтором, и тот же мусор придёт снова.
    """
    telo = _obnovlenie(500400, 1, text="в группе")
    telo["message"]["chat"]["type"] = "group"
    otvet = _poslat(root_client, bot_nastroen, telo)
    assert otvet.status_code == 200
    assert otvet.json()["status"] == "ignored"


def test_kontakt_privyazyvaet_tolko_po_tochnomu_nomeru(root_client, bot_nastroen):
    """Клиент поделился номером, и номер совпал точно — диалог привязан.

    Точно и только точно. Привязка по имени в этом проекте запрещена оплаченным
    уроком: в заказах совпадение по частичному имени уводило деньги и товар на
    чужую карточку. Здесь ценой была бы переписка, которую читает не тот
    человек.
    """
    klient = root_client.post(
        f"{API}/clients", json={"name": "Пётр с номером", "phone": "+380671112233"}
    ).json()

    telo = _obnovlenie(500500, 1)
    telo["message"]["contact"] = {"phone_number": "+380671112233", "first_name": "Пётр"}
    _poslat(root_client, bot_nastroen, telo)

    assert _dialog(root_client, 500500)["client_id"] == klient["id"], (
        "диалог не привязался к карточке по точному совпадению номера"
    )


def test_neizvestnyy_nomer_ne_privyazyvaetsya(root_client, bot_nastroen):
    """Номер никому не принадлежит — диалог остаётся без карточки.

    Парная к предыдущей и важнее её: без этой проверки привязка, цепляющая кого
    попало, тоже была бы зелёной. Пустая привязка — законное состояние, а не
    ошибка.
    """
    telo = _obnovlenie(500600, 1)
    telo["message"]["contact"] = {"phone_number": "+380679998877", "first_name": "Никто"}
    _poslat(root_client, bot_nastroen, telo)

    assert _dialog(root_client, 500600)["client_id"] is None, (
        "диалог привязался к чужой карточке"
    )


def test_metka_iz_ssylki_zapominaetsya(root_client, bot_nastroen):
    """`/start naklejka` — откуда клиент пришёл. Ссылка умеет нести метку."""
    _poslat(root_client, bot_nastroen, _obnovlenie(500700, 1, text="/start naklejka"))
    assert _dialog(root_client, 500700)["source"] == "naklejka"


# --- отправка ----------------------------------------------------------------


def test_otvet_uhodit_v_telegram_i_lozhitsya_v_perepisku(root_client, bot_nastroen, monkeypatch):
    """Менеджер ответил — телеграм вызван, ответ виден в переписке.

    Настоящей сети здесь нет и быть не должно: проверка не имеет права зависеть
    от чужой доступности, а настоящий бот означал бы настоящие сообщения
    настоящим людям.
    """
    from core.services import telegram_service

    _poslat(root_client, bot_nastroen, _obnovlenie(500800, 1, text="вопрос"))
    dialog = _dialog(root_client, 500800)

    ushlo = []

    def podstava(kluch, chat_id, text, opener=None):
        ushlo.append((chat_id, text))
        return {"message_id": 555}

    monkeypatch.setattr(telegram_service, "poslat_tekst", podstava)

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/messages", json={"text": "Ответ клиенту"})
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["send_state"] == "sent"
    assert otvet.json()["direction"] == "out"
    assert ushlo == [(500800, "Ответ клиенту")], f"в телеграм ушло не то: {ushlo}"

    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["вопрос", "Ответ клиенту"]


def test_otkaz_telegrama_viden_menedzheru(root_client, bot_nastroen, monkeypatch):
    """Телеграм отказал — сообщение остаётся с пометкой и причиной.

    Не откатываем и не прячем: менеджер обязан увидеть, что ответ не ушёл.
    Иначе он уверен, что ответил, и ждёт реакции клиента, которой не будет.
    Причина отдаётся наружу, потому что «бот заблокирован пользователем» и
    «неверный токен» чинятся по-разному.
    """
    from core.services import telegram_service

    _poslat(root_client, bot_nastroen, _obnovlenie(500900, 1, text="вопрос"))
    dialog = _dialog(root_client, 500900)

    def otkazat(*args, **kwargs):
        raise telegram_service.TelegramOtkaz("bot was blocked by the user")

    monkeypatch.setattr(telegram_service, "poslat_tekst", otkazat)

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/messages", json={"text": "Не дойдёт"})
    assert otvet.status_code == 201, otvet.text
    telo = otvet.json()
    assert telo["send_state"] == "failed"
    assert "blocked" in telo["send_error"], f"причина отказа потерялась: {telo}"


def test_pustoy_otvet_ne_uhodit(root_client, bot_nastroen):
    """Пустое сообщение клиенту не уходит."""
    _poslat(root_client, bot_nastroen, _obnovlenie(501000, 1, text="вопрос"))
    dialog = _dialog(root_client, 501000)
    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/messages", json={"text": "   "})
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "message_empty"


def test_privyazka_rukami_menyaet_kartochku(root_client, bot_nastroen):
    """Чей диалог — решает человек, а не догадка системы."""
    _poslat(root_client, bot_nastroen, _obnovlenie(501100, 1, text="а это кто"))
    dialog = _dialog(root_client, 501100)
    assert dialog["client_id"] is None

    klient = root_client.post(f"{API}/clients", json={"name": "Названный вручную"}).json()
    otvet = root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]})
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["client_id"] == klient["id"]


def test_privyazka_k_nesushchestvuyushchemu_otvergaetsya(root_client, bot_nastroen):
    """Карточки нет — отказ, а не привязка в никуда."""
    _poslat(root_client, bot_nastroen, _obnovlenie(501200, 1, text="и это"))
    dialog = _dialog(root_client, 501200)
    otvet = root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": 999999})
    assert otvet.status_code == 404
    assert otvet.json()["error"]["code"] == "client_not_found"


def test_perepiska_privyazannogo_dialoga_vidna_v_lente_klienta(root_client, bot_nastroen):
    """Привязали диалог — сообщения попадают в общую ленту клиента.

    Ради этого весь канал и переносится в CRM: открыл карточку — видишь ВСЁ
    общение с человеком, а не три отдельных окна. Почта и звонки уже там,
    телеграм становится третьим.
    """
    klient = root_client.post(
        f"{API}/clients", json={"name": "Лента и телеграм", "phone": "+380675554433"}
    ).json()

    telo = _obnovlenie(501300, 1, text="Сообщение в ленту")
    telo["message"]["contact"] = {"phone_number": "+380675554433", "first_name": "Пётр"}
    _poslat(root_client, bot_nastroen, telo)

    zapisi = root_client.get(f"{API}/clients/{klient['id']}/notes?per_page=200").json()["items"]
    iz_telegrama = [z for z in zapisi if z["kind"] == "telegram"]
    assert iz_telegrama, f"записи из телеграма нет в ленте клиента: {zapisi}"
