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
