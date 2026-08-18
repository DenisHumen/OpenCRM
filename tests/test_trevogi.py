"""Тревоги мониторинга с кнопками: доставка, нажатия, тишина.

Проверяется то, чего не видно ни глазами, ни на стенде за один заход:
**поведение при ПОВТОРЕ**. Повтор здесь не редкость, а устройство канала.
Alertmanager повторяет доставку сам — раз в час у критического, раз в сутки у
предупреждения, плюс лишний раз при каждом перечитывании конфига; кнопку в
служебном чате жмут двое, потому что оба увидели одно сообщение; телеграм
отбивает отправку, и всё начинается заново.

Каждый из этих случаев по отдельности выглядит безобидно, а вместе они дают
ровно ту беду, ради которой канал и затевался: дежурный не может понять, взялся
ли кто-нибудь за аварию. Поэтому почти каждая проверка ниже — про второй раз.

Сети здесь нет: Bot API и Alertmanager подменяются на список вызовов. Это не
экономия — иначе проверки писали бы в живой чат и ставили настоящие silence.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from core.services import telegram_service, trevogi_service
from tests.conftest import API

ROOT = Path(__file__).resolve().parent.parent
ALERTMANAGER = ROOT / "docker" / "monitoring" / "alertmanager"
SHABLON_CRM = ALERTMANAGER / "alertmanager-crm.yml.template"
SHABLON_PRYAMOY = ALERTMANAGER / "alertmanager.yml.template"
ENTRYPOINT = ALERTMANAGER / "entrypoint.sh"

TG = f"{API}/telegram"
TREVOGI = f"{API}/alerts"

#: Форма настоящего токена: цифры, двоеточие, буквенно-цифровой хвост.
OBRAZETS_TOKENA = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

#: Чат тревог. Отрицательный — это группа, и служебный чат обычно именно она.
CHAT_TREVOG = "-1009900000001"


# --- обстановка ---------------------------------------------------------------


@pytest.fixture()
def kanal(root_client):
    """Бот фирмы настроен, чат тревог назван.

    Блок телеграма включается на время проверки и гасится после: по умолчанию он
    выключен, а настройки бота лежат за его ручками.
    """
    vklyuchen = root_client.post(f"{API}/modules/telegram", json={"enabled": True})
    assert vklyuchen.status_code == 200, vklyuchen.text
    zapis = root_client.put(
        f"{TG}/settings", json={"token": OBRAZETS_TOKENA, "digest_chat": CHAT_TREVOG}
    )
    assert zapis.status_code == 200, zapis.text

    from web.api.routes import trevogi as marshruty

    # Ограничитель общий на процесс и переживает проверки: без сброса соседние
    # начинают влиять друг на друга. Урок из docs/13, раздел 7.
    marshruty.limiter.ochistit()
    yield
    root_client.delete(f"{TG}/settings")
    root_client.post(f"{API}/modules/telegram", json={"enabled": False})


@pytest.fixture()
def bot(monkeypatch):
    """Все обращения к Bot API — в список. Возвращает `[(метод, поля), ...]`.

    Подменяется `_vyzov`, а не отдельные функции: через него идут и отправка, и
    правка сообщения, и ответ на нажатие, — то есть проверка видит ВСЁ, что
    ушло бы в телеграм, включая то, чего не ждала.
    """
    vyzovy: list[tuple[str, dict]] = []

    def podstava(token, metod, polya, opener=None):
        vyzovy.append((metod, polya))
        if metod == "sendMessage":
            return {"message_id": 5000 + len(vyzovy)}
        return {}

    monkeypatch.setattr(telegram_service, "_vyzov", podstava)
    return vyzovy


@pytest.fixture()
def alertmanager(monkeypatch):
    """Все обращения к Alertmanager — в список. `[(путь, тело), ...]`."""
    vyzovy: list[tuple[str, dict]] = []

    def podstava(put, telo, opener=None):
        vyzovy.append((put, telo))
        return {"silenceID": f"sil-{len(vyzovy)}"}

    monkeypatch.setattr(trevogi_service, "_vyzov", podstava)
    return vyzovy


def _dostavka(
    otpechatok: str,
    *,
    status: str = "firing",
    alertname: str = "SiteDown",
    severity: str = "critical",
    metki: dict | None = None,
) -> dict:
    """Доставка от Alertmanager в том виде, в каком он её шлёт."""
    return {
        "version": "4",
        "status": status,
        "receiver": "opencrm",
        "groupLabels": {"alertname": alertname},
        "commonLabels": {"alertname": alertname, "severity": severity},
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": alertname, "severity": severity, **(metki or {})},
                "annotations": {
                    "summary": "Сайт не отвечает: https://example.com/healthz",
                    "description": "Внешняя проверка не проходит уже 2 минуты.",
                },
                "startsAt": "2026-08-18T10:00:00.000Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": otpechatok,
            }
        ],
    }


def _poslat(root_client, telo):
    return root_client.post(f"{TREVOGI}/webhook", json=telo)


def _nazhali(root_client, dannye: str, *, imya="Иван", familiya="Петров", login="ivan", kto=777001):
    """Нажатие кнопки — тем же вебхуком, каким приходят сообщения клиентов."""
    from core.services import telegram_service as ts
    from database.session import SessionLocal

    with SessionLocal() as db:
        sekret = ts.webhook_secret(db)
    return root_client.post(
        f"{TG}/webhook",
        json={
            "update_id": 1,
            "callback_query": {
                "id": "cbq-1",
                "from": {
                    "id": kto,
                    "first_name": imya,
                    "last_name": familiya,
                    "username": login,
                },
                "message": {"message_id": 5001, "chat": {"id": int(CHAT_TREVOG), "type": "group"}},
                "data": dannye,
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": sekret},
    )


def _poslannye(vyzovy) -> list[dict]:
    return [polya for metod, polya in vyzovy if metod == "sendMessage"]


def _pravki(vyzovy) -> list[dict]:
    return [polya for metod, polya in vyzovy if metod == "editMessageText"]


def _podskazki(vyzovy) -> list[str]:
    return [polya.get("text", "") for metod, polya in vyzovy if metod == "answerCallbackQuery"]


def _knopki(polya: dict) -> list[str]:
    """Подписи кнопок из `reply_markup` одного вызова."""
    import json

    razmetka = json.loads(polya.get("reply_markup") or '{"inline_keyboard": []}')
    return [k["text"] for ryad in razmetka["inline_keyboard"] for k in ryad]


def _dannye_knopok(polya: dict) -> list[str]:
    import json

    razmetka = json.loads(polya.get("reply_markup") or '{"inline_keyboard": []}')
    return [k["callback_data"] for ryad in razmetka["inline_keyboard"] for k in ryad]


# --- доставка -----------------------------------------------------------------


def test_trevoga_uhodit_s_knopkami_prinyat_i_zaglushit(root_client, bot, kanal):
    """Ради этих двух кнопок канал и переехал к нам.

    Своя интеграция Alertmanager клавиатуру приложить не умеет вовсе, и без неё
    «принято» означает вход на сервер: открыть его интерфейс (наружу он не
    смотрит), найти тревогу, выписать метки, поставить silence руками. Ночью с
    телефона этого не делает никто.
    """
    otvet = _poslat(root_client, _dostavka("fp-osnovnaya"))
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["sent"] == 1

    poslano = _poslannye(bot)
    assert len(poslano) == 1, "тревога ушла не одним сообщением"
    assert str(poslano[0]["chat_id"]) == CHAT_TREVOG, "тревога ушла не в чат тревог"
    assert "Сайт не отвечает" in poslano[0]["text"], (
        "в сообщении нет сути — в списке чатов оно неотличимо от любого другого"
    )
    assert _knopki(poslano[0]) == ["✅ Принято", "🔕 Заглушить на час"]


def test_povtornaya_dostavka_ne_shlet_vtorogo_soobshcheniya(root_client, bot, kanal):
    """Alertmanager повторяет доставку сам — у горящей тревоги сообщение одно.

    Повторов тут не один источник, а три: `repeat_interval` (час у
    критического), повтор при неудачной отправке и перечитывание конфига,
    обрывающее отправку на полпути. Без защиты лежащий сайт дал бы сообщение в
    час, каждое со своими кнопками, — и «принято», нажатое на вчерашнем, не
    значило бы ничего.
    """
    telo = _dostavka("fp-povtor")
    pervaya = _poslat(root_client, telo)
    vtoraya = _poslat(root_client, telo)

    assert pervaya.json()["sent"] == 1
    assert vtoraya.json()["duplicate"] == 1, vtoraya.text
    assert vtoraya.json()["sent"] == 0
    assert len(_poslannye(bot)) == 1, "повтор доставки размножил сообщение в чате"


def test_otboy_perepisyvaet_soobshchenie_i_ubiraet_knopki(root_client, bot, kanal):
    """Погасшая тревога обязана перестать выглядеть горящей.

    Иначе служебный чат превращается в ленту красных сообщений, из которых
    половина уже неактуальна, и читать её перестают целиком — вместе с той
    одной, что горит по-настоящему.
    """
    _poslat(root_client, _dostavka("fp-otboy"))
    otvet = _poslat(root_client, _dostavka("fp-otboy", status="resolved"))
    assert otvet.json()["resolved"] == 1, otvet.text

    pravki = _pravki(bot)
    assert len(pravki) == 1, "сообщение об отбое не переписано"
    assert "Отбой" in pravki[0]["text"]
    assert _knopki(pravki[0]) == [], (
        "у погасшей тревоги остались кнопки — «принять» уже нечего, а тишина "
        "поставилась бы на то, чего нет"
    )


def test_zagorevshayasya_snova_trevoga_ne_schitaetsya_povtorom(root_client, bot, kanal):
    """Отпечаток тревоги считается по меткам и завтра будет тем же.

    Значит память об отправленном обязана освобождаться отбоем. Не освободи её —
    и следующее падение сайта было бы принято за повтор вчерашнего: сообщения
    нет, кнопок нет, дежурный ничего не узнал. Молча.
    """
    _poslat(root_client, _dostavka("fp-snova"))
    _poslat(root_client, _dostavka("fp-snova", status="resolved"))
    opyat = _poslat(root_client, _dostavka("fp-snova"))

    assert opyat.json()["sent"] == 1, opyat.text
    assert len(_poslannye(bot)) == 2, "вторая авария не дошла — её сочли повтором первой"


def test_neudavshayasya_otpravka_ne_zapiraet_trevogu_navsegda(
    root_client, bot, kanal, monkeypatch
):
    """Отметка «уже слали» ставится ДО отправки — и снимается, если та не удалась.

    Это тот же признак, запирающий сам себя, который проект уже ловил на
    аватарах: отметка ставится действием, а действие делается только по
    отсутствию отметки. Останься она после неудачи — тревога не ушла бы никогда,
    сколько бы Alertmanager ни повторял.

    Заодно проверяется ответ наружу: преходящий отказ обязан быть пятисоткой.
    Ответь мы 200, Alertmanager счёл бы доставку удавшейся и не повторил бы.
    """
    zapisyvayushchiy = telegram_service._vyzov
    otkazyvat = {"da": True}

    def otkazat(token, metod, polya, opener=None):
        if otkazyvat["da"] and metod == "sendMessage":
            raise telegram_service.TelegramOtkaz("Too Many Requests: retry after 30")
        return zapisyvayushchiy(token, metod, polya, opener)

    monkeypatch.setattr(telegram_service, "_vyzov", otkazat)
    upalo = _poslat(root_client, _dostavka("fp-otkaz"))
    assert upalo.status_code == 503, (
        "неудачная отправка отвечает 200 — Alertmanager сочтёт тревогу доставленной"
    )
    assert upalo.json()["failed"] == 1
    assert _poslannye(bot) == [], "сообщение всё-таки ушло — проверка смотрит не туда"

    # Телеграм ожил, Alertmanager повторил доставку.
    otkazyvat["da"] = False
    povtor = _poslat(root_client, _dostavka("fp-otkaz"))
    assert povtor.json()["sent"] == 1, "тревога заперта собственной отметкой навсегда"
    assert len(_poslannye(bot)) == 1


def test_bez_nastroennogo_chata_trevogi_ne_prinimayutsya(root_client, bot):
    """Ненастроенный канал — не отказ, а «доставляй прежним путём».

    Ответь мы отказом, Alertmanager копил бы очередь повторов. Ответь мы «ок» —
    тревога исчезла бы: у нас нет ни бота, ни чата, чтобы её показать. Поэтому
    третий ответ: приняли и честно сказали, что не слали.
    """
    otvet = _poslat(root_client, _dostavka("fp-nenastroeno"))
    assert otvet.status_code == 200, otvet.text
    assert otvet.json() == {"status": "skipped", "reason": "not_configured"}
    assert _poslannye(bot) == []


def test_gotovnost_otvechaet_alertmanageru(root_client, bot, kanal):
    """По этому ответу Alertmanager выбирает путь доставки.

    Ответ «нет» обязан быть двухсоткой с признаком, а не отказом: отказ он
    принял бы за поломку самой проверки и вёл бы себя одинаково при живой и
    мёртвой CRM — то есть проверка перестала бы что-либо решать.
    """
    gotov = root_client.get(f"{TREVOGI}/ready")
    assert gotov.status_code == 200, gotov.text
    assert gotov.json() == {"ready": True}

    root_client.delete(f"{TG}/settings")
    ne_gotov = root_client.get(f"{TREVOGI}/ready")
    assert ne_gotov.status_code == 200
    assert ne_gotov.json() == {"ready": False}, (
        "без токена бота канал объявляет себя готовым — тревоги уйдут в никуда"
    )


def test_trevoga_prishedshaya_snaruzhi_otvergaetsya(root_client, bot, kanal):
    """Единственное, чем закрыт приём вместо сессии, — граница сети.

    Alertmanager стучится напрямую, внутри сети compose; снаружи до приложения
    можно дойти ровно одним путём — через nginx, а тот на каждом запросе
    дописывает `X-Forwarded-For` и убрать его посторонний не может. Значит
    запрос с этим заголовком тревогой не является.

    Без проверки любой из интернета слал бы владельцу поддельные аварии — и
    настоящие после этого читались бы вполглаза.
    """
    otvet = root_client.post(
        f"{TREVOGI}/webhook",
        json=_dostavka("fp-snaruzhi"),
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert otvet.status_code == 403, otvet.text
    assert otvet.json()["error"]["code"] == "alerts_external"
    assert _poslannye(bot) == [], "поддельная тревога всё-таки ушла в чат"


# --- нажатия ------------------------------------------------------------------


def test_prinyal_otmechaet_kto_imenno_i_perepisyvaet_soobshchenie(root_client, bot, kanal):
    """Принятая тревога обязана перестать выглядеть непринятой.

    Иначе второй дежурный жмёт «принять» по той же самой, а третий идёт её
    чинить — втроём по одной поломке. Имя в сообщении отвечает на единственный
    вопрос, который задают в служебном чате ночью: занят этим кто-нибудь или
    нет.
    """
    _poslat(root_client, _dostavka("fp-prinyal"))
    otvet = _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:a:fp-prinyal")
    assert otvet.status_code == 200, otvet.text

    pravki = _pravki(bot)
    assert len(pravki) == 1, "сообщение не переписано — тревога выглядит непринятой"
    assert "Иван Петров" in pravki[0]["text"], "не видно, КТО принял тревогу"
    assert "✅ Принято" not in _knopki(pravki[0]), (
        "кнопка «принято» осталась и приглашает принять то, что уже приняли"
    )
    assert "🔕 Заглушить на час" in _knopki(pravki[0]), (
        "заглушить принятую тревогу по-прежнему осмысленно — этой кнопке рано исчезать"
    )
    assert _podskazki(bot) == ["Принято"], (
        "нажавшему не ответили: у него крутятся часики, и через пару секунд он "
        "нажмёт второй раз"
    )


def test_vtoroy_nazhavshiy_uznaet_imya_pervogo(root_client, bot, kanal):
    """Кнопку в служебном чате жмут двое — оба видят одно сообщение.

    Перезаписывать отметку вторым нельзя: тогда «принял» отвечал бы на вопрос
    «кто нажал последним», а спрашивают «кто этим занят». Второму при этом
    отвечаем не молчанием, а именем первого — он спрашивал ровно об этом.
    """
    _poslat(root_client, _dostavka("fp-dvoe"))
    _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:a:fp-dvoe", imya="Иван", familiya="Петров")
    _nazhali(
        root_client,
        f"{trevogi_service.PRISTAVKA}:a:fp-dvoe",
        imya="Марина",
        familiya="Сидорова",
        login="marina",
        kto=777002,
    )

    assert _podskazki(bot) == ["Принято", "Уже принял: Иван Петров (@ivan)"]
    assert len(_pravki(bot)) == 1, (
        "второе нажатие переписало сообщение заново — телеграм отбивает правку "
        "неизменившегося текста, и в журнале копятся отказы на ровном месте"
    )


@pytest.fixture()
def zapis_ne_uspevaet(monkeypatch):
    """Соседний процесс ещё не переписал запись, а мы её уже прочитали.

    Именно так выглядит гонка двух нажатий: правка записи — три действия
    (прочитать, поменять, записать), и между первым и третьим успевает второй
    нажавший. Воспроизводим её тем, что запись «не доезжает»: тогда второе
    нажатие видит ровно то, что видел бы одновременный процесс.
    """
    monkeypatch.setattr(trevogi_service, "_zapomnit", lambda otpechatok, zapis: None)


def test_odnovremennoe_nazhatie_ne_delaet_dvuh_prinyavshih(
    root_client, bot, kanal, zapis_ne_uspevaet
):
    """Двое жмут «принято» разом — принявший всё равно один.

    Кнопку в служебном чате жмут с разницей в секунду: оба увидели одно
    сообщение. Рабочих процессов у приложения несколько, и «оба прочитали
    запись до того, как её переписали» — не теоретический случай. Решает
    хранилище одним действием, а не порядок, в котором нам повезло прочитать.
    """
    _poslat(root_client, _dostavka("fp-gonka-ack"))
    _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:a:fp-gonka-ack")
    _nazhali(
        root_client,
        f"{trevogi_service.PRISTAVKA}:a:fp-gonka-ack",
        imya="Марина",
        familiya="Сидорова",
        login="marina",
        kto=777004,
    )

    assert _podskazki(bot) == ["Принято", "Уже принял: Иван Петров (@ivan)"], (
        "второй нажавший тоже стал принявшим — в чате теперь двое «взялись», и "
        "оба уверены, что второй просто опоздал"
    )


def test_odnovremennoe_zaglushenie_ne_plodit_tishiny(
    root_client, bot, kanal, alertmanager, zapis_ne_uspevaet
):
    """Та же гонка у тишины, и цена её выше.

    Две тишины на одну тревогу снимаются по отдельности: отменивший одну
    увидит, что тревога всё равно молчит, и пойдёт искать причину в правилах.
    """
    _poslat(root_client, _dostavka("fp-gonka-sil"))
    _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:s:fp-gonka-sil")
    _nazhali(
        root_client,
        f"{trevogi_service.PRISTAVKA}:s:fp-gonka-sil",
        imya="Марина",
        familiya="Сидорова",
        login="marina",
        kto=777005,
    )

    assert len(alertmanager) == 1, "одновременные нажатия поставили две тишины"


def test_neudavshayasya_tishina_ne_zapiraet_knopku(
    root_client, bot, kanal, monkeypatch
):
    """Отметка «глушу» снимается, если тишина не встала.

    Останься она — кнопка исчезла бы, тишины не было бы, а следующее нажатие
    получило бы «уже заглушено» с именем того, у кого НЕ ПОЛУЧИЛОСЬ. Тот же
    признак, запирающий сам себя, только в мелком масштабе.

    Второй раз жмёт ДРУГОЙ человек, и это не украшение проверки: отметка
    хранит имя занявшего, и тот же самый человек прошёл бы сквозь неё, не
    заметив, что она застряла. Так это и выглядит в жизни — «у Ивана не
    вышло, попробую я».
    """
    _poslat(root_client, _dostavka("fp-tishina-zapor"))

    padaet = {"da": True}

    def podstava(put, telo, opener=None):
        if padaet["da"]:
            raise trevogi_service.AlertmanagerOtkaz("HTTP 500")
        return {"silenceID": "sil-pozzhe"}

    monkeypatch.setattr(trevogi_service, "_vyzov", podstava)
    _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:s:fp-tishina-zapor")
    assert _podskazki(bot) == ["Не удалось заглушить — Alertmanager не ответил"]

    # Alertmanager ожил, и попробовал уже другой человек.
    padaet["da"] = False
    _nazhali(
        root_client,
        f"{trevogi_service.PRISTAVKA}:s:fp-tishina-zapor",
        imya="Марина",
        familiya="Сидорова",
        login="marina",
        kto=777006,
    )
    assert _podskazki(bot)[-1] == "Заглушено на час", (
        "повторное нажатие после неудачи отвечает «уже заглушено» — отметка "
        "осталась за тем, у кого не получилось, а тишины нет"
    )


def test_zaglushit_stavit_tishinu_po_vsem_metkam_trevogi(
    root_client, bot, kanal, alertmanager
):
    """Глушим ровно эту тревогу, а не всё правило целиком.

    Разница настоящая: `ContainerUnhealthy` приходит с меткой контейнера, и
    тишина по одному `alertname` заглушила бы заодно все остальные. Человек,
    нажав «заглушить» на одном контейнере, перестал бы узнавать про девять —
    молча и до тех пор, пока кто-нибудь не откроет Alertmanager.
    """
    _poslat(
        root_client,
        _dostavka(
            "fp-tishina", alertname="ContainerUnhealthy", metki={"service": "app"}
        ),
    )
    otvet = _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:s:fp-tishina")
    assert otvet.status_code == 200, otvet.text

    assert len(alertmanager) == 1, "тишина в Alertmanager не поставлена"
    put, telo = alertmanager[0]
    assert put == "/api/v2/silences"
    imena = {m["name"]: m["value"] for m in telo["matchers"]}
    assert imena == {
        "alertname": "ContainerUnhealthy",
        "severity": "critical",
        "service": "app",
    }, "тишина ставится не по всем меткам — заглушит соседние тревоги заодно"
    assert all(m["isRegex"] is False for m in telo["matchers"]), (
        "метки ушли как выражения: значение со звёздочкой или точкой заглушило бы чужое"
    )
    assert "Иван Петров" in telo["createdBy"], (
        "тишина подписана не человеком — список тишин в Alertmanager это "
        "единственное место, где видно, кто и что заглушил"
    )
    assert _podskazki(bot) == ["Заглушено на час"]


def test_povtornoe_zaglushenie_ne_plodit_tishiny(root_client, bot, kanal, alertmanager):
    """Два нажатия — две тишины, и снятие одной не снимает второй.

    Цена ошибки видна не сразу и потому высока: человек, отменивший тишину,
    обнаружит, что тревога всё равно молчит, и пойдёт искать причину в
    правилах — то есть там, где её нет.
    """
    _poslat(root_client, _dostavka("fp-tishina-dvazhdy"))
    _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:s:fp-tishina-dvazhdy")
    _nazhali(
        root_client,
        f"{trevogi_service.PRISTAVKA}:s:fp-tishina-dvazhdy",
        imya="Марина",
        familiya="Сидорова",
        login="marina",
        kto=777003,
    )

    assert len(alertmanager) == 1, "второе нажатие поставило вторую тишину"
    assert _podskazki(bot)[-1].startswith("Уже заглушено:"), (
        "второму нажавшему не сказали, что тревога уже заглушена и кем"
    )


def test_neudavshayasya_tishina_ne_pomechaet_trevogu_zaglushennoy(
    root_client, bot, kanal, monkeypatch
):
    """Alertmanager не ответил — отметки быть не должно.

    Поставь мы её, кнопка исчезла бы, в сообщении появилось бы «заглушено», а
    тревога продолжала бы приходить. Человек был бы уверен, что заглушил, и
    считал бы это поломкой мониторинга.
    """
    _poslat(root_client, _dostavka("fp-tishina-otkaz"))

    def otkazat(put, telo, opener=None):
        raise trevogi_service.AlertmanagerOtkaz("HTTP 500")

    monkeypatch.setattr(trevogi_service, "_vyzov", otkazat)
    _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:s:fp-tishina-otkaz")

    assert _podskazki(bot) == ["Не удалось заглушить — Alertmanager не ответил"]
    assert _pravki(bot) == [], (
        "сообщение переписано как заглушенное, хотя тишины нет — кнопка исчезла, "
        "а тревога будет приходить дальше"
    )


def test_nazhatie_na_zabytuyu_trevogu_otvechaet_a_ne_molchit(root_client, bot, kanal):
    """Сообщение со старыми кнопками остаётся в чате навсегда, память — нет.

    Нажатие по вчерашнему — обычное дело, и молчание в ответ читается как «не
    сработало»: человек жмёт ещё раз, потом ещё. Отвечать надо даже на то
    нажатие, которое мы выполнять не собираемся.
    """
    otvet = _nazhali(root_client, f"{trevogi_service.PRISTAVKA}:a:fp-takoy-net")
    assert otvet.status_code == 200, otvet.text
    assert _podskazki(bot) == ["Эта тревога уже не отслеживается"]
    assert _pravki(bot) == []


def test_callback_data_vlezaet_v_predel_telegrama(root_client, bot, kanal):
    """У `callback_data` телеграма 64 БАЙТА, и это не рекомендация.

    Длиннее — отказ ВСЕГО сообщения, то есть тревога не уходит вовсе. Отпечаток
    приходит от Alertmanager и однажды окажется длиннее ожидаемого; поймать это
    надо здесь, а не по несостоявшейся доставке аварии.
    """
    _poslat(root_client, _dostavka("f" * 40))
    for dannye in _dannye_knopok(_poslannye(bot)[0]):
        assert len(dannye.encode("utf-8")) <= 64, (
            f"«{dannye}» — {len(dannye.encode('utf-8'))} байт: телеграм отобьёт "
            "сообщение целиком, и об аварии никто не узнает"
        )


def test_knopka_zayavlena_v_reestre_nazhatiy():
    """Без записи в реестре нажатие разбирается как неизвестная кнопка.

    Снаружи это выглядит хуже всего: сообщение с кнопками приходит, кнопки
    нажимаются, а в ответ «этой кнопки больше нет». Проверка стережёт связь,
    которой не видно ни в одном из двух файлов по отдельности.
    """
    assert telegram_service.NAZHATIYA.get(trevogi_service.PRISTAVKA) is trevogi_service.nazhali


# --- конфиг Alertmanager ------------------------------------------------------


def _poluchateli(put: Path) -> dict[str, str]:
    """{имя приёмника: его кусок текста} из шаблона Alertmanager."""
    hvost = put.read_text(encoding="utf-8").split("\nreceivers:", 1)[1]
    return {
        sovpalo.group(1): sovpalo.group(2)
        for sovpalo in re.finditer(r"\n  - name: (\S+)\n(.*?)(?=\n  - name: |\Z)", hvost, re.S)
    }


def _priyomnik_marshruta(put: Path, vazhnost: str) -> str:
    """Кому уходит тревога такой важности."""
    marshruty = put.read_text(encoding="utf-8").split("routes:", 1)[1].split("\nreceivers:", 1)[0]
    kusok = marshruty.split(f'severity="{vazhnost}"', 1)[1]
    return re.search(r"receiver: (\S+)", kusok).group(1)


def test_avariya_ne_uhodit_dvumya_soobshcheniyami():
    """Приёмник Alertmanager отрабатывает ВСЕ свои настройки подряд.

    Оставь мы рядом с `webhook_configs` прежний `telegram_configs`, владелец
    получал бы каждую аварию дважды — с кнопками и без. Второе сообщение быстро
    научило бы не читать оба, и кнопки потеряли бы смысл вместе с чатом.
    """
    poluchateli = _poluchateli(SHABLON_CRM)
    kuda = _priyomnik_marshruta(SHABLON_CRM, "critical")
    assert kuda in poluchateli, f"аварии уходят в несуществующий приёмник {kuda}"

    blok = poluchateli[kuda]
    assert "webhook_configs:" in blok, "аварии перестали уходить через CRM — кнопок не будет"
    assert "telegram_configs:" not in blok, (
        "у приёмника аварий остался и прямой телеграм — тревога уйдёт двумя "
        "сообщениями, с кнопками и без"
    )
    assert re.search(r"^\s+send_resolved: true\s*$", blok, re.M), (
        "отбой не доставляется: погасшая тревога останется в чате красной "
        "навсегда, а зажёгшаяся снова будет сочтена повтором и промолчит"
    )


def test_preduprezhdenie_po_prezhnemu_prihodit_bez_zvuka(monkeypatch):
    """Предупреждение, звенящее ночью, учит выключать звук всему чату.

    А выключенный звук чата — это выключенный звук и у аварии тоже. Довод
    старый и оплаченный, и появление кнопок не имеет права его отменить.

    **Механизм сменился, довод — нет.** Раньше тишину обеспечивал отдельный
    приёмник Alertmanager, и проверка сравнивала приёмники. Теперь звук
    выключает сама служба по метке важности, а приёмник один — иначе оформление
    сообщения пришлось бы держать в двух копиях, которые расходятся молча.
    Проверка смотрит туда, где решение теперь живёт: авария звенит,
    предупреждение приходит тихо.
    """
    from core.services import trevogi_service

    poslannoe = []

    def podstava(kluch, chat_id, text, knopki, opener=None, tiho=False):
        poslannoe.append(tiho)
        return {"message_id": 100 + len(poslannoe)}

    monkeypatch.setattr(trevogi_service.telegram_service, "poslat_s_knopkami", podstava)

    from database.session import SessionLocal

    # Зовём настоящую точку разбора одной тревоги, а не заведённый ради проверки
    # помощник: помощник стерёг бы сам себя, а разбор — тот, что работает.
    with SessionLocal() as db:
        for vazhnost in ("critical", "warning"):
            trevogi_service._odna(
                db,
                "token",
                1,
                {
                    "status": "firing",
                    "fingerprint": f"proba-zvuk-{vazhnost}",
                    "labels": {"alertname": "Проба", "severity": vazhnost},
                    "annotations": {"summary": "проверка звука"},
                    "startsAt": "",
                },
            )

    assert poslannoe == [False, True], (
        f"важность больше не решает, звенеть ли телефону: {poslannoe}"
    )


def test_tekst_preduprezhdeniya_ne_razoshelsya_s_pryamym_putem():
    """Копия шаблона сообщения здесь неизбежна — значит нужен сторож.

    Якорь YAML (`*soobshchenie`) живёт внутри одного файла, а приёмника
    `telegram` в шаблоне через CRM нет вовсе. Копию пришлось выписать целиком, и
    без этой проверки она разошлась бы с оригиналом молча: половина
    предупреждений оформлялась бы по-старому, и понять, какая именно, можно было
    бы только сравнив два случайных сообщения в чате.
    """
    def tekst(put: Path) -> str:
        stroki = put.read_text(encoding="utf-8").splitlines()
        nachalo = next(i for i, s in enumerate(stroki) if s.strip().startswith("message:"))
        otstup = len(stroki[nachalo]) - len(stroki[nachalo].lstrip())
        telo = []
        for stroka in stroki[nachalo + 1 :]:
            if stroka.strip() and (len(stroka) - len(stroka.lstrip())) <= otstup:
                break
            telo.append(stroka.strip())
        assert telo, f"тело сообщения не нашлось в {put.name}"
        return "\n".join(telo)

    assert tekst(SHABLON_CRM) == tekst(SHABLON_PRYAMOY), (
        "текст сообщения разъехался между прямым путём и путём через CRM"
    )


def test_inhibitsiya_odinakova_na_oboih_putyah_dostavki():
    """Лежащий сайт зажигает полдюжины правил, а чинят одну поломку.

    Гашение следствий работает ДО выбора приёмника, то есть обязано быть
    одинаковым на всех путях. Разъедься эти списки — и по одному пути авария
    давала бы одно сообщение, а по другому шесть; заметить это можно было бы
    только сравнив два падения сайта, случившихся при разных настройках.
    """
    def inhibitsiya(put: Path) -> str:
        text = put.read_text(encoding="utf-8")
        kusok = text.split("inhibit_rules:", 1)[1].split("\nroute:", 1)[0]
        return "\n".join(
            s.strip() for s in kusok.splitlines() if s.strip() and not s.lstrip().startswith("#")
        )

    assert inhibitsiya(SHABLON_CRM) == inhibitsiya(SHABLON_PRYAMOY), (
        "списки гашения следствий разъехались между прямым путём и путём через CRM"
    )


def test_molchashchaya_crm_vozvrashchaet_dostavku_na_pryamoy_put(tmp_path):
    """Главный запасной путь всего замысла, и он обязан работать сам.

    Тревога о лежащем сайте не может доставляться через тот же сайт. Значит
    выбор пути нельзя делать переключателем: он остался бы в положении «через
    CRM» ровно тогда, когда CRM не отвечает, и авария не дошла бы никуда.
    Поэтому путь ПРОВЕРЯЕТСЯ, и проверка обязана падать в сторону прямой
    доставки — при любом исходе, включая отсутствие `wget` в чужом образе.

    Сторож не текстовый: выбор берётся из самого файла и выполняется настоящей
    оболочкой.
    """
    if not shutil.which("sh"):
        pytest.skip("нет /bin/sh — проверять оболочкой нечем")

    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    vybor = re.search(r"\nvybrat_shablon\(\) \{\n(.*?)\n\}\n", entrypoint, re.S)
    assert vybor, "функция vybrat_shablon пропала — проверка смотрит не туда"
    proba = re.search(r"\ncrm_gotov\(\) \{\n(.*?)\n\}\n", entrypoint, re.S)
    assert proba, "проба готовности CRM пропала"

    # Настоящий файл, а не /dev/null: точка входа проверяет шаблон на
    # существование через `-f`, а для символьного устройства он ложен.
    est_shablon = tmp_path / "alertmanager-crm.yml.template"
    est_shablon.write_text("receivers: []\n", encoding="utf-8")

    def vybrano(otvet_crm: str | None, shablon_est: bool = True) -> str:
        """Какой шаблон выберет точка входа при таком ответе CRM."""
        wget = (
            f'wget() {{ echo \'{otvet_crm}\'; }}'
            if otvet_crm is not None
            # Ни ответа, ни самого wget — оба исхода означают «не готова».
            else "wget() { return 1; }"
        )
        put_shablona = est_shablon.as_posix() if shablon_est else "/net-takogo-fayla"
        stsenariy = f"""
TOKEN=t
CHAT=c
CRM_READY=http://app:8000/api/v1/alerts/ready
SHABLON_CRM={put_shablona}
{wget}
crm_gotov() {{
{proba.group(1)}
}}
vybrat_shablon() {{
{vybor.group(1)}
}}
vybrat_shablon
echo "$TEMPLATE"
"""
        zapusk = subprocess.run(
            ["sh", "-c", stsenariy], capture_output=True, text=True, timeout=60
        )
        assert zapusk.returncode == 0, zapusk.stderr
        return zapusk.stdout.strip()

    assert vybrano('{"ready":true}') == est_shablon.as_posix(), (
        "готовая CRM не выбирается — кнопок не будет никогда"
    )
    assert "alertmanager.yml.template" in vybrano('{"ready":false}'), (
        "CRM ответила «не готова», а доставка всё равно идёт через неё — "
        "тревоги уйдут в никуда"
    )
    assert "alertmanager.yml.template" in vybrano(None), (
        "CRM не ответила вовсе (лежит, нет wget в образе), а доставка осталась "
        "через неё — то есть авария не дойдёт именно тогда, когда она случилась"
    )
    assert "alertmanager.yml.template" in vybrano('{"ready":true}', shablon_est=False), (
        "шаблон не примонтирован, а выбран: sed по отсутствующему файлу под "
        "set -e уронит точку входа, и Alertmanager уйдёт в цикл перезапуска"
    )
