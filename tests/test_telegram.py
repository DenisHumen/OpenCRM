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
        lambda kluch, chat_id, text, opener=None: {"message_id": 900},
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
