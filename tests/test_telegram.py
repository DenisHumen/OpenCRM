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

    def podstava(kluch, chat_id, text, opener=None, otvet_na=None):
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


# --- живое состояние ---------------------------------------------------------


def _dat_pravo_na_telegram(root_client, manager_client) -> None:
    """Выдать менеджеру право смотреть переписку.

    Роль по умолчанию о новом разделе не знает — так и должно быть: права на
    свежий блок не появляются у всех сами. Значит в проверке их надо выдать
    явно, как это сделает владелец в матрице доступов.
    """
    ya = manager_client.get(f"{API}/auth/me").json()
    roli = root_client.get(f"{API}/roles").json()["items"]
    moya = next(r for r in roli if r["id"] == ya["role_id"])
    obnovleno = root_client.patch(
        f"{API}/roles/{moya['id']}",
        json={"permissions": sorted(set(moya["permissions"]) | {"telegram.view"})},
    )
    assert obnovleno.status_code == 200, obnovleno.text


def test_prisutstvie_pokazyvaet_kto_v_chate(root_client, manager_client, bot_nastroen):
    """Двое открыли один диалог — оба видят друг друга.

    Ради этого весь живой слой и строится: баннер «в чате не только вы» должен
    показывать ИМЕНА, а не число. Число не говорит, с кем договариваться.
    """
    _dat_pravo_na_telegram(root_client, manager_client)
    _poslat(root_client, bot_nastroen, _obnovlenie(502100, 1, text="кто ответит"))
    dialog = _dialog(root_client, 502100)

    pervyy = root_client.post(f"{TG}/chats/{dialog['id']}/presence", json={"present": True})
    assert pervyy.status_code == 200, pervyy.text
    assert len(pervyy.json()["watchers"]) == 1, "сам себя в списке не увидел"

    vtoroy = manager_client.post(f"{TG}/chats/{dialog['id']}/presence", json={"present": True})
    assert vtoroy.status_code == 200, vtoroy.text
    imena = {kto["name"] for kto in vtoroy.json()["watchers"]}
    assert len(imena) == 2, f"второй не увидел первого: {vtoroy.json()['watchers']}"


def test_ushedshiy_propadaet_srazu(root_client, bot_nastroen):
    """Закрыл чат — исчез из баннера немедленно, а не через срок годности.

    Срок снял бы отметку и сам, но через пятнадцать секунд. Всё это время сосед
    видел бы предупреждение о конфликте с человеком, который уже ушёл, — то
    есть предупреждение о том, чего нет.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(502200, 1, text="я ухожу"))
    dialog = _dialog(root_client, 502200)

    root_client.post(f"{TG}/chats/{dialog['id']}/presence", json={"present": True})
    ushyol = root_client.post(f"{TG}/chats/{dialog['id']}/presence", json={"present": False})
    assert ushyol.status_code == 200, ushyol.text
    assert ushyol.json()["watchers"] == [], "ушедший остался в чате"


def test_prisutstvie_v_chuzhom_dialoge_otvergaetsya(root_client, bot_nastroen):
    """Отметиться в несуществующем диалоге нельзя."""
    otvet = root_client.post(f"{TG}/chats/999999/presence", json={"present": True})
    assert otvet.status_code == 404
    assert otvet.json()["error"]["code"] == "telegram_chat_not_found"


def test_potok_otvechaet_srazu_i_ne_buferizuetsya(root_client, telegram_on, monkeypatch):
    """Поток здоровается первым событием и просит nginx не копить ответ.

    Оба свойства проверяются вместе, потому что оба невидимы на локальной
    машине. Без первого события вкладка полминуты не знает, подключилась она
    или висит. Без `X-Accel-Buffering: no` nginx копит поток в буфере и отдаёт
    пачкой — то есть поток перестаёт быть потоком, а становится очень медленным
    запросом. Заметить это без настоящего nginx нельзя вовсе.
    """
    # Срок жизни соединения на время проверки — секунда. Без него поток
    # держится пять минут, и проверка ждала бы их полностью: разрыв со стороны
    # тестового клиента до приложения не доходит, `is_disconnected` в нём не
    # срабатывает. Так и вышло в первой редакции — прогон завис.
    from web.api.routes import telegram as marshruty

    monkeypatch.setattr(marshruty, "MAX_ZHIZN_POTOKA", 1)

    with root_client.stream("GET", f"{TG}/stream") as otvet:
        assert otvet.status_code == 200
        assert otvet.headers["content-type"].startswith("text/event-stream")
        assert otvet.headers.get("x-accel-buffering") == "no", (
            "nginx будет копить поток в буфере, и живые обновления станут пачками"
        )
        for stroka in otvet.iter_lines():
            if stroka.startswith("data:"):
                import json as _json

                sobytie = _json.loads(stroka[len("data:") :])
                assert sobytie["type"] == "ready"
                break


def test_novoe_soobshchenie_obyavlyaetsya_v_shinu(root_client, bot_nastroen):
    """Пришло сообщение — о нём объявлено всем процессам.

    Проверяем шину напрямую, а не через поток: поток — это уже способ доставки,
    а объявление обязано случиться независимо от того, слушает его кто-нибудь
    или нет. Иначе сообщение, пришедшее в момент, когда все вкладки закрыты,
    не объявлялось бы вовсе.
    """
    from core import realtime

    podpiska = realtime.podpisatsya()
    assert podpiska is not None, "Redis недоступен — живой слой не проверить"
    try:
        _poslat(root_client, bot_nastroen, _obnovlenie(502300, 1, text="объяви меня"))

        import json as _json
        import time as _time

        uslyshano = []
        # Ждём недолго: объявление уходит сразу, а не по расписанию. Секунды с
        # запасом хватает соседнему контейнеру на той же машине.
        do = _time.monotonic() + 3
        while _time.monotonic() < do and not uslyshano:
            soobshchenie = podpiska.get_message(timeout=0.2)
            if soobshchenie and soobshchenie.get("data"):
                dannye = soobshchenie["data"]
                if isinstance(dannye, bytes):
                    dannye = dannye.decode("utf-8")
                razobrano = _json.loads(dannye)
                if razobrano.get("type") == "message":
                    uslyshano.append(razobrano)

        assert uslyshano, "о новом сообщении в шину не объявили"
        assert uslyshano[0]["direction"] == "in"
        assert uslyshano[0]["preview"] == "объяви меня"
        # Имя собеседника едет в самом событии. Без него уведомление на экране
        # менеджера говорит «Telegram» и ничего больше: по такому непонятно,
        # бежать отвечать или это очередной «спасибо».
        assert uslyshano[0]["title"] == "Пётр", f"в событии нет имени: {uslyshano[0]}"
    finally:
        podpiska.close()


# --- утренняя сводка ---------------------------------------------------------


def test_svodka_uhodit_i_soderzhit_tsifry(root_client, bot_nastroen, monkeypatch):
    """Сводка собирается и уходит в назначенный чат.

    Настоящей сети здесь нет: подставляем отправку и смотрим, ЧТО именно ушло.
    Проверять «вызвали телеграм» без разбора текста бессмысленно — сводка,
    ушедшая пустой, вызывает его точно так же.
    """
    from core.services import telegram_service

    root_client.put(f"{TG}/settings", json={"digest_chat": "123456789"})

    ushlo = []
    monkeypatch.setattr(
        telegram_service,
        "poslat_tekst_razmetkoy",
        lambda kluch, chat_id, text, opener=None: ushlo.append((chat_id, text))
        or {"message_id": 1},
    )

    otvet = root_client.post(f"{TG}/digest/send")
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "sent", otvet.text

    assert len(ushlo) == 1, f"сводка ушла не один раз: {ushlo}"
    chat_id, tekst = ushlo[0]
    assert chat_id == 123456789, "сводка ушла не в тот чат"
    assert "Сводка за" in tekst
    assert "Заявок новых" in tekst, f"в сводке нет цифр по делу:\n{tekst}"


def test_svodka_bez_nastroyki_molchit(root_client, telegram_on):
    """Чат не назван — сводка не отправляется и это не отказ.

    Не отказ намеренно: расписание не должно краснеть у того, кто телеграмом не
    пользуется. Красное расписание, которое так и задумано, перестают читать —
    и вместе с ним перестают читать настоящие отказы.
    """
    otvet = root_client.post(f"{TG}/digest/send")
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "skipped"
    assert otvet.json()["reason"] == "not_configured"


def test_upavshiy_razdel_ne_ronyaet_svodku(root_client, bot_nastroen, monkeypatch):
    """Один счёт сломался — сводка всё равно уходит и говорит об этом.

    Иначе одна сломанная выборка означала бы, что владелец не получит НИЧЕГО, и
    не узнает, что канал жив. А молчание сводки читается как «всё сломалось» —
    то есть поломка одного счёта выглядела бы как поломка всего.
    """
    from core.services import telegram_service
    from database.repositories import svodka as svodka_repo

    root_client.put(f"{TG}/settings", json={"digest_chat": "123456789"})

    def slomat(*args, **kwargs):
        raise RuntimeError("подставная поломка счёта")

    monkeypatch.setattr(svodka_repo, "prosrocheno_napominaniy", slomat)

    ushlo = []
    monkeypatch.setattr(
        telegram_service,
        "poslat_tekst_razmetkoy",
        lambda kluch, chat_id, text, opener=None: ushlo.append(text) or {"message_id": 1},
    )

    otvet = root_client.post(f"{TG}/digest/send")
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "sent", "сломанный раздел уронил всю сводку"
    assert otvet.json()["otkazy"], "о несосчитанном разделе не сказано"
    assert "Не сосчиталось" in ushlo[0], f"в тексте нет пометки о поломке:\n{ushlo[0]}"

    # Парная проверка: `svodka_service` не проглотил поломку молча.
    assert "просроченные напоминания" in otvet.json()["otkazy"]


def test_okno_svodki_eto_proshedshie_sutki_po_mestnomu(root_client):
    """Сводка считает вчерашние сутки по МЕСТНОМУ времени, а не по UTC.

    Перепутать здесь легче всего, и цена ошибки — цифры, сдвинутые на несколько
    часов, которые выглядят правдоподобно. «За вчера» обязано означать вчера
    того, кто читает, иначе утренние цифры включают чужой вечер.
    """
    from datetime import datetime, timedelta, timezone

    from core.services import svodka_service

    # Полночь по местному: окно обязано быть ровно предыдущими сутками.
    mestnyy_polden = datetime(2026, 8, 16, 12, 0, tzinfo=svodka_service.POYAS)
    ot, do, den = svodka_service._sutki(mestnyy_polden)

    assert do - ot == timedelta(days=1), f"окно не сутки: {do - ot}"
    assert den == "15.08.2026", f"подпись не про вчера: {den}"
    # Границы отдаются в UTC и без пояса — база живёт в нём.
    assert ot.tzinfo is None and do.tzinfo is None
    ozhidaemoe_nachalo = (
        datetime(2026, 8, 15, 0, 0, tzinfo=svodka_service.POYAS)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    assert ot == ozhidaemoe_nachalo, f"начало окна съехало: {ot} против {ozhidaemoe_nachalo}"


def test_razmetka_svodki_imeet_zapasnoy_put(root_client, bot_nastroen, monkeypatch):
    """Телеграм не разобрал разметку — сводка уходит плоским текстом.

    Свойство, ради которого это написано: ошибка в ОФОРМЛЕНИИ не имеет права
    заглушить сводку. Молчание сводки читается как «всё сломалось», и разбирать
    будут не ту поломку.
    """
    from core.services import telegram_service

    vyzovy = []

    def podstava(token, metod, polya, opener=None):
        vyzovy.append(polya)
        if polya.get("parse_mode"):
            raise telegram_service.TelegramOtkaz("can't parse entities")
        return {"message_id": 2}

    monkeypatch.setattr(telegram_service, "_vyzov", podstava)

    itog = telegram_service.poslat_tekst_razmetkoy(
        "123:AAA", 5, "<b>Сводка</b> и <i>цифры</i>"
    )
    assert itog == {"message_id": 2}
    assert len(vyzovy) == 2, "запасного пути не было — сводка потерялась бы"
    assert "parse_mode" not in vyzovy[1], "вторая попытка снова с разметкой"
    assert vyzovy[1]["text"] == "Сводка и цифры", (
        f"теги не сняты, телеграм отобьёт и это: {vyzovy[1]['text']!r}"
    )


# --- приглашение и подключение -----------------------------------------------


def test_priglashenie_daet_ssylku_i_kod(root_client, bot_nastroen):
    """Ссылка и QR, которыми клиента приводят к боту.

    Без этого канал не работает вовсе, и это не украшение: телеграм не
    позволяет боту написать первым тому, кто его не запускал. Клиент обязан
    начать разговор сам — значит нужен способ его привести.
    """
    root_client.put(f"{TG}/settings", json={"bot_username": "@moy_bot"})

    otvet = root_client.get(f"{TG}/invite", params={"label": "naklejka"})
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    # Собачка снимается при сохранении: в ссылке её быть не должно.
    assert telo["url"] == "https://t.me/moy_bot?start=naklejka", telo["url"]
    assert telo["qr_svg"].lstrip().startswith("<svg"), "QR-код не отрисовался"


def test_priglashenie_bez_imeni_bota_otkazyvaet_ponyatno(root_client, telegram_on):
    """Имя бота не задано — отказ с внятным кодом, а не ссылка в никуда.

    Ссылка `https://t.me/?start=…` выглядит настоящей и не работает. Отдать её
    значило бы разослать клиентам нерабочий адрес и узнать об этом от них.
    """
    # Имя бота стираем явно: отключение бота его НЕ трогает (оно про связь, а
    # не про данные), и соседняя проверка оставила бы своё. Первая редакция
    # этого не учла и краснела на настоящей ссылке.
    root_client.put(f"{TG}/settings", json={"bot_username": ""})

    otvet = root_client.get(f"{TG}/invite")
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_username_missing"


def test_metka_priglasheniya_proveryaetsya(root_client, bot_nastroen):
    """Метка — только буквы, цифры, дефис и подчёркивание.

    Требование самого телеграма. Пропусти мы сюда пробел или кириллицу — вышла
    бы ссылка, которая молча не работает, а это худший вид поломки: она
    выглядит рабочей.
    """
    root_client.put(f"{TG}/settings", json={"bot_username": "moy_bot"})
    otvet = root_client.get(f"{TG}/invite", params={"label": "с сайта"})
    assert otvet.status_code == 422, otvet.text


def test_podklyuchenie_bez_https_obyasnyaet_prichinu(root_client, bot_nastroen):
    """Телеграм принимает вебхук только по HTTPS — говорим это своими словами.

    В проверках адрес сайта задан по http, и это тот самый случай. Отдать
    человеку отказ телеграма дословно значило бы отправить его читать чужую
    документацию; он читает наш экран.
    """
    otvet = root_client.post(f"{TG}/connect")
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_needs_https"


def test_podklyuchenie_bez_tokena_otkazyvaet(root_client, telegram_on):
    """Подключать нечего, пока не введён токен."""
    otvet = root_client.post(f"{TG}/connect")
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_not_configured"


def test_vlozhenie_otdayotsya_tolko_svoyo(root_client, bot_nastroen, monkeypatch):
    """Файл отдаётся по своему диалогу и не отдаётся по чужому.

    Проверка на принадлежность, а не только на существование. Идентификаторы
    сообщений сквозные, и без неё адрес чужого диалога с чужим номером
    сообщения отдавал бы чужую переписку тому, кто просто подставил число.
    """
    from core.services import telegram_service

    # Кладём входящее с файлом, подставив загрузку из телеграма.
    monkeypatch.setattr(
        telegram_service, "skachat_fayl", lambda kluch, file_id, opener=None: b"soderzhimoe"
    )
    telo = _obnovlenie(503100, 1, caption="вот файл")
    telo["message"]["document"] = {
        "file_id": "AAA",
        "file_name": "dogovor.pdf",
        "file_size": 11,
    }
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 503100)

    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert lenta[0]["has_file"] is True, f"файл не забрался: {lenta}"

    svoyo = root_client.get(
        f"{TG}/chats/{dialog['id']}/messages/{lenta[0]['id']}/file"
    )
    assert svoyo.status_code == 200, svoyo.text
    assert svoyo.content == b"soderzhimoe"

    # Тот же файл по ЧУЖОМУ диалогу — отказ.
    _poslat(root_client, bot_nastroen, _obnovlenie(503200, 1, text="чужой диалог"))
    chuzhoy = _dialog(root_client, 503200)
    otkaz = root_client.get(
        f"{TG}/chats/{chuzhoy['id']}/messages/{lenta[0]['id']}/file"
    )
    assert otkaz.status_code == 404, "файл отдан по чужому диалогу"


# --- видео по нажатию --------------------------------------------------------


def test_video_ne_zabiraetsya_srazu_no_zabiraetsya_po_nazhatiyu(
    root_client, bot_nastroen, monkeypatch
):
    """Видео помечается, но не тянется, — и тянется, когда его попросили.

    Переписка с видео съест диск за недели, а в этом проекте за диском уже
    однажды никто не следил, пока не стало поздно. Но и терять возможность
    посмотреть нельзя: клиент прислал видео о поломке, а менеджер его не видит.

    Проверка парная в одном теле нарочно: по отдельности каждая половина
    зеленела бы и на неверном поведении. «Не забрали сразу» верно и для видео,
    которое не забирается никогда; «забрали по нажатию» верно и для видео,
    которое тянется само.
    """
    from core.services import telegram_service

    skachivaniy = []

    def podstava(kluch, file_id, opener=None):
        skachivaniy.append(file_id)
        return b"eto video"

    monkeypatch.setattr(telegram_service, "skachat_fayl", podstava)

    telo = _obnovlenie(504100, 1, caption="вот поломка")
    telo["message"]["video"] = {
        "file_id": "VIDEO-1",
        "file_name": "polomka.mp4",
        "file_size": 5_000_000,
    }
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 504100)

    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    stroka = lenta[0]
    assert skachivaniy == [], "видео забрали сразу — диск кончится за недели"
    assert stroka["has_file"] is False
    assert stroka["can_fetch"] is True, "видео нечем забрать позже"
    assert stroka["file_name"] == "polomka.mp4", (
        "имя потерялось — в переписке стояло бы безымянное «видео»"
    )

    zabrano = root_client.post(
        f"{TG}/chats/{dialog['id']}/messages/{stroka['id']}/fetch"
    )
    assert zabrano.status_code == 200, zabrano.text
    assert skachivaniy == ["VIDEO-1"], f"забрали не то: {skachivaniy}"
    assert zabrano.json()["has_file"] is True
    assert zabrano.json()["can_fetch"] is False

    fayl = root_client.get(
        f"{TG}/chats/{dialog['id']}/messages/{stroka['id']}/file"
    )
    assert fayl.status_code == 200
    assert fayl.content == b"eto video"


def test_povtornoe_nazhatie_ne_kachaet_dvazhdy(root_client, bot_nastroen, monkeypatch):
    """Кнопку нажали дважды — телеграм спросили один раз.

    Кнопки нажимают дважды, это обычное дело. Второе скачивание того же видео —
    это лишний трафик и лишний файл на диске рядом с первым.
    """
    from core.services import telegram_service

    skachivaniy = []
    monkeypatch.setattr(
        telegram_service,
        "skachat_fayl",
        lambda kluch, file_id, opener=None: skachivaniy.append(file_id) or b"video",
    )

    telo = _obnovlenie(504200, 1)
    telo["message"]["video"] = {"file_id": "VIDEO-2", "file_size": 1000}
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 504200)
    stroka = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"][0]

    adres = f"{TG}/chats/{dialog['id']}/messages/{stroka['id']}/fetch"
    assert root_client.post(adres).status_code == 200
    assert root_client.post(adres).status_code == 200
    assert skachivaniy == ["VIDEO-2"], f"скачали дважды: {skachivaniy}"


def test_kartinka_po_prezhnemu_zabiraetsya_srazu(root_client, bot_nastroen, monkeypatch):
    """Парная к видео: картинку тянем сразу, и это не должно сломаться.

    Без неё правка «не тянуть видео» могла бы заодно перестать тянуть всё
    остальное, и переписка в CRM стала бы неполной — то есть менеджер полез бы
    в свой телефон, ровно туда, откуда общение и уводили.
    """
    from core.services import telegram_service

    monkeypatch.setattr(
        telegram_service, "skachat_fayl", lambda kluch, file_id, opener=None: b"kartinka"
    )

    telo = _obnovlenie(504300, 1, caption="фото")
    telo["message"]["photo"] = [{"file_id": "PH-1", "file_size": 2048}]
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 504300)

    stroka = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"][0]
    assert stroka["has_file"] is True, "картинка перестала забираться сразу"
    assert stroka["can_fetch"] is False


# --- непрочитанное -----------------------------------------------------------


def test_neprochitannoe_schitaetsya_i_snimaetsya_chteniem(root_client, bot_nastroen):
    """Пришло входящее — счётчик вырос; открыли диалог — обнулился.

    Обе половины в одном теле нарочно. По отдельности каждая зеленела бы на
    неверном поведении: «счётчик вырос» верно и для счётчика, который никогда не
    обнуляется, а «обнулился» — для того, который всегда ноль.

    Прочитанное отмечается САМИМ чтением ленты, а не отдельной кнопкой: человек
    открыл диалог и увидел сообщения. Кнопка «прочитано» существует ровно
    затем, чтобы её забывали нажимать.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(505100, 1, text="первое"))
    _poslat(root_client, bot_nastroen, _obnovlenie(505100, 2, text="второе"))

    dialog = _dialog(root_client, 505100)
    assert dialog["unread"] == 2, f"счётчик непрочитанного неверен: {dialog}"

    # Открыли ленту — значит прочитали.
    root_client.get(f"{TG}/chats/{dialog['id']}/messages")
    assert _dialog(root_client, 505100)["unread"] == 0, "чтение ленты не сняло счётчик"

    # Пришло ещё — счётчик снова про новое, а не про всё подряд.
    _poslat(root_client, bot_nastroen, _obnovlenie(505100, 3, text="третье"))
    assert _dialog(root_client, 505100)["unread"] == 1, (
        "после дочитывания счётчик считает не с границы, а с начала"
    )


def test_svoy_otvet_ne_stanovitsya_neprochitannym(root_client, bot_nastroen, monkeypatch):
    """Свои же ответы непрочитанными не бывают.

    Иначе значок горел бы после каждого собственного ответа, и его перестали бы
    замечать — вместе с настоящими сообщениями клиентов.
    """
    from core.services import telegram_service

    monkeypatch.setattr(
        telegram_service,
        "poslat_tekst",
        lambda kluch, chat_id, text, opener=None, otvet_na=None: {"message_id": 900},
    )

    _poslat(root_client, bot_nastroen, _obnovlenie(505200, 1, text="вопрос"))
    dialog = _dialog(root_client, 505200)
    root_client.get(f"{TG}/chats/{dialog['id']}/messages")
    assert _dialog(root_client, 505200)["unread"] == 0

    root_client.post(f"{TG}/chats/{dialog['id']}/messages", json={"text": "мой ответ"})
    assert _dialog(root_client, 505200)["unread"] == 0, (
        "собственный ответ засчитан непрочитанным"
    )


def test_granitsa_prochitannogo_tolko_rastyot(root_client, bot_nastroen):
    """Открыли старую страницу переписки — граница назад не поехала.

    Иначе листание вглубь помечало бы непрочитанным всё, что пришло позже:
    человек ушёл читать начало разговора и вернулся к десятку «новых»
    сообщений, которых он уже читал.
    """
    for nomer in range(1, 4):
        _poslat(root_client, bot_nastroen, _obnovlenie(505300, nomer, text=f"№{nomer}"))
    dialog = _dialog(root_client, 505300)

    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert _dialog(root_client, 505300)["unread"] == 0

    # Листаем вглубь: просим то, что ДО первого показанного.
    root_client.get(f"{TG}/chats/{dialog['id']}/messages?before={lenta[-1]['id']}")
    assert _dialog(root_client, 505300)["unread"] == 0, (
        "листание вглубь сдвинуло границу назад"
    )


def test_neprochitannoe_lichnoe_a_ne_obshchee(root_client, manager_client, bot_nastroen):
    """Один прочитал — у второго счётчик остался.

    Граница «дочитал до сюда» личная: общая означала бы, что первый открывший
    диалог гасит значок всем, и сообщение клиента теряется для того, кто его не
    видел.
    """
    _dat_pravo_na_telegram(root_client, manager_client)
    _poslat(root_client, bot_nastroen, _obnovlenie(505400, 1, text="кому-то одному"))

    dialog = _dialog(root_client, 505400)
    root_client.get(f"{TG}/chats/{dialog['id']}/messages")
    assert _dialog(root_client, 505400)["unread"] == 0

    chuzhoy = [
        d for d in manager_client.get(f"{TG}/chats").json()["items"] if d["chat_id"] == 505400
    ][0]
    assert chuzhoy["unread"] == 1, "чужое чтение погасило значок у соседа"


def test_otbor_po_metke_istochnika(root_client, bot_nastroen):
    """Метка из ссылки отвечает на вопрос «откуда пришли клиенты».

    Без отбора она лежала бы мёртвым грузом: записана — и не спросишь. А
    спрашивают её ровно затем, ради чего метки и заводят: сравнить, что
    работает — наклейка на квитанции или кнопка на сайте.

    Совпадение точное, а не по подстроке: метки короткие и назначает их
    владелец сам, а подстрока склеила бы «sayt» и «sayt-akciya».
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(506100, 1, text="/start sayt"))
    _poslat(root_client, bot_nastroen, _obnovlenie(506200, 1, text="/start sayt-akciya"))
    _poslat(root_client, bot_nastroen, _obnovlenie(506300, 1, text="/start naklejka"))

    s_sayta = root_client.get(f"{TG}/chats", params={"source": "sayt"}).json()["items"]
    nomera = {d["chat_id"] for d in s_sayta}
    assert 506100 in nomera, "диалог с меткой не нашёлся"
    assert 506200 not in nomera, "подстрока склеила «sayt» и «sayt-akciya»"
    assert 506300 not in nomera, "в отбор попал чужой источник"

    # Без отбора видны все три.
    vse = {d["chat_id"] for d in root_client.get(f"{TG}/chats").json()["items"]}
    assert {506100, 506200, 506300} <= vse, "без отбора список неполон"


# --- заявка и задача из диалога ----------------------------------------------


def test_zayavka_iz_dialoga_beryot_nazvanie_iz_razgovora(root_client, bot_nastroen):
    """Заявка заводится по переписке, и названа она тем, с чего клиент начал.

    Ради этого канал и внутри CRM, а не в соседней вкладке: иначе менеджер
    читает переписку здесь, а заявку заводит отдельно, перенося сведения
    руками — ровно ту работу, которую перенос должен был убрать.

    Название по умолчанию из последнего входящего, а не «Заявка из телеграма»:
    переименовать потом — одно движение, а вспомнить, о чём был разговор, по
    безликому названию нельзя.
    """
    klient = root_client.post(
        f"{API}/clients", json={"name": "Из диалога", "phone": "+380671230011"}
    ).json()

    telo = _obnovlenie(507100, 1, text="Нужен ремонт холодильника")
    telo["message"]["contact"] = {"phone_number": "+380671230011", "first_name": "Пётр"}
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 507100)
    assert dialog["client_id"] == klient["id"]

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/deal", json={})
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["title"] == "Нужен ремонт холодильника", otvet.text
    assert otvet.json()["client_id"] == klient["id"]


def test_zayavka_bez_privyazki_otkazyvaet_ponyatno(root_client, bot_nastroen):
    """Диалог ничей — заявку не заводим и говорим почему.

    Подставить «кого-нибудь» нельзя: это тот же оплаченный урок про чужую
    карточку. Завести клиента самим — тоже: решение «этот диалог — вот этот
    человек» принимает человек, а не догадка.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(507200, 1, text="а это кто"))
    dialog = _dialog(root_client, 507200)

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/deal", json={})
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_chat_not_linked"


def test_zadacha_iz_dialoga_zavoditsya_i_bez_privyazki(root_client, bot_nastroen):
    """Напоминание не требует карточки, в отличие от заявки.

    «Перезвонить этому человеку» осмысленно и до того, как выяснили, кто он.
    Требовать сначала разобраться, а потом уже не забыть — значит потерять
    ровно те разговоры, где разбираться было некогда.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(507300, 1, text="перезвоните позже"))
    dialog = _dialog(root_client, 507300)

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/task", json={})
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["title"], "задача без названия"


def test_svoyo_nazvanie_pobezhdaet_ugadannoe(root_client, bot_nastroen):
    """Назвали заявку сами — берётся названное, а не угаданное из переписки."""
    klient = root_client.post(
        f"{API}/clients", json={"name": "Своё название", "phone": "+380671230022"}
    ).json()
    telo = _obnovlenie(507400, 1, text="здравствуйте")
    telo["message"]["contact"] = {"phone_number": "+380671230022", "first_name": "Пётр"}
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 507400)

    otvet = root_client.post(
        f"{TG}/chats/{dialog['id']}/deal", json={"title": "Замена компрессора"}
    )
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["title"] == "Замена компрессора"
    assert otvet.json()["client_id"] == klient["id"]


# --- предел телеграма на файл ------------------------------------------------


def test_predel_telegrama_polveka_ne_menyaetsya():
    """Пятьдесят мегабайт — предел самого телеграма, а не наша настройка.

    Проверка на число выглядит мелочно ровно до первой правки «а давайте
    поднимем». Поднять его нельзя: Bot API откажет, файл к клиенту не уйдёт, а
    человек узнает об этом словами nginx после полной заливки.

    То же число названо в `Telegram.tsx` — браузер бережёт от впустую
    потраченной заливки, сервер от обхода браузера.
    """
    from core.services.telegram_service import MAX_TELEGRAM_FILE

    assert MAX_TELEGRAM_FILE == 50 * 1024 * 1024


def test_slishkom_bolshoy_fayl_otvergaetsya_do_otpravki(
    root_client, bot_nastroen, monkeypatch
):
    """Отказ приходит ДО обращения к телеграму и не оставляет следов.

    На живом показе видео уходило целиком и только тогда получало
    «Request Entity Too Large» — чужими словами, без намёка на предел и на то,
    что делать. Здесь важны три вещи разом: отказ понятный, телеграм не
    дёргали, строки «не доставлено» в переписке не осталось. Последнее не
    придирка: строка означала бы, что менеджер видит в диалоге сообщение,
    которого клиенту не отправляли и отправить не могли.
    """
    from core.services import telegram_service

    _poslat(root_client, bot_nastroen, _obnovlenie(505500, 1, text="привет"))
    dialog = _dialog(root_client, 505500)
    bylo = len(root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"])

    zvali = []
    monkeypatch.setattr(
        telegram_service,
        "poslat_fayl",
        lambda *a, **k: zvali.append(a) or {"message_id": 1},
    )
    # Предел двигаем вниз, а не шлём пятьдесят мегабайт: проверяем сторожа, а не
    # выносливость памяти. Само число проверено соседним тестом.
    monkeypatch.setattr(telegram_service, "MAX_TELEGRAM_FILE", 16)

    otvet = root_client.post(
        f"{TG}/chats/{dialog['id']}/files",
        files={"file": ("otchet.pdf", b"x" * 64, "application/pdf")},
    )
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_file_too_large", otvet.text
    assert "MB" in otvet.json()["error"]["message"], otvet.text

    assert not zvali, "телеграм дёрнули файлом, который заведомо не примут"
    stalo = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert len(stalo) == bylo, f"осталась строка о неотправленном: {stalo}"


# --- фотография видна в переписке --------------------------------------------


def test_fotografiya_otdayotsya_dlya_pokaza_a_dokument_dlya_sohraneniya(
    root_client, bot_nastroen, monkeypatch
):
    """Фото открывается, документ скачивается — по заголовку показа.

    Переписка рисует фотографию картинкой по этой самой ссылке. Стой на ней
    `attachment`, щелчок по фото начинал бы скачивание вместо просмотра.

    Обратное послабление опаснее: присланный посторонним HTML, показанный с
    нашего домена, — это чужой скрипт в нашем происхождении. Поэтому `inline`
    ровно у фотографий, а не у всего, что похоже на картинку.
    """
    from core.services import telegram_service

    monkeypatch.setattr(
        telegram_service, "skachat_fayl", lambda kluch, file_id, opener=None: b"soderzhimoe"
    )

    telo = _obnovlenie(505600, 1, caption="фото")
    telo["message"]["photo"] = [{"file_id": "PH-9", "file_size": 2048}]
    _poslat(root_client, bot_nastroen, telo)

    telo = _obnovlenie(505600, 2, caption="документ")
    telo["message"]["document"] = {
        "file_id": "DOC-9",
        "file_name": "dogovor.pdf",
        "file_size": 11,
    }
    _poslat(root_client, bot_nastroen, telo)

    dialog = _dialog(root_client, 505600)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    po_vidu = {s["kind"]: s["id"] for s in lenta}
    assert "photo" in po_vidu and "document" in po_vidu, f"не тот разбор: {lenta}"

    foto = root_client.get(
        f"{TG}/chats/{dialog['id']}/messages/{po_vidu['photo']}/file"
    )
    assert foto.status_code == 200, foto.text
    assert foto.headers["content-disposition"].startswith("inline"), (
        f"фото отдаётся на скачивание: {foto.headers['content-disposition']}"
    )
    # Вид содержимого важен не меньше: заголовок `nosniff` стоит на всём, и с
    # `text/plain` браузер откажется показывать картинку вовсе.
    assert foto.headers["content-type"].startswith("image/"), foto.headers["content-type"]

    dok = root_client.get(
        f"{TG}/chats/{dialog['id']}/messages/{po_vidu['document']}/file"
    )
    assert dok.status_code == 200, dok.text
    assert dok.headers["content-disposition"].startswith("attachment"), (
        f"документ показывается вместо сохранения: {dok.headers['content-disposition']}"
    )


# --- прочтение у открытого диалога -------------------------------------------


def test_dochityvanie_ne_dvigaet_granitsu_a_otmetka_dvigaet(root_client, bot_nastroen):
    """Разделение сделано нарочно, и обе половины проверяются вместе.

    Дочитывание (`after=`) приносит сообщения в том числе в СВЁРНУТУЮ вкладку —
    засчитывать их прочитанными нельзя, иначе вернувшийся не увидит, что
    пропустил. А когда на вкладку смотрят, счётчик у открытого диалога обязан
    сниматься: иначе менеджер читает сообщение и одновременно видит слева от
    него «1», снять которую можно только уйдя и вернувшись.

    Различает эти два случая только браузер, поэтому граница двигается
    отдельной ручкой, а не самим дочитыванием.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(505700, 1, text="первое"))
    dialog = _dialog(root_client, 505700)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert _dialog(root_client, 505700)["unread"] == 0

    _poslat(root_client, bot_nastroen, _obnovlenie(505700, 2, text="второе"))
    svezhee = root_client.get(
        f"{TG}/chats/{dialog['id']}/messages?after={lenta[-1]['id']}"
    ).json()["items"]
    assert len(svezhee) == 1, f"дочитывание вернуло не то: {svezhee}"
    assert _dialog(root_client, 505700)["unread"] == 1, (
        "дочитывание засчитало прочитанным то, чего человек мог не видеть"
    )

    otvet = root_client.post(
        f"{TG}/chats/{dialog['id']}/read", json={"up_to": svezhee[-1]["id"]}
    )
    assert otvet.status_code == 200, otvet.text
    assert _dialog(root_client, 505700)["unread"] == 0, "отметка не сняла счётчик"


def test_otstavshaya_otmetka_ne_voskreshaet_prochitannoe(root_client, bot_nastroen):
    """Отметка с меньшим номером границу назад не двигает.

    Запросы приходят не в том порядке, в каком ушли, и отставший «прочитано до
    пятого» после «до седьмого» вернул бы двум сообщениям вид непрочитанных.
    Человек пошёл бы смотреть то, что уже читал, — и перестал бы верить
    счётчику вовсе.
    """
    for nomer in range(1, 4):
        _poslat(root_client, bot_nastroen, _obnovlenie(505800, nomer, text=f"№{nomer}"))
    dialog = _dialog(root_client, 505800)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert _dialog(root_client, 505800)["unread"] == 0

    root_client.post(
        f"{TG}/chats/{dialog['id']}/read", json={"up_to": lenta[0]["id"]}
    )
    assert _dialog(root_client, 505800)["unread"] == 0, (
        "отставшая отметка сдвинула границу назад"
    )


def test_otmetka_v_chuzhom_dialoge_otvergaetsya(root_client, bot_nastroen):
    """Несуществующий диалог — отказ, а не тихое «ок».

    Тихое «ок» на неизвестный номер означает ключ в Redis, который никому не
    принадлежит и не истекает по смыслу, а живёт месяц просто так.
    """
    otvet = root_client.post(f"{TG}/chats/999777/read", json={"up_to": 1})
    assert otvet.status_code == 404, otvet.text


# --- аватар собеседника ------------------------------------------------------


def _avatar_podstavlen(monkeypatch, snimki, skachano=b"kartinka"):
    """Подменить разговор с телеграмом и считать обращения."""
    from core.services import telegram_service

    zvali = []

    def vyzov(kluch, metod, dannye=None, opener=None):
        zvali.append(metod)
        if metod == "getUserProfilePhotos":
            return {"photos": snimki}
        raise AssertionError(f"неожиданный вызов {metod}")

    monkeypatch.setattr(telegram_service, "_vyzov", vyzov)
    monkeypatch.setattr(
        telegram_service,
        "skachat_fayl",
        lambda kluch, file_id, opener=None, srok=None: skachano,
    )
    return zvali


def _zabyt_kogda_sprashivali(chat_row_id: int) -> None:
    """Состарить отметку: как будто сутки прошли."""
    from database.repositories import telegram as telegram_repo
    from database.session import SessionLocal

    with SessionLocal() as db:
        stroka = telegram_repo.get_chat(db, chat_row_id)
        stroka.avatar_checked_at = None
        db.commit()


def test_avatar_beryotsya_pri_priyome_i_ne_chashche_raza_v_sutki(
    root_client, bot_nastroen, monkeypatch
):
    """Аватар забирается при входящем сообщении, а второе за сутки не тянет ничего.

    **Почему при приёме, а не при показе.** Ручка показа при первом же открытии
    списка просит до сотни аватаров разом: соединение к базе держится всё время
    разговора с телеграмом, пул в десять соединений занимается целиком, и в
    «QueuePool limit reached» упирается весь CRM, включая проверку здоровья, на
    которой обновление откатывается. Телеграм на сотню вызовов подряд отвечает
    429 и придерживает бота — тогда перестают ходить не аватары, а сообщения.

    В приёме частоту ограничивает сама переписка.
    """
    zvali = _avatar_podstavlen(
        monkeypatch, [[{"file_id": "AV-1", "width": 160, "height": 160}]]
    )

    _poslat(root_client, bot_nastroen, _obnovlenie(508100, 1, text="привет"))
    assert zvali == ["getUserProfilePhotos"], f"вызовов при первом сообщении: {zvali}"

    dialog = _dialog(root_client, 508100)
    assert dialog["has_avatar"] is True, "аватар забрали, а признак его не показывает"

    kartinka = root_client.get(f"{TG}/chats/{dialog['id']}/avatar")
    assert kartinka.status_code == 200, kartinka.text
    assert kartinka.content == b"kartinka"
    assert kartinka.headers["content-type"].startswith("image/")

    # Второе сообщение в тех же сутках — ни одного нового обращения.
    _poslat(root_client, bot_nastroen, _obnovlenie(508100, 2, text="ещё"))
    assert zvali == ["getUserProfilePhotos"], f"за аватаром сходили повторно: {zvali}"


def test_ruchka_avatara_ne_hodit_v_telegram(root_client, bot_nastroen, monkeypatch):
    """Показ аватара — чтение файла с диска, и ничего больше.

    Сторож на то самое решение: любое обращение к телеграму из этой ручки
    возвращает первую беду — стадо запросов на открытии списка, занятый пул и
    429 в ответ.
    """
    from core.services import telegram_service

    _avatar_podstavlen(monkeypatch, [[{"file_id": "AV-7", "width": 160, "height": 160}]])
    _poslat(root_client, bot_nastroen, _obnovlenie(508700, 1, text="привет"))
    dialog = _dialog(root_client, 508700)

    def nelzya(*a, **k):
        raise AssertionError("ручка показа пошла в телеграм")

    monkeypatch.setattr(telegram_service, "_vyzov", nelzya)
    monkeypatch.setattr(telegram_service, "skachat_fayl", nelzya)

    otvet = root_client.get(f"{TG}/chats/{dialog['id']}/avatar")
    assert otvet.status_code == 200, otvet.text
    assert otvet.content == b"kartinka"


def test_otsutstvie_avatara_ne_zapiraet_dialog_navsegda(
    root_client, bot_nastroen, monkeypatch
):
    """Спросили, не нашли — но завтра спросим снова.

    Самая дорогая ошибка первой редакции пряталась именно здесь. Признак
    «стоит ли просить картинку» включал в себя «ещё не спрашивали», а забор жил
    в ручке показа. Стоило один раз не найти аватар — признак становился
    ложным, экран переставал звать ручку, а звать было ЕДИНСТВЕННЫМ способом
    обновить. Собеседник, поставивший фотографию назавтра, оставался без лица
    навсегда, и починить это можно было только правкой строки в базе.
    """
    zvali = _avatar_podstavlen(monkeypatch, [])

    _poslat(root_client, bot_nastroen, _obnovlenie(508200, 1, text="привет"))
    dialog = _dialog(root_client, 508200)
    assert zvali == ["getUserProfilePhotos"]
    assert dialog["has_avatar"] is False, "нечего показывать, а признак зовёт за картинкой"

    otkaz = root_client.get(f"{TG}/chats/{dialog['id']}/avatar")
    assert otkaz.status_code == 404, otkaz.text
    assert otkaz.json()["error"]["code"] == "telegram_no_avatar"

    # В тех же сутках больше не спрашиваем.
    _poslat(root_client, bot_nastroen, _obnovlenie(508200, 2, text="ещё"))
    assert zvali == ["getUserProfilePhotos"], f"спросили повторно в тех же сутках: {zvali}"

    # А назавтра — спрашиваем, и найденное показывается.
    _zabyt_kogda_sprashivali(dialog["id"])
    _avatar_podstavlen(monkeypatch, [[{"file_id": "AV-9", "width": 160, "height": 160}]], b"pozdnyaya")
    _poslat(root_client, bot_nastroen, _obnovlenie(508200, 3, text="поставил фото"))

    assert _dialog(root_client, 508200)["has_avatar"] is True, (
        "аватар, поставленный позже, не появился — диалог заперт навсегда"
    )
    svezhiy = root_client.get(f"{TG}/chats/{dialog['id']}/avatar")
    assert svezhiy.status_code == 200, svezhiy.text
    assert svezhiy.content == b"pozdnyaya"


def test_otkaz_telegrama_po_avataru_ne_teryaet_soobshchenie(
    root_client, bot_nastroen, monkeypatch
):
    """Телеграм не ответил про аватар — сообщение всё равно принято и записано.

    Аватар — украшение поверх переписки. Уронить из-за него приём значило бы
    обменять важное на неважное: телеграм счёл бы доставку неудавшейся и начал
    повторять.
    """
    from core.services import telegram_service

    def padaet(kluch, metod, dannye=None, opener=None):
        raise telegram_service.TelegramOtkaz("телеграм молчит")

    monkeypatch.setattr(telegram_service, "_vyzov", padaet)

    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(508300, 1, text="привет"))
    assert otvet.status_code == 200, otvet.text

    dialog = _dialog(root_client, 508300)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["привет"], "сообщение потерялось из-за аватара"


def test_svezhiy_avatar_zamenyaet_staryy_a_ne_kopitsya(root_client, bot_nastroen, monkeypatch):
    """Файл аватара один на диалог.

    Случайное имя, как у вложений, означало бы новую копию на диске раз в сутки
    на каждого собеседника: у полусотни диалогов это восемнадцать тысяч файлов в
    год, и ни один из них не нужен.
    """
    from config.settings import get_settings

    _avatar_podstavlen(monkeypatch, [[{"file_id": "AV-2", "width": 160, "height": 160}]])
    _poslat(root_client, bot_nastroen, _obnovlenie(508400, 1, text="привет"))
    dialog = _dialog(root_client, 508400)

    katalog = get_settings().storage_dir / "telegram" / "508400"
    assert [p.name for p in katalog.iterdir()] == ["avatar.jpg"], (
        f"в каталоге диалога лишние файлы: {[p.name for p in katalog.iterdir()]}"
    )

    _zabyt_kogda_sprashivali(dialog["id"])
    _avatar_podstavlen(monkeypatch, [[{"file_id": "AV-3", "width": 160, "height": 160}]], b"novaya")
    _poslat(root_client, bot_nastroen, _obnovlenie(508400, 2, text="сменил фото"))

    assert [p.name for p in katalog.iterdir()] == ["avatar.jpg"], "аватары копятся на диске"
    svezhiy = root_client.get(f"{TG}/chats/{dialog['id']}/avatar")
    assert svezhiy.content == b"novaya", "показывается старый аватар вместо нового"



# --- ответ на конкретное сообщение -------------------------------------------


def test_otvet_privyazyvaetsya_i_uhodit_s_privyazkoy(root_client, bot_nastroen, monkeypatch):
    """Ответ помнит, на что он ответ, — и телеграм узнаёт об этом тоже.

    Обе половины в одном теле: привязка, которая есть у нас и не уехала в
    телеграм, показывает цитату менеджеру и НЕ показывает клиенту — то есть
    ровно та беда, ради которой всё и делалось, остаётся у него на экране.
    """
    from core.services import telegram_service

    ushlo = {}

    def poslat(kluch, chat_id, text, opener=None, otvet_na=None):
        ushlo["otvet_na"] = otvet_na
        return {"message_id": 991}

    monkeypatch.setattr(telegram_service, "poslat_tekst", poslat)

    _poslat(root_client, bot_nastroen, _obnovlenie(509100, 41, text="сколько стоит доставка?"))
    dialog = _dialog(root_client, 509100)
    vopros = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"][0]

    otvet = root_client.post(
        f"{TG}/chats/{dialog['id']}/messages",
        json={"text": "двести гривен", "reply_to_id": vopros["id"]},
    )
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["reply_to_id"] == vopros["id"], "привязка не сохранилась"
    assert ushlo["otvet_na"] == 41, (
        f"в телеграм ушёл не тот номер: {ushlo}. Клиент увидит ответ без цитаты"
    )


def test_otvet_na_chuzhoe_soobshchenie_otvergaetsya(root_client, bot_nastroen, monkeypatch):
    """Привязать ответ к сообщению ЧУЖОГО диалога нельзя.

    Номер сообщения приходит из браузера, и подставить в него чужой — дело
    одной строки. Без проверки цитата в ленте показала бы слова другого
    клиента: не «неверная ссылка», а чужая переписка на экране.
    """
    from core.services import telegram_service

    monkeypatch.setattr(
        telegram_service,
        "poslat_tekst",
        lambda kluch, chat_id, text, opener=None, otvet_na=None: {"message_id": 992},
    )

    _poslat(root_client, bot_nastroen, _obnovlenie(509200, 51, text="первый диалог"))
    _poslat(root_client, bot_nastroen, _obnovlenie(509300, 52, text="второй диалог"))
    pervyy = _dialog(root_client, 509200)
    vtoroy = _dialog(root_client, 509300)
    chuzhoe = root_client.get(f"{TG}/chats/{vtoroy['id']}/messages").json()["items"][0]

    otkaz = root_client.post(
        f"{TG}/chats/{pervyy['id']}/messages",
        json={"text": "ответ не туда", "reply_to_id": chuzhoe["id"]},
    )
    assert otkaz.status_code == 422, otkaz.text
    assert otkaz.json()["error"]["code"] == "telegram_reply_not_here"


def test_vhodyashchiy_otvet_klienta_zapominaet_na_chto_otvechayut(root_client, bot_nastroen):
    """Клиент ответил на наше сообщение — привязка сохраняется и у нас.

    Без неё переписка в CRM читается иначе, чем у клиента в телефоне: там
    цитата есть, здесь нет. Разговор про три позиции заказа так расходится на
    два разных разговора.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(509400, 61, text="какой из трёх вариантов?"))
    dialog = _dialog(root_client, 509400)
    nashe = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"][0]

    telo = _obnovlenie(509400, 62, text="второй")
    telo["message"]["reply_to_message"] = {"message_id": 61}
    _poslat(root_client, bot_nastroen, telo)

    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    otvet_klienta = [s for s in lenta if s["body"] == "второй"][0]
    assert otvet_klienta["reply_to_id"] == nashe["id"], (
        f"привязка входящего ответа потерялась: {otvet_klienta}"
    )


def test_otvet_na_neizvestnoe_soobshchenie_ne_teryaet_samo_soobshchenie(
    root_client, bot_nastroen
):
    """Клиент ответил на то, чего у нас нет, — сообщение всё равно записано.

    Такое бывает законно: переписка началась до подключения канала, и первых
    сообщений у нас нет вовсе. Потерять из-за этого ответ клиента значило бы
    потерять сам разговор.
    """
    telo = _obnovlenie(509500, 71, text="да, согласен")
    telo["message"]["reply_to_message"] = {"message_id": 999999}
    otvet = _poslat(root_client, bot_nastroen, telo)
    assert otvet.status_code == 200, otvet.text

    dialog = _dialog(root_client, 509500)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["да, согласен"]
    assert lenta[0]["reply_to_id"] is None, "привязка выдумана на пустом месте"



# --- именной эмодзи -----------------------------------------------------------


def _telegram_podstavlen(monkeypatch, otvety: dict, skachano=b"kartinka"):
    """Подменить разговор с телеграмом словарём «метод → ответ» и считать вызовы."""
    from core.services import telegram_service

    zvali = []

    def vyzov(kluch, metod, dannye=None, opener=None):
        zvali.append(metod)
        if metod in otvety:
            return otvety[metod]
        raise AssertionError(f"неожиданный вызов {metod}")

    monkeypatch.setattr(telegram_service, "_vyzov", vyzov)
    monkeypatch.setattr(
        telegram_service,
        "skachat_fayl",
        lambda kluch, file_id, opener=None, srok=None: skachano,
    )
    return zvali


def test_imennoy_emodzi_beryotsya_kartinkoy_i_pokazyvaetsya(
    root_client, bot_nastroen, monkeypatch
):
    """У премиума забирается СТАТИЧНАЯ миниатюра значка и отдаётся экрану.

    Сам значок — стикер в формате TGS (сжатый Lottie), и браузер его не рисует:
    ради анимации понадобилась бы новая библиотека во фронтенде. Миниатюра
    рисуется обычной картинкой, и значок при этом тот самый, включая платные
    наборы.
    """
    zvali = _telegram_podstavlen(
        monkeypatch,
        {
            "getUserProfilePhotos": {"photos": []},
            "getChat": {"emoji_status_custom_emoji_id": "5350305076983339640"},
            "getCustomEmojiStickers": [{"thumbnail": {"file_id": "TH-1"}}],
        },
        skachano=b"znachok",
    )

    telo = _obnovlenie(510100, 81, text="привет")
    telo["message"]["from"]["is_premium"] = True
    _poslat(root_client, bot_nastroen, telo)

    assert "getChat" in zvali and "getCustomEmojiStickers" in zvali, f"вызовы: {zvali}"

    dialog = _dialog(root_client, 510100)
    assert dialog["is_premium"] is True
    assert dialog["has_emoji"] is True, f"значок забрали, а признак молчит: {dialog}"

    kartinka = root_client.get(f"{TG}/chats/{dialog['id']}/emoji")
    assert kartinka.status_code == 200, kartinka.text
    assert kartinka.content == b"znachok"


def test_u_ne_premiuma_za_emodzi_ne_hodim(root_client, bot_nastroen, monkeypatch):
    """Без премиума именного эмодзи не бывает — и двух вызовов ради него тоже.

    Иначе каждый диалог стоил бы двух лишних обращений к Bot API в сутки при
    заведомо пустом ответе, а телеграм считает вызовы, а не их полезность.
    """
    zvali = _telegram_podstavlen(monkeypatch, {"getUserProfilePhotos": {"photos": []}})

    _poslat(root_client, bot_nastroen, _obnovlenie(510200, 82, text="привет"))

    assert "getChat" not in zvali, f"сходили за значком к обычному человеку: {zvali}"
    assert _dialog(root_client, 510200)["has_emoji"] is False


def test_snyatyy_emodzi_propadaet_i_u_nas(root_client, bot_nastroen, monkeypatch):
    """Убрал значок — он пропадает и в CRM.

    Иначе он висел бы у имени вечно: показ читается как «вот его значок», а не
    «вот его значок по состоянию на прошлый год».
    """
    _telegram_podstavlen(
        monkeypatch,
        {
            "getUserProfilePhotos": {"photos": []},
            "getChat": {"emoji_status_custom_emoji_id": "77"},
            "getCustomEmojiStickers": [{"thumbnail": {"file_id": "TH-2"}}],
        },
    )
    telo = _obnovlenie(510300, 83, text="привет")
    telo["message"]["from"]["is_premium"] = True
    _poslat(root_client, bot_nastroen, telo)
    dialog = _dialog(root_client, 510300)
    assert dialog["has_emoji"] is True

    # Назавтра значок снят.
    _zabyt_kogda_sprashivali(dialog["id"])
    _telegram_podstavlen(
        monkeypatch,
        {"getUserProfilePhotos": {"photos": []}, "getChat": {}},
    )
    telo = _obnovlenie(510300, 84, text="снял значок")
    telo["message"]["from"]["is_premium"] = True
    _poslat(root_client, bot_nastroen, telo)

    assert _dialog(root_client, 510300)["has_emoji"] is False, "снятый значок остался висеть"
    assert root_client.get(f"{TG}/chats/{dialog['id']}/emoji").status_code == 404



# --- выключение блока на всех уровнях ----------------------------------------


def test_vyklyuchennyy_blok_zakryt_na_vseh_svoih_urovnyah(root_client, bot_nastroen):
    """Выключенный блок исчезает целиком, а не «в основном».

    `docs/osnovy/11-bloki-i-svyaznost.md` называет семь уровней, и пропущенный делает выключение
    косметическим: пункт спрятан, а адрес работает; экран убран, а настройки
    открыты. Здесь проверяются те уровни, которые проверяемы с сервера, — по
    одному запросу на каждый. Уровни интерфейса (меню, маршруты SPA) держатся
    списками в `Sidebar.tsx` и `App.tsx` и проверяются `tests/test_layout.py`.

    Приём вебхука в этот список НЕ входит намеренно: телеграм не умеет узнать,
    что канал выключили, и отвечать ему отказом значило бы копить у него очередь
    повторов, которая вывалится разом при включении. Он отвечает «принято» и
    ничего не делает — это отдельная проверка ниже.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(511100, 91, text="привет"))
    dialog = _dialog(root_client, 511100)

    otklyuchen = root_client.post(f"{API}/modules/telegram", json={"enabled": False})
    assert otklyuchen.status_code == 200, otklyuchen.text

    # По ручке на уровень: настройки, список диалогов, лента, отправка, поток,
    # вложение, аватар, сводка, присутствие, отметка прочтения.
    adresa = [
        ("GET", f"{TG}/settings"),
        ("GET", f"{TG}/chats"),
        ("GET", f"{TG}/invite"),
        ("GET", f"{TG}/chats/{dialog['id']}/messages"),
        ("POST", f"{TG}/chats/{dialog['id']}/messages"),
        ("POST", f"{TG}/chats/{dialog['id']}/presence"),
        ("POST", f"{TG}/chats/{dialog['id']}/read"),
        ("GET", f"{TG}/chats/{dialog['id']}/avatar"),
        ("GET", f"{TG}/chats/{dialog['id']}/emoji"),
        ("POST", f"{TG}/digest/send"),
        ("PATCH", f"{TG}/chats/{dialog['id']}"),
        ("POST", f"{TG}/chats/{dialog['id']}/deal"),
        ("POST", f"{TG}/chats/{dialog['id']}/task"),
    ]
    for metod, adres in adresa:
        otvet = getattr(root_client, metod.lower())(
            adres, **({"json": {}} if metod in ("POST", "PATCH") else {})
        )
        assert otvet.status_code == 403, f"{metod} {adres} отвечает {otvet.status_code}"
        assert otvet.json()["error"]["code"] == "module_disabled", f"{metod} {adres}"

    root_client.post(f"{API}/modules/telegram", json={"enabled": True})


def test_pri_vyklyuchennom_bloke_slova_klienta_ne_propadayut(root_client, bot_nastroen):
    """Блок выключен, а сообщение клиента записано — и находится после включения.

    Отказывать телеграму нельзя: он сочтёт доставку неудавшейся и будет
    повторять часами, а при включении вывалит накопленное разом.

    Но и «принять и выбросить» нельзя, хотя соблазн есть: раздела нет, значит
    и записи не надо. Со стороны клиента это выглядит так — написал, телеграм
    показал «доставлено», ответа нет и не будет, а слов его нет нигде. Хуже
    этого здесь ничего не бывает, и происходит оно молча.

    Поэтому приём пишет, а показывать записанное некому: раздел закрыт целиком
    (соседняя проверка). Перестать принимать — отдельное явное действие,
    `DELETE /telegram/settings` снимает вебхук.
    """
    root_client.post(f"{API}/modules/telegram", json={"enabled": False})
    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(511200, 92, text="пока выключено"))
    assert otvet.status_code == 200, otvet.text

    # Пока блок выключен, ручки закрыты — даже своему.
    assert root_client.get(f"{TG}/chats").status_code == 403

    root_client.post(f"{API}/modules/telegram", json={"enabled": True})
    dialog = _dialog(root_client, 511200)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["пока выключено"], (
        "слова клиента, написанные при выключенном блоке, потерялись"
    )


def test_dannye_perepiski_perezhivayut_vyklyuchenie(root_client, bot_nastroen):
    """Выключение — про связь, а не про данные.

    Правило блоков прямо говорит: данные при выключении не стираются. Переписка
    с клиентом бывает доказательством, и потерять её из-за переключателя нельзя.
    """
    _poslat(root_client, bot_nastroen, _obnovlenie(511300, 93, text="сохрани меня"))
    dialog = _dialog(root_client, 511300)

    root_client.post(f"{API}/modules/telegram", json={"enabled": False})
    root_client.post(f"{API}/modules/telegram", json={"enabled": True})

    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["сохрани меня"], "переписка не пережила выключение"



# --- нажатия кнопок -----------------------------------------------------------


def _knopka_podstavlena(monkeypatch, pristavka, obrabotchik):
    """Подписать обработчик и убрать его за собой.

    Реестр обработчиков живёт в модуле, а не в запросе: подписка одной проверки
    досталась бы всем следующим, и порядок прогона начал бы значить.
    `monkeypatch` снимает её сам.
    """
    from core.services import telegram_service

    monkeypatch.setitem(telegram_service.NAZHATIYA, pristavka, obrabotchik)


def test_nazhatie_dohodit_do_svoego_obrabotchika(root_client, bot_nastroen, monkeypatch):
    """Кнопку нажали — позвали того, кто её заявил, и погасили часики.

    Ответ телеграму обязателен: пока он его не получил, у нажавшего крутится
    ожидание, и через пару секунд человек жмёт второй раз. Для тревоги это
    означает второго дежурного, побежавшего чинить уже починенное.
    """
    from core.services import telegram_service

    zvali = []
    otvety = []
    _knopka_podstavlena(
        monkeypatch, "proba", lambda db, nazhatie: zvali.append(nazhatie["data"]) or "принято"
    )
    monkeypatch.setattr(
        telegram_service,
        "otvetit_na_nazhatie",
        lambda kluch, callback_id, tekst="", opener=None: otvety.append((callback_id, tekst)),
    )

    otvet = _poslat(
        root_client,
        bot_nastroen,
        {"update_id": 7001, "callback_query": {"id": "cb-1", "data": "proba:42"}},
    )
    assert otvet.status_code == 200, otvet.text
    assert zvali == ["proba:42"], f"обработчик не позвали: {zvali}"
    assert otvety == [("cb-1", "принято")], f"телеграму не ответили: {otvety}"


def test_neizvestnaya_knopka_vsyo_ravno_poluchaet_otvet(root_client, bot_nastroen, monkeypatch):
    """Кнопка из прошлой жизни — не повод молчать.

    Сообщение с кнопками остаётся в чате навсегда, а обработчик после
    обновления может исчезнуть. Молчание в ответ выглядит для человека как
    «нажал и ничего не произошло», и он жмёт снова и снова.
    """
    from core.services import telegram_service

    otvety = []
    monkeypatch.setattr(
        telegram_service,
        "otvetit_na_nazhatie",
        lambda kluch, callback_id, tekst="", opener=None: otvety.append(tekst),
    )

    otvet = _poslat(
        root_client,
        bot_nastroen,
        {"update_id": 7002, "callback_query": {"id": "cb-2", "data": "davno_ushlo:1"}},
    )
    assert otvet.status_code == 200, otvet.text
    assert otvety and otvety[0], f"на неизвестную кнопку не ответили ничем: {otvety}"


def test_upavshiy_obrabotchik_ne_ronyaet_priyom(root_client, bot_nastroen, monkeypatch):
    """Обработчик упал — приём отвечает 200 и говорит нажавшему, что не вышло.

    Пятисотка здесь означала бы, что телеграм считает доставку неудавшейся и
    повторяет нажатие часами. Одно нажатие превратилось бы в поток.
    """
    from core.services import telegram_service

    def padaet(db, nazhatie):
        raise RuntimeError("внутри всё сломалось")

    otvety = []
    _knopka_podstavlena(monkeypatch, "beda", padaet)
    monkeypatch.setattr(
        telegram_service,
        "otvetit_na_nazhatie",
        lambda kluch, callback_id, tekst="", opener=None: otvety.append(tekst),
    )

    otvet = _poslat(
        root_client,
        bot_nastroen,
        {"update_id": 7003, "callback_query": {"id": "cb-3", "data": "beda:9"}},
    )
    assert otvet.status_code == 200, otvet.text
    assert otvety and otvety[0], "нажавшему не сказали, что не вышло"


def test_nazhatie_ne_zavodit_dialog_i_ne_pishet_v_perepisku(root_client, bot_nastroen, monkeypatch):
    """Нажатие — не сообщение: ни диалога, ни строки в ленте от него не остаётся.

    У `callback_query` нет ни текста, ни чата в привычном смысле, и разбирать
    его как сообщение значило бы завести диалог с пустым именем и записать в
    переписку пустоту. Клиент увидел бы в CRM разговор, которого не было.
    """
    from core.services import telegram_service

    monkeypatch.setattr(
        telegram_service,
        "otvetit_na_nazhatie",
        lambda kluch, callback_id, tekst="", opener=None: None,
    )
    bylo = len(root_client.get(f"{TG}/chats").json()["items"])

    _poslat(
        root_client,
        bot_nastroen,
        {
            "update_id": 7004,
            "callback_query": {
                "id": "cb-4",
                "data": "proba:1",
                "from": {"id": 512100, "first_name": "Дежурный"},
                "message": {"message_id": 5, "chat": {"id": 512100, "type": "private"}},
            },
        },
    )

    stalo = root_client.get(f"{TG}/chats").json()["items"]
    assert len(stalo) == bylo, f"нажатие завело диалог: {[d['chat_id'] for d in stalo]}"


def test_dlina_callback_data_pomeshchaetsya_v_predel_telegrama(monkeypatch):
    """64 байта — предел телеграма на `callback_data`, и он не рекомендация.

    Длиннее — отказ ВСЕГО сообщения, а не одной кнопки: тревога не уйдёт вовсе.
    Проверка сторожит собственный помощник: он собирает клавиатуру, и соблазн
    положить в кнопку состояние вместо опознавателя велик.
    """
    from core.services import telegram_service

    ushlo = {}

    def vyzov(kluch, metod, dannye=None, opener=None):
        ushlo.update(dannye or {})
        return {"message_id": 1}

    # Через `monkeypatch`, а НЕ перезагрузкой модуля. Перезагрузка заводит новый
    # словарь `NAZHATIYA`, и подписки, сделанные соседними разделами при ввозе
    # (тревоги подписываются именно так), пропадают до конца прогона. Проверки
    # после этой краснели бы через одну и по порядку запуска.
    monkeypatch.setattr(telegram_service, "_vyzov", vyzov)
    telegram_service.poslat_s_knopkami(
        "token",
        123,
        "тревога",
        [[{"text": "Принято", "callback_data": "ack:12345"}]],
    )

    import json as _json

    razmetka = _json.loads(ushlo["reply_markup"])
    for ryad in razmetka["inline_keyboard"]:
        for knopka in ryad:
            dlina = len(knopka["callback_data"].encode("utf-8"))
            assert dlina <= 64, f"кнопка «{knopka['text']}»: {dlina} байт из 64"



# --- приём ничего не держит и ничего не теряет --------------------------------


def test_vlozhenie_kachaetsya_posle_fiksatsii_soobshcheniya(
    root_client, bot_nastroen, monkeypatch
):
    """К моменту скачивания файла сообщение УЖЕ записано и зафиксировано.

    **Зачем так.** Прежде вложение качалось внутри транзакции, взявшей диалог
    `FOR UPDATE`. Три клиента с фотографиями занимали три соединения из десяти
    на минуту; телеграм, не дождавшись ответа, повторял доставку, повторы
    вставали на тот же замок — и «QueuePool limit» накрывал всё приложение
    вместе с проверкой здоровья, по которой обновление откатывает релиз.

    Проверка смотрит на это единственным честным способом: изнутри скачивания
    спрашивает базу ДРУГИМ соединением. Видно сообщение — значит транзакция
    закрыта, замка нет, соединение свободно.
    """
    from core.services import telegram_service
    from database.repositories import telegram as telegram_repo
    from database.session import SessionLocal

    vidno_so_storony = {}

    def skachat(kluch, file_id, opener=None, srok=None):
        # Другая сессия — то есть другое соединение и другая транзакция.
        with SessionLocal() as chuzhaya:
            dialogi = [
                d for d in telegram_repo.spisok_dialogov(chuzhaya, per_page=100)[0]
                if d.chat_id == 515100
            ]
            vidno_so_storony["dialog"] = bool(dialogi)
            if dialogi:
                lenta = telegram_repo.lenta(chuzhaya, dialogi[0].id)
                vidno_so_storony["soobshcheniy"] = len(lenta)
        return b"kartinka"

    monkeypatch.setattr(telegram_service, "skachat_fayl", skachat)

    telo = _obnovlenie(515100, 101, caption="вот фото")
    telo["message"]["photo"] = [{"file_id": "PH-515", "file_size": 2048}]
    otvet = _poslat(root_client, bot_nastroen, telo)
    assert otvet.status_code == 200, otvet.text

    assert vidno_so_storony.get("dialog") is True, (
        "во время скачивания диалог не виден со стороны — значит транзакция "
        "всё ещё открыта и держит замок с соединением"
    )
    assert vidno_so_storony.get("soobshcheniy", 0) >= 1, (
        "сообщение не зафиксировано до скачивания: упади сеть — и слова клиента "
        "пропадут вместе с его фотографией"
    )

    # И файл в итоге дописан к той же строке, а не потерян.
    dialog = _dialog(root_client, 515100)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert len(lenta) == 1, f"сообщение задвоилось: {lenta}"
    assert lenta[0]["has_file"] is True, "вложение не дописалось к сообщению"


def test_neudavsheesya_vlozhenie_ne_teryaet_soobshchenie(
    root_client, bot_nastroen, monkeypatch
):
    """Файл не забрался — текст клиента всё равно на месте.

    Разрыв на скачивании обычен: 429 от телеграма, оборванная сеть, пропавший
    файл. Потерять из-за этого подпись к фотографии значило бы потерять то, ради
    чего переписка и ведётся.
    """
    from core.services import telegram_service

    def padaet(kluch, file_id, opener=None, srok=None):
        raise telegram_service.TelegramOtkaz("сеть моргнула")

    monkeypatch.setattr(telegram_service, "skachat_fayl", padaet)

    telo = _obnovlenie(515200, 102, caption="фото со сметой")
    telo["message"]["photo"] = [{"file_id": "PH-516", "file_size": 2048}]
    assert _poslat(root_client, bot_nastroen, telo).status_code == 200

    dialog = _dialog(root_client, 515200)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["фото со сметой"]
    assert lenta[0]["has_file"] is False
    # Сессия после отката цела: следующее сообщение записывается как обычно.
    assert _poslat(root_client, bot_nastroen, _obnovlenie(515200, 103, text="ещё")).status_code == 200
    assert len(root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]) == 2


def test_beda_bazy_otvechaet_povtorom_a_ne_prinyato(root_client, bot_nastroen, monkeypatch):
    """Не смогли записать — говорим телеграму «повтори», а не «принято».

    200 на непойманной беде базы означает: повтора не будет, а сообщения нет
    нигде. Замок не дождался, соединение оборвалось, сервер перезапускают —
    всё это преходяще и чинится повтором, который телеграм сделает сам.

    А вот ошибка РАЗБОРА повтором не чинится, и на неё по-прежнему 200: то же
    обновление приедет и разберётся так же, только очередь у телеграма вырастет.
    """
    from sqlalchemy.exc import OperationalError

    from core.services import telegram_service

    def ne_dozhdalsya(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("lock wait timeout"))

    monkeypatch.setattr(telegram_service, "prinyat", ne_dozhdalsya)
    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(515300, 104, text="привет"))
    assert otvet.status_code == 503, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_store_failed"

    def razbor_slomalsya(*a, **k):
        raise KeyError("неожиданное поле")

    monkeypatch.setattr(telegram_service, "prinyat", razbor_slomalsya)
    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(515300, 105, text="привет"))
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["status"] == "error"


def test_priyom_zhivyot_bez_ogranichitelya(root_client, bot_nastroen, monkeypatch):
    """Redis лёг — переписка с клиентами продолжается.

    Ограничитель на этой ручке сторожит ПОДБОР секрета, а не поток сообщений.
    Закрывать из-за него приём значило бы отбивать 503 всем клиентам разом:
    телеграм копит повторы часами и, не дождавшись, теряет их. Подбор при этом
    не дешевеет — секрет сверяется постоянным временем и с одной попытки не
    угадывается.
    """
    from core import exceptions as errors
    from web.api.routes import telegram as ruchka

    def net_redisa(adres):
        raise errors.LimiterUnavailableError("Redis не отвечает")

    monkeypatch.setattr(ruchka.webhook_limiter, "is_blocked", net_redisa)

    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(515400, 106, text="слышно?"))
    assert otvet.status_code == 200, otvet.text
    dialog = _dialog(root_client, 515400)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["слышно?"]


def test_odnovremennoe_pervoe_soobshchenie_ne_dvoit_dialog(root_client, bot_nastroen, monkeypatch):
    """Два первых обновления нового клиента заводят ОДИН диалог.

    `FOR UPDATE` по несуществующей строке не запирает ничего: оба обновления
    видят «чата нет» и оба идут его заводить. Первое сообщение нового клиента
    приходит именно так — «/start» и вопрос двумя обновлениями подряд.

    Проверка воспроизводит гонку честно: подменяет чтение так, что первый заход
    «не видит» уже заведённую строку, — и требует, чтобы приём поднял чужую
    запись, а не упал на уникальности `chat_id`.
    """
    from database.repositories import telegram as telegram_repo
    from core.services import telegram_service

    nastoyashchee = telegram_repo.vzyat_pod_pravku
    slepykh = {"ostalos": 1}

    def slepoy(db, chat_id):
        # Первый заход после появления чужой строки притворяется, что её нет.
        if slepykh["ostalos"] > 0 and chat_id == 515500:
            slepykh["ostalos"] -= 1
            return None
        return nastoyashchee(db, chat_id)

    _poslat(root_client, bot_nastroen, _obnovlenie(515500, 107, text="/start"))
    monkeypatch.setattr(telegram_service.telegram_repo, "vzyat_pod_pravku", slepoy)

    otvet = _poslat(root_client, bot_nastroen, _obnovlenie(515500, 108, text="а сколько стоит?"))
    assert otvet.status_code == 200, otvet.text

    dialog = _dialog(root_client, 515500)  # сам по себе требует РОВНО одного
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert [s["body"] for s in lenta] == ["/start", "а сколько стоит?"], (
        f"второе сообщение нового клиента потерялось: {lenta}"
    )


def test_nomera_chatov_u_proverok_ne_peresekayutsya():
    """Один номер телеграм-чата — одна проверка. Иначе они молча портят друг друга.

    База у набора одна на весь прогон, и диалог, заведённый соседней проверкой,
    остаётся. Хуже того: `prinyat` защищён от повторной доставки по паре
    «диалог + номер сообщения», и вторая проверка с тем же номером чата и тем же
    `message_id` получает не свои данные, а тихое «duplicate» — её собственное
    сообщение НЕ записывается вовсе.

    Так и вышло с проверками аватара: поодиночке зелёные, в полном наборе
    красные, потому что номера 506100–506300 уже заняты отбором по метке
    источника. Разбирать такое приходится по вычитанию, поэтому проверка
    механическая.
    """
    import pathlib
    import re

    text = pathlib.Path(__file__).read_text(encoding="utf-8")

    # Разбираем файл на проверки и смотрим, какие номера чатов каждая занимает.
    kuski = re.split(r"\ndef (test_\w+)", text)
    zanyato: dict[str, set[str]] = {}
    for i in range(1, len(kuski), 2):
        imya, telo = kuski[i], kuski[i + 1]
        nomera = set(re.findall(r"_obnovlenie\((\d{5,})", telo))
        nomera |= set(re.findall(r"_dialog\(root_client, (\d{5,})", telo))
        if nomera:
            zanyato[imya] = nomera

    assert len(zanyato) >= 10, f"проверок с номерами разобрано {len(zanyato)} — смотрим не туда"

    chey: dict[str, list[str]] = {}
    for imya, nomera in zanyato.items():
        for nomer in nomera:
            chey.setdefault(nomer, []).append(imya)

    delyat = {n: sorted(kto) for n, kto in chey.items() if len(kto) > 1}
    assert not delyat, (
        "один номер чата занят несколькими проверками — они будут портить друг "
        "друга в полном наборе:\n  "
        + "\n  ".join(f"{n}: {', '.join(kto)}" for n, kto in sorted(delyat.items()))
    )


def test_spisok_dialogov_listaetsya_do_kontsa(root_client, bot_nastroen):
    """Пройдя список страницами, доходим ровно до всех диалогов — без дыр и повторов.

    На это опирается экран. Он показывает первую страницу и просит следующую,
    пока показано меньше, чем сказано в `total`; значит вранья в `total` или
    сдвига страниц он не переживёт — и молча покажет не всех.

    Именно с потолка всё и началось: экран просил сотню и на этом заканчивался,
    а спросить продолжение было нечем. Сервер листать умел всегда, поэтому здесь
    закрепляется его сторона уговора — `total`, `page` и непересекающиеся
    страницы.

    База у набора одна на весь прогон, поэтому «всего» здесь чужое пополам со
    своим: проверяется полнота обхода, а свои диалоги ищутся в собранном по
    номеру чата.
    """
    skolko = 12
    for i in range(skolko):
        otvet = _poslat(
            root_client,
            bot_nastroen,
            _obnovlenie(520000 + i, 900 + i, text="здравствуйте"),
        )
        assert otvet.status_code == 200, otvet.text

    # Свои узнаём по chat_id: имя `_obnovlenie` кладёт всем одно.
    nashi = set(range(520000, 520000 + skolko))
    sobrano: list[int] = []
    vsego = None
    stranitsa = 1
    while True:
        otvet = root_client.get(f"{TG}/chats?page={stranitsa}&per_page=5").json()
        assert otvet["page"] == stranitsa, f"сервер отдал не ту страницу: {otvet['page']}"
        assert otvet["per_page"] == 5
        if vsego is None:
            vsego = otvet["total"]
        else:
            assert otvet["total"] == vsego, "«всего» пляшет между страницами"
        sobrano += [d["chat_id"] for d in otvet["items"]]
        if not otvet["items"] or len(sobrano) >= vsego:
            break
        stranitsa += 1
        assert stranitsa < 200, "листание не заканчивается"

    assert len(sobrano) == len(set(sobrano)), "страницы пересекаются — диалог встретился дважды"
    assert len(sobrano) == vsego, f"пройдено {len(sobrano)} из {vsego} — в листании дыра"
    poteryany = nashi - set(sobrano)
    assert not poteryany, f"листание не дошло до диалогов {sorted(poteryany)}"


def test_istochniki_nazyvayut_sebya_i_schitayutsya(root_client, bot_nastroen):
    """Экран узнаёт, какие метки есть и по сколько диалогов на каждой.

    Отбор по метке ручка диалогов принимала и раньше, но список меток спросить
    было нечем: их придумывает владелец, раздавая наклейки и ссылки, и через
    полгода сам не помнит полного списка. Отбор без списка можно предложить
    только полем ввода — то есть предложить угадывать.

    Счёт проверяется вместе с составом: он и есть ответ на вопрос, ради
    которого метки заводят. Метка без счёта — это просто слово.
    """
    for nomer, metka in ((521000, "vitrina"), (521001, "vitrina"), (521002, "kvitanciya")):
        otvet = _poslat(root_client, bot_nastroen, _obnovlenie(nomer, 950 + nomer % 100,
                                                              text=f"/start {metka}"))
        assert otvet.status_code == 200, otvet.text
    # Диалог без метки: «пришёл сам» — не источник, и в списке ему не место.
    _poslat(root_client, bot_nastroen, _obnovlenie(521003, 953, text="здравствуйте"))

    spisok = root_client.get(f"{TG}/sources").json()["items"]
    schyot = {stroka["source"]: stroka["count"] for stroka in spisok}
    assert schyot.get("vitrina") == 2, f"витрина посчитана неверно: {spisok}"
    assert schyot.get("kvitanciya") == 1, f"квитанция посчитана неверно: {spisok}"
    assert "" not in schyot, "диалог без метки попал в список источников"

    # Частые сверху: владелец смотрит на этот список, чтобы понять, что
    # работает, и первая строка обязана отвечать на это без вчитывания.
    mesta = [stroka["source"] for stroka in spisok]
    assert mesta.index("vitrina") < mesta.index("kvitanciya"), (
        f"источники идут не по убыванию счёта: {spisok}"
    )


def test_otbor_po_istochniku_otseivaet_chuzhih(root_client, bot_nastroen):
    """Отбор по метке отдаёт ровно свои диалоги — и «всего» считает по ним же.

    Второе не менее важно первого: экран показывает первую страницу и просит
    следующую, пока показанных меньше, чем сказано в `total`. Посчитай сервер
    «всего» без отбора — и человек ушёл бы листать пустоту.
    """
    for nomer in (521100, 521101):
        _poslat(root_client, bot_nastroen, _obnovlenie(nomer, 960 + nomer % 100,
                                                      text="/start yarmarka"))

    otvet = root_client.get(f"{TG}/chats?source=yarmarka&per_page=50").json()
    metki = {dialog["source"] for dialog in otvet["items"]}
    assert metki == {"yarmarka"}, f"в отбор попали чужие метки: {metki}"
    assert otvet["total"] == len(otvet["items"]) == 2, (
        f"«всего» посчитано не по отбору: {otvet['total']} при {len(otvet['items'])} строках"
    )


def test_fayl_uhodit_s_podpisyu_i_otvetom(root_client, bot_nastroen, monkeypatch):
    """Подпись и «в ответ на» едут вместе с файлом, а не следом.

    Отдельным сообщением подпись отправить нельзя — у клиента в телеграме это
    будет ВТОРОЕ сообщение, и картинка придёт голой, неизвестно к чему. Ручка
    принимала оба поля с самого начала; экран их не отправлял, и набранный
    текст молча оставался в поле.

    Проверяется то, что дошло до самого телеграма, а не то, что записано в
    переписку: записать можно и не отправив.
    """
    from core.services import telegram_service

    _poslat(root_client, bot_nastroen, _obnovlenie(521200, 970, text="а покажите"))
    dialog = _dialog(root_client, 521200)
    lenta = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    vopros = lenta[-1]["id"]

    # Доводы разбираем по подписи настоящей функции, а не по `kwargs`: подпись
    # к файлу едет позиционным доводом, и проверка, смотревшая только в
    # `kwargs`, увидела бы пустоту и на исправном коде.
    import inspect

    obraz = inspect.signature(telegram_service.poslat_fayl)
    ushlo = {}

    def zapomnit(*a, **kw):
        ushlo.update(obraz.bind(*a, **kw).arguments)
        return {"message_id": 7}

    monkeypatch.setattr(telegram_service, "poslat_fayl", zapomnit)

    otvet = root_client.post(
        f"{TG}/chats/{dialog['id']}/files",
        files={"file": ("shema.png", b"x" * 64, "image/png")},
        data={"caption": "вот эта деталь", "reply_to_id": str(vopros)},
    )
    assert otvet.status_code == 201, otvet.text

    assert ushlo.get("podpis") == "вот эта деталь", (
        f"подпись не доехала до телеграма: {ushlo}"
    )
    assert ushlo.get("otvet_na") is not None, f"«в ответ на» потерялось: {ushlo}"

    # И в переписке сообщение стоит ответом на тот же вопрос — иначе менеджер
    # видит одно, а клиент получил другое.
    stalo = root_client.get(f"{TG}/chats/{dialog['id']}/messages").json()["items"]
    assert stalo[-1]["reply_to_id"] == vopros, f"в переписке ответ не привязан: {stalo[-1]}"
