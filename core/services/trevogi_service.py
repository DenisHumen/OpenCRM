"""Тревоги мониторинга в телеграме — с кнопками «принято» и «заглушить на час».

--------------------------------------------------------------------------
Почему тревогу шлём мы, а не сам Alertmanager
--------------------------------------------------------------------------

У Alertmanager есть своя интеграция с телеграмом, и до сих пор тревоги уходили
через неё (`docker/monitoring/alertmanager/alertmanager.yml.template`). Она
умеет всё, кроме одного: **приложить к сообщению клавиатуру**. В её настройках
нет `reply_markup` ни в каком виде, и добавить его нечем — это не наша
недоработка и не вопрос версии.

А без клавиатуры «принято» и «заглушить» превращаются в вход на сервер: открыть
интерфейс Alertmanager (он наружу не смотрит), найти тревогу, выписать метки,
поставить silence руками. В три часа ночи с телефона этого не делает никто, и
тревога либо чинится вслепую, либо не чинится вовсе.

Поэтому сообщение собираем и шлём мы. Alertmanager при этом остаётся тем, чем
был: он группирует, гасит следствия (`inhibit_rules`), помнит, что уже
отправлял, и знает про silence. Мы забираем у него ровно доставку.

--------------------------------------------------------------------------
Каким ботом
--------------------------------------------------------------------------

Ботом ФИРМЫ — тем, чей токен лежит в базе (`telegram_service`). Не деплойным,
которым тревоги уходят сегодня, и это вынужденно, а не по вкусу:

**нажатие кнопки приходит вебхуком, а вебхук в этой системе есть ровно у одного
бота — бота фирмы.** Деплойный бот только пишет: `deploy/notify.py` и
Alertmanager обращаются к нему исходящими запросами, приёма у него нет вовсе.
Завести приём и ему означало бы второй вебхук, второй секрет, вторую открытую
ручку и токен деплоя внутри контейнера приложения — четыре новых места ради
кнопки, которая и так работает у соседнего бота.

Довод «клиент не должен попадать в поток сообщений о сервере» (docs/13, 2.1)
этим не нарушается: сообщения уходят В НАЗВАННЫЙ ЧАТ владельца, а не тем, кто
боту написал. Ровно так же и по тем же причинам этим ботом уходит утренняя
сводка (`svodka_service`) — то есть решение не новое, а уже принятое.

Куда слать: `telegram_alerts_chat`, а если он пуст — туда же, куда сводку
(`telegram_digest_chat`). Пустая настройка означает «тревоги в CRM не
настроены», и тогда мы отказываемся принимать их у Alertmanager — он доставит
их прежним путём (см. `docker/monitoring/alertmanager/entrypoint.sh`).

--------------------------------------------------------------------------
Разметки в сообщении нет намеренно
--------------------------------------------------------------------------

`poslat_s_knopkami` шлёт текст без `parse_mode`, и для тревоги это не
ограничение, а защита. Телеграм отбивает сообщение с неразобравшейся разметкой
кодом 400 ЦЕЛИКОМ. В тексте тревоги стоит чужое: имя контейнера, адрес,
описание правила — там бывают `<`, `&` и звёздочки. Разметка означала бы, что
об аварии не узнали именно потому, что описание было подробным. Тот же довод
записан у Alertmanager, только там он вынужден решать это экранированием, а нам
экранировать нечего.

--------------------------------------------------------------------------
Память о тревоге — в Redis, а не в базе
--------------------------------------------------------------------------

Между тревогой и её сообщением в чате нужна связь: по нажатию надо знать, какое
сообщение переписывать и какие метки заглушать. Живёт эта связь ровно столько,
сколько горит тревога, — минуты или часы.

В базе ей не место по правилу проекта: это производное и недолговечное, как
присутствие в мессенджере (`core/realtime.py`). А главное — **настоящая запись
о том, кто принял тревогу, лежит в самом чате**: сообщение переписывается и
остаётся там навсегда. Redis держит только ссылку, и потеря этой ссылки стоит
одного лишнего сообщения, а не потерянного знания.

Отсюда поведение при недоступном Redis: тревога уходит ВСЁ РАВНО, просто без
защиты от повтора. Порядок именно такой — лишнее сообщение об аварии дешевле
пропущенного.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core import redis_client
from core.services import telegram_service
from core.utils import now_utc
from database.repositories import settings as settings_repo

logger = logging.getLogger(__name__)

#: Куда слать тревоги. Отдельно от чата сводки, но с запасным путём на него:
#: у маленькой конторы это один и тот же служебный чат, и требовать заполнить
#: две настройки одинаково значило бы завести способ ошибиться.
SETTING_CHAT = "telegram_alerts_chat"
SETTING_DIGEST_CHAT = telegram_service.SETTING_DIGEST_CHAT

#: Приставка `callback_data` у наших кнопок. Три буквы, потому что в
#: `callback_data` телеграм отводит 64 БАЙТА на всё: `trv:a:<отпечаток>` — это
#: 22 байта, и запас остаётся.
PRISTAVKA = "trv"
DEYSTVIE_PRINYAL = "a"
DEYSTVIE_ZAGLUSHIT = "s"

#: На сколько глушит кнопка. Час — это «я этим занимаюсь, не мешайте», а не
#: «выключите тревогу»: за час поломку либо чинят, либо она возвращается и
#: спрашивает снова. Сутки в одну кнопку класть нельзя — заглушенная на сутки
#: авария забывается вместе с причиной.
SROK_TISHINY = 3600

#: Сколько помним отправленную тревогу. Больше суток: `repeat_interval` у
#: критического — час, у предупреждения — сутки, и память обязана пережить
#: повтор, иначе он придёт вторым сообщением.
SROK_PAMYATI = 36 * 3600

#: Приставка ключей памяти: `…trev:<отпечаток>`.
KLYUCH = f"{redis_client.PREFIX}trev:"

#: Где живёт Alertmanager. Внутри сети compose он всегда `alertmanager:9093`,
#: поэтому умолчание рабочее и настраивать обычно нечего.
#:
#: Читается из окружения напрямую, а не через `config/settings.py`, и это
#: временно: настройке место там, рядом с `base_url` и `redis_url`. Пока её
#: туда не внесли, чтение здесь честнее, чем зашитый адрес.
ALERTMANAGER = os.environ.get("OPENCRM_ALERTMANAGER_URL", "").strip() or "http://alertmanager:9093"

#: Сколько ждём Alertmanager. Он свой, в соседнем контейнере; секунды хватает с
#: запасом, а ждать дольше нельзя: мы отвечаем на нажатие кнопки, и у нажавшего
#: всё это время крутятся часики.
TIMEOUT = 5


class AlertmanagerOtkaz(Exception):
    """Alertmanager отказал. Отдельным видом — чтобы отличать от нашей поломки."""


# --- настройки ---------------------------------------------------------------


def chat(db: Session) -> str:
    """Идентификатор чата тревог. Пусто — тревоги в CRM не настроены."""
    vse = {row.key: row.value for row in settings_repo.rows_with_prefix(db, "telegram_")}
    return (vse.get(SETTING_CHAT) or vse.get(SETTING_DIGEST_CHAT) or "").strip()


def gotov(db: Session) -> bool:
    """Можем ли мы принять тревогу и доставить её.

    Спрашивается снаружи, из `entrypoint.sh` Alertmanager: пока ответ «нет», он
    доставляет тревоги прежним путём, напрямую в телеграм. Это и есть запасной
    путь — не бумажный, а работающий сам.
    """
    return bool(telegram_service.token(db)) and bool(chat(db))


# --- память о тревоге --------------------------------------------------------
#
# Три действия и все через одно место: занять (кто первый — тот и шлёт),
# вспомнить, забыть. Отказ Redis всюду означает «памяти нет», а не «ошибка»:
# без памяти тревога уходит повторно, и это правильная сторона ошибки.


def _klient():
    try:
        return redis_client.get_client()
    except Exception as beda:  # noqa: BLE001 — Redis не обязан быть живым
        logger.warning("память тревог недоступна: %r", beda)
        return None


def _zanyat(otpechatok: str, zapis: dict) -> bool:
    """Занять тревогу под себя. `True` — мы первые, можно слать.

    `SET NX` целиком в Redis, а не «прочитать и записать»: два одновременных
    обращения Alertmanager (повтор доставки, наш второй процесс) оба увидели бы
    пустоту и оба отправили бы сообщение.

    Redis недоступен — отвечаем `True`. Без памяти мы не умеем отличить повтор
    от новой тревоги, и выбирать приходится между лишним сообщением и
    пропущенной аварией.
    """
    client = _klient()
    if client is None:
        return True
    try:
        return bool(client.set(KLYUCH + otpechatok, json.dumps(zapis), nx=True, ex=SROK_PAMYATI))
    except Exception as beda:  # noqa: BLE001
        logger.warning("не заняли тревогу в памяти: %r", beda)
        return True


def _vspomnit(otpechatok: str) -> dict | None:
    client = _klient()
    if client is None:
        return None
    try:
        syroye = client.get(KLYUCH + otpechatok)
    except Exception as beda:  # noqa: BLE001
        logger.warning("не прочитали память тревог: %r", beda)
        return None
    if not syroye:
        return None
    try:
        return json.loads(syroye)
    except Exception:  # noqa: BLE001 — чужое или испорченное значение
        return None


def _zapomnit(otpechatok: str, zapis: dict) -> None:
    """Переписать занятую запись. Срок годности продлевается вместе с ней."""
    client = _klient()
    if client is None:
        return
    try:
        client.set(KLYUCH + otpechatok, json.dumps(zapis), ex=SROK_PAMYATI)
    except Exception as beda:  # noqa: BLE001
        logger.warning("не записали память тревог: %r", beda)


def _zabyt(otpechatok: str) -> None:
    client = _klient()
    if client is None:
        return
    try:
        client.delete(KLYUCH + otpechatok, f"{KLYUCH}ack:{otpechatok}", f"{KLYUCH}sil:{otpechatok}")
    except Exception as beda:  # noqa: BLE001
        logger.warning("не убрали тревогу из памяти: %r", beda)


def _zanyat_otmetku(otpechatok: str, vid: str, kem: str) -> str:
    """Кто ПЕРВЫМ нажал эту кнопку. Возвращает имя занявшего.

    Отдельным ключом и одним `SET NX`, а не полем в записи, и это не
    придирка. Правка поля — три действия: прочитать запись, поменять, записать.
    Между первым и третьим успевает второй нажавший, а в служебном чате двое
    жмут «принято» с разницей в секунду — оба увидели одно сообщение и оба
    хотят помочь. Рабочих процессов у приложения при этом несколько, то есть
    «оба одновременно» здесь не теоретическое.

    Тот же приём и по тому же доводу, по которому диалог мессенджера берётся
    `FOR UPDATE`: решение «кто первый» принимает хранилище, а не порядок, в
    котором нам повезло прочитать.

    Redis недоступен — считаем первым спросившего. Без общего хранилища
    отличить его от второго нечем, а отказать обоим значило бы, что при
    недоступном Redis тревогу нельзя принять вовсе.
    """
    client = _klient()
    if client is None:
        return kem
    kluch = f"{KLYUCH}{vid}:{otpechatok}"
    try:
        if client.set(kluch, kem, nx=True, ex=SROK_PAMYATI):
            return kem
        return client.get(kluch) or kem
    except Exception as beda:  # noqa: BLE001
        logger.warning("не заняли отметку нажатия: %r", beda)
        return kem


def _osvobodit_otmetku(otpechatok: str, vid: str) -> None:
    """Снять отметку, за которой не последовало действия.

    Нужна ровно там, где действие может не удаться: заняли «глушу», а
    Alertmanager не ответил. Оставь мы отметку — кнопка исчезла бы, тишины не
    было бы, и следующее нажатие получило бы «уже заглушено». Признак,
    запирающий сам себя, только в мелком масштабе.
    """
    client = _klient()
    if client is None:
        return
    try:
        client.delete(f"{KLYUCH}{vid}:{otpechatok}")
    except Exception as beda:  # noqa: BLE001
        logger.warning("не сняли отметку нажатия: %r", beda)


# --- текст сообщения ---------------------------------------------------------


def _kogda(znachenie: str) -> datetime | None:
    """Время из Alertmanager (RFC 3339) в наше, без пояса.

    Терпимо к записи: он шлёт и `...Z`, и смещением, и с долями секунды.
    Не разобрали — возвращаем `None`, а не роняем разбор: время в сообщении
    украшение, а тревога — нет.
    """
    if not znachenie:
        return None
    try:
        razobrano = datetime.fromisoformat(str(znachenie).replace("Z", "+00:00"))
    except ValueError:
        return None
    if razobrano.tzinfo is not None:
        razobrano = razobrano.astimezone(timezone.utc).replace(tzinfo=None)
    return razobrano


def _dlitelnost(sekund: float) -> str:
    """Сколько времени прошло — коротко и без склонений.

    Без склонений намеренно: «1 минуту», «2 минуты», «5 минут» — три формы, и
    ошибка в них видна в каждом сообщении об аварии. Сокращения одинаковы для
    любого числа.
    """
    sekund = int(max(sekund, 0))
    if sekund < 60:
        return f"{sekund} с"
    minut = sekund // 60
    if minut < 60:
        return f"{minut} мин"
    chasov, minut = divmod(minut, 60)
    if chasov < 24:
        return f"{chasov} ч {minut:02d} мин"
    dney, chasov = divmod(chasov, 24)
    return f"{dney} дн {chasov} ч"


def _tekst(zapis: dict, *, otbey: bool = False) -> str:
    """Сообщение о тревоге целиком, включая отметки о принятии и тишине.

    Собирается ЗАНОВО при каждой перерисовке, а не дописывается к прежнему.
    Дописывание означало бы, что нажавший дважды получает две строки «принял», а
    порядок строк зависит от порядка нажатий — то есть состояние тревоги
    приходилось бы вычитывать из истории правок сообщения.

    Первая строка устроена как у Alertmanager и по той же причине: в списке
    чатов видно только её, и она обязана ответить «идти смотреть или нет».
    Значка два и они про разное: 🔴/🟢 — горит или погасло, 🆘/⚠️ — вставать
    сейчас или посмотреть утром.
    """
    kritichno = zapis.get("severity") == "critical"
    if otbey:
        zagolovok = "🟢 Отбой"
    else:
        zagolovok = ("🔴 🆘 Авария" if kritichno else "🔴 ⚠️ Предупреждение")

    stroki = [f"{zagolovok} · {zapis.get('summary') or zapis.get('alertname') or 'Тревога'}"]
    if zapis.get("description"):
        stroki.append(str(zapis["description"]))

    nachalo = _kogda(zapis.get("startsAt", ""))
    if nachalo is not None:
        seychas = now_utc().replace(tzinfo=None)
        proshlo = (seychas - nachalo).total_seconds()
        stroki.append(
            f"⏱ длилась {_dlitelnost(proshlo)}" if otbey else f"⏱ горит уже {_dlitelnost(proshlo)}"
        )

    if zapis.get("prinyal"):
        stroki.append(f"✅ Принял: {zapis['prinyal']}")
    if zapis.get("zaglushil"):
        stroki.append(f"🔕 Заглушено на час — {zapis['zaglushil']}")
    return "\n".join(stroki)


def _knopki(zapis: dict) -> list[list[dict]]:
    """Клавиатура под тревогой. Отвечает состоянию, а не показывается всегда.

    Принятая тревога кнопку «принято» теряет: оставленная, она приглашает
    принять её второй раз, и второй дежурный идёт чинить то, что уже чинят.
    Заглушенная теряет «заглушить» — по той же причине.
    """
    otpechatok = zapis["fp"]
    ryad: list[dict] = []
    if not zapis.get("prinyal"):
        ryad.append({"text": "✅ Принято", "callback_data": f"{PRISTAVKA}:{DEYSTVIE_PRINYAL}:{otpechatok}"})
    if not zapis.get("zaglushil"):
        ryad.append(
            {"text": "🔕 Заглушить на час", "callback_data": f"{PRISTAVKA}:{DEYSTVIE_ZAGLUSHIT}:{otpechatok}"}
        )
    return [ryad] if ryad else []


def _otpechatok(trevoga: dict) -> str:
    """Чем одна тревога отличается от другой.

    Своим отпечатком от Alertmanager, если он его прислал: он и есть хэш меток,
    и считать своё значило бы завести второй ответ на тот же вопрос. Не
    прислал — считаем сами по тем же меткам, чтобы повтор всё-таки узнавался.
    """
    svoy = str(trevoga.get("fingerprint") or "").strip()
    if svoy:
        return svoy[:32]
    metki = trevoga.get("labels") or {}
    slepok = "\x00".join(f"{k}={metki[k]}" for k in sorted(metki))
    return hashlib.sha1(slepok.encode("utf-8")).hexdigest()[:16]


# --- приём от Alertmanager ---------------------------------------------------


def prinyat(db: Session, telo: dict, opener=None) -> dict:
    """Разобрать доставку от Alertmanager и разослать то, чего ещё не слали.

    **Идемпотентность здесь обязательна, а не желательна.** Alertmanager
    повторяет доставку сам: `repeat_interval` — час у критического и сутки у
    предупреждения; плюс повтор при неудачной отправке, плюс перечитывание
    конфига, обрывающее отправку на полпути (разбор этого — в
    `docker/monitoring/alertmanager/entrypoint.sh`). Без защиты один лежащий
    сайт дал бы сообщение в час, и каждое со своими кнопками — то есть
    «принято», нажатое на вчерашнем, ничего бы не значило.

    Защита одна и снаружи простая: **у тревоги одно сообщение, пока она горит.**
    Повтор доставки его не размножает, а обновление состояния (принята,
    заглушена, погасла) переписывает то же самое. Отбой сообщение перерисовывает
    и память освобождает — иначе та же тревога, зажёгшаяся назавтра, была бы
    принята за повтор и промолчала.
    """
    kluch = telegram_service.token(db)
    kuda = chat(db)
    if not kluch or not kuda:
        # Не настроено — не отказ. Alertmanager при этом доставляет прежним
        # путём (см. выбор шаблона в его entrypoint.sh), и повторять ему нечего.
        return {"status": "skipped", "reason": "not_configured"}

    trevogi = telo.get("alerts") or []
    if not isinstance(trevogi, list):
        return {"status": "ignored", "reason": "no_alerts"}

    itog = {"sent": 0, "duplicate": 0, "resolved": 0, "failed": 0}
    for trevoga in trevogi:
        if not isinstance(trevoga, dict):
            continue
        try:
            chto = _odna(db, kluch, int(kuda), trevoga, opener)
        except telegram_service.TelegramOtkaz as beda:
            # Телеграм отказал — это преходящее (сеть, 429, перезапуск). Считаем
            # неудачей и говорим об этом наружу: Alertmanager повторит, и это
            # ровно то, что нужно. Молчаливое 200 здесь означало бы потерянную
            # аварию.
            logger.warning("тревога не ушла в телеграм: %s", beda)
            itog["failed"] += 1
        except ValueError:
            # Идентификатор чата не число. Настройка проверяется при вводе, но
            # прийти сюда с мусором она всё-таки может — уронить из-за этого всю
            # доставку нельзя.
            logger.warning("чат тревог задан не числом: %r", kuda)
            itog["failed"] += 1
        else:
            itog[chto] += 1

    itog["status"] = "failed" if itog["failed"] else "ok"
    return itog


def _odna(db: Session, kluch: str, kuda: int, trevoga: dict, opener=None) -> str:
    """Одна тревога из доставки. Отвечает, что с ней сделали."""
    otpechatok = _otpechatok(trevoga)
    metki = trevoga.get("labels") or {}
    poyasneniya = trevoga.get("annotations") or {}
    pogasla = str(trevoga.get("status") or "").lower() == "resolved"

    if pogasla:
        return _pogasla(kluch, otpechatok, opener)

    zapis = {
        "fp": otpechatok,
        "chat_id": kuda,
        "message_id": 0,
        "labels": {str(k): str(v) for k, v in metki.items()},
        "severity": str(metki.get("severity") or ""),
        "alertname": str(metki.get("alertname") or ""),
        "summary": str(poyasneniya.get("summary") or ""),
        "description": str(poyasneniya.get("description") or ""),
        "startsAt": str(trevoga.get("startsAt") or ""),
    }

    if not _zanyat(otpechatok, zapis):
        # Уже слали. Ровно этого мы и добивались: повтор доставки не размножает
        # сообщение. Обновлять его тоже незачем — время «горит уже» в чате
        # устаревает, но переписывать сообщение каждый час значило бы дёргать
        # телефон уведомлением о правке.
        return "duplicate"

    try:
        otvet = telegram_service.poslat_s_knopkami(
            kluch,
            kuda,
            _tekst(zapis),
            _knopki(zapis),
            opener,
            # Предупреждение приходит без звука, авария — со звуком. Разница не
            # косметическая: звенящее ночью «соединений к базе 82%» учит
            # выключать звук всему чату, а выключенный звук чата — это
            # выключенный звук и у аварии. Кнопки при этом остаются у обоих:
            # «заглушить на час» нужна как раз тому, что шумит, но не горит.
            tiho=zapis.get("severity") != "critical",
        )
    except Exception:
        # Не ушло — освобождаем занятое, иначе повтор Alertmanager наткнётся на
        # нашу же отметку «уже слали» и тревога не уйдёт никогда. Это тот самый
        # признак, запирающий сам себя, который проект уже ловил на аватарах.
        _zabyt(otpechatok)
        raise

    zapis["message_id"] = int(otvet.get("message_id") or 0)
    _zapomnit(otpechatok, zapis)
    return "sent"


def _pogasla(kluch: str, otpechatok: str, opener=None) -> str:
    """Отбой: перерисовать сообщение и забыть тревогу.

    Забыть обязательно. Отпечаток считается по меткам и у той же тревоги
    завтра будет тем же — оставленная в памяти запись превратила бы новое
    падение сайта в «повтор», о котором никто не узнает.
    """
    zapis = _vspomnit(otpechatok)
    if zapis is None:
        # Об этой тревоге мы не сообщали (память истекла, Redis перезапускался,
        # тревога зажглась до включения канала). Отбой без тревоги — сообщение
        # ни о чём.
        return "resolved"

    if zapis.get("message_id"):
        try:
            telegram_service.perepisat_soobshchenie(
                kluch,
                int(zapis["chat_id"]),
                int(zapis["message_id"]),
                _tekst(zapis, otbey=True),
                [],
                opener,
            )
        except Exception as beda:  # noqa: BLE001
            # Сообщение могли удалить руками, а телеграм отбивает правку
            # неизменившегося текста. Ни то, ни другое не повод повторять
            # доставку: тревога погасла, и это главное.
            logger.warning("не переписали сообщение об отбое: %r", beda)
    _zabyt(otpechatok)
    return "resolved"


# --- нажатие кнопки ----------------------------------------------------------


def _kto_nazhal(nazhatie: dict) -> str:
    """Как назвать того, кто нажал.

    **Сотрудника по telegram-номеру сопоставить сегодня нечем**, и это не
    недосмотр разбора: у `users` нет ни колонки с номером телеграма, ни способа
    её заполнить — экрана «привязать свой телеграм» в профиле не существует.
    Завести колонку без такого экрана значило бы добавить в боевую базу поле,
    которое навсегда останется пустым, и получить ту же самую подпись из
    телеграма, только дороже.

    Поэтому подписываемся именем из телеграма. В служебном чате владельца это
    работает: там несколько человек, и «Иван Петров» они друг про друга знают.
    Логин добавляем, когда он есть, — тёзки в чате бывают, а логин в телеграме
    уникален.
    """
    kto = nazhatie.get("from") or {}
    chasti = [str(kto.get("first_name") or ""), str(kto.get("last_name") or "")]
    imya = " ".join(c for c in chasti if c).strip()
    login = str(kto.get("username") or "").strip()
    if imya and login:
        return f"{imya} (@{login})"[:120]
    if imya:
        return imya[:120]
    if login:
        return f"@{login}"[:120]
    return f"telegram:{kto.get('id')}"[:120]


def nazhali(db: Session, nazhatie: dict) -> str:
    """Обработчик наших кнопок. Возвращает текст всплывающей подсказки.

    Заявлен в реестре `telegram_service.NAZHATIYA` при импорте этого модуля —
    приём обновлений о нём ничего не знает и знать не должен.

    **Подсказка возвращается всегда, включая отказ.** Пока телеграм не получил
    ответ на нажатие, у нажавшего крутятся часики, и через несколько секунд он
    жмёт второй раз. Молчание здесь читается как «не сработало».
    """
    chasti = str(nazhatie.get("data") or "").split(":", 2)
    if len(chasti) != 3:
        return "Не разобрали кнопку"
    _, deystvie, otpechatok = chasti

    zapis = _vspomnit(otpechatok)
    if zapis is None:
        # Сообщение со старыми кнопками остаётся в чате навсегда, а память
        # тревоги живёт полтора суток. Нажатие по вчерашнему — обычное дело.
        return "Эта тревога уже не отслеживается"

    kem = _kto_nazhal(nazhatie)
    if deystvie == DEYSTVIE_PRINYAL:
        podskazka, izmenilos = _prinyat_trevogu(zapis, kem)
    elif deystvie == DEYSTVIE_ZAGLUSHIT:
        podskazka, izmenilos = _zaglushit_trevogu(zapis, kem)
    else:
        return "Такой кнопки больше нет"

    # Перерисовываем только на настоящее изменение. Телеграм отбивает правку
    # неизменившегося текста кодом 400 («message is not modified»), и второе
    # нажатие давало бы запись в журнал на ровном месте — а журнал читают,
    # когда ищут беду.
    if izmenilos:
        _pererisovat(db, zapis)
    return podskazka


def _prinyat_trevogu(zapis: dict, kem: str) -> tuple[str, bool]:
    """Отметить, кто взялся. Второй нажавший узнаёт про первого.

    **Первый нажавший и есть принявший.** Перезаписывать отметку вторым значило
    бы, что «принял» отвечает на вопрос «кто нажал последним», а нужен ответ на
    «кто этим занят». Второму при этом отвечаем не молчанием, а именем первого:
    он спрашивал ровно об этом.
    """
    if zapis.get("prinyal"):
        return f"Уже принял: {zapis['prinyal']}", False
    pervyy = _zanyat_otmetku(zapis["fp"], "ack", kem)
    if pervyy != kem:
        # Соседний процесс успел раньше, а мы прочитали запись до того, как он
        # её переписал. Свою стало́ бы вносить нельзя — она затёрла бы его имя.
        return f"Уже принял: {pervyy}", False
    zapis["prinyal"] = kem
    return "Принято", True


def _zaglushit_trevogu(zapis: dict, kem: str) -> tuple[str, bool]:
    """Поставить silence в Alertmanager на час.

    Идемпотентность здесь дороже, чем у остальных кнопок, потому что цена
    ошибки видна не сразу: два нажатия — два silence, и снятие одного не
    снимает второго. Человек, отменивший тишину, увидит, что тревога всё равно
    молчит, и искать причину будет в правилах.
    """
    if zapis.get("zaglushil"):
        return f"Уже заглушено: {zapis['zaglushil']}", False
    pervyy = _zanyat_otmetku(zapis["fp"], "sil", kem)
    if pervyy != kem:
        return f"Уже заглушено: {pervyy}", False
    try:
        zapis["silence_id"] = postavit_tishinu(zapis.get("labels") or {}, kem)
    except AlertmanagerOtkaz as beda:
        logger.warning("не поставили тишину: %s", beda)
        # Отметку снимаем: иначе кнопка исчезнет, тишины не будет, а следующее
        # нажатие получит «уже заглушено» — человек будет уверен, что заглушил.
        _osvobodit_otmetku(zapis["fp"], "sil")
        return "Не удалось заглушить — Alertmanager не ответил", False
    zapis["zaglushil"] = kem
    return "Заглушено на час", True


def _pererisovat(db: Session, zapis: dict) -> None:
    """Переписать сообщение под новое состояние и запомнить его.

    Порядок такой: сперва в память, потом в телеграм. Обратный означал бы, что
    неудачная правка сообщения (телеграм моргнул) теряет отметку «принято»
    вместе с собой — а она и есть то, ради чего кнопку нажимали.
    """
    _zapomnit(zapis["fp"], zapis)
    kluch = telegram_service.token(db)
    if not kluch or not zapis.get("message_id"):
        return
    try:
        telegram_service.perepisat_soobshchenie(
            kluch,
            int(zapis["chat_id"]),
            int(zapis["message_id"]),
            _tekst(zapis),
            _knopki(zapis),
        )
    except Exception as beda:  # noqa: BLE001
        # Сообщение не переписалось — подсказка нажавшему всё равно уйдёт, и
        # отметка в памяти уже стоит. Ронять разбор нажатия из-за оформления
        # значило бы потерять и подсказку.
        logger.warning("не переписали сообщение о тревоге: %r", beda)


# --- разговор с Alertmanager -------------------------------------------------


def _vyzov(put: str, telo: dict, opener=None) -> dict:
    """Один вызов Alertmanager. Своими руками на `urllib`, без зависимости.

    Тот же довод, по которому так сделан разговор с телеграмом: библиотека ради
    одного вызова — это ещё один пакет, который надо обновлять, и ещё одна
    причина, по которой сборка однажды не соберётся.
    """
    import urllib.error
    import urllib.request

    zapros = urllib.request.Request(
        f"{ALERTMANAGER.rstrip('/')}{put}",
        data=json.dumps(telo).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    otkryt = opener or urllib.request.urlopen
    try:
        with otkryt(zapros, timeout=TIMEOUT) as otvet:
            syroye = otvet.read().decode("utf-8")
    except urllib.error.HTTPError as beda:
        # Alertmanager объясняет отказ телом, и объяснение стоит вытащить:
        # «silence: invalid matchers» и «404» чинятся по-разному.
        try:
            poyasnenie = beda.read().decode("utf-8")[:200]
        except Exception:  # noqa: BLE001
            poyasnenie = ""
        raise AlertmanagerOtkaz(f"HTTP {beda.code} {poyasnenie}".strip()) from None
    except Exception as beda:  # noqa: BLE001 — сеть, DNS, таймаут
        raise AlertmanagerOtkaz(str(beda)) from None

    try:
        return json.loads(syroye) if syroye.strip() else {}
    except Exception:  # noqa: BLE001
        raise AlertmanagerOtkaz("ответ Alertmanager не разобрался") from None


def postavit_tishinu(metki: dict, kem: str, sekund: int = SROK_TISHINY, opener=None) -> str:
    """Заглушить ровно эту тревогу на срок. Возвращает идентификатор тишины.

    **Глушим по ВСЕМ меткам тревоги, а не по одному `alertname`.** Разница
    настоящая: `ContainerUnhealthy` приходит с меткой контейнера, и тишина по
    имени правила заглушила бы заодно все остальные контейнеры. Человек, нажав
    «заглушить» на одном, перестал бы узнавать про девять — молча и до тех пор,
    пока кто-нибудь не откроет Alertmanager.

    Автор в silence пишется настоящий: список тишин в Alertmanager — это
    единственное место, где видно, кто и что заглушил, и подпись «opencrm»
    сделала бы его бесполезным.
    """
    if not metki:
        raise AlertmanagerOtkaz("у тревоги нет меток — глушить нечего")

    seychas = datetime.now(timezone.utc)
    otvet = _vyzov(
        "/api/v2/silences",
        {
            "matchers": [
                {"name": str(k), "value": str(v), "isRegex": False, "isEqual": True}
                for k, v in sorted(metki.items())
            ],
            "startsAt": seychas.isoformat().replace("+00:00", "Z"),
            "endsAt": (seychas + timedelta(seconds=sekund)).isoformat().replace("+00:00", "Z"),
            "createdBy": kem[:120],
            "comment": "Заглушено кнопкой в телеграме",
        },
        opener,
    )
    return str(otvet.get("silenceID") or otvet.get("id") or "")


# Заявляем свои кнопки приёму обновлений. Здесь, а не в приёме: реестр для того
# и заведён, чтобы новая кнопка не требовала правки самого горячего кода канала.
telegram_service.podpisatsya_na_knopku(PRISTAVKA, nazhali)
