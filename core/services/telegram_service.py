"""Бот фирмы: настройки подключения.

**Это ОТДЕЛЬНЫЙ бот, а не тот, что уведомляет об обновлениях.** Разделение не
косметическое, и стоит назвать три причины, потому что соблазн обойтись одним
велик:

1. **Разные читатели.** Деплойный бот пишет владельцу в служебный чат: «сборка
   упала», «база откатилась». Бот фирмы пишут КЛИЕНТЫ. Один токен на оба
   означал бы, что клиент, нажавший «старт», попадает в тот же поток, где идут
   сообщения о состоянии сервера.
2. **Разные жизни секрета.** Токен деплоя лежит в `autoupdate.env` на машине и
   нужен ДО того, как приложение поднялось, — иначе некому сообщить, что оно не
   поднялось. Токен бота фирмы настраивается в интерфейсе и живёт в базе:
   владелец меняет его сам, не заходя на сервер.
3. **Разная цена утечки.** Утёкший деплойный токен позволяет слать сообщения в
   служебный чат. Утёкший токен бота фирмы позволяет читать и писать КЛИЕНТАМ
   от имени фирмы.

**Почему токен в базе, а не в переменной окружения.** Владелец заводит бота
сам, у @BotFather, и меняет при отзыве. Требовать ради этого доступа к серверу
значило бы, что настройка возможна только через того, кто такой доступ имеет.
Секрет при этом наружу не отдаётся: `GET` возвращает только признак «настроен»
и хвост токена для узнавания, а не сам токен.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import realtime
from core.utils import normalize_phone, now_utc
from database.models import ClientNote
from database.models.telegram import (
    DIRECTION_IN,
    DIRECTION_OUT,
    KIND_DOCUMENT,
    KIND_PHOTO,
    KIND_TEXT,
    KIND_VIDEO,
    KIND_VOICE,
    SEND_FAILED,
    SEND_PENDING,
    SEND_SENT,
)
from core.services import telegram_uborka
from database.repositories import settings as settings_repo
from database.repositories import telegram as telegram_repo

logger = logging.getLogger(__name__)

#: Вид записи в ленте клиента. Рядом с `call` и `email`: третий канал общения
#: в том же месте — в этом и смысл единой ленты.
NOTE_KIND = "telegram"

#: Общий приставок настроек канала. Ни одна из них не уезжает в общий
#: `GET /settings`: там их не ждут, а токен там и вовсе не должен появляться.
SETTINGS_PREFIX = "telegram_"

#: Токен бота от @BotFather.
SETTING_TOKEN = "telegram_bot_token"
#: Куда слать утреннюю сводку. Идентификатор чата, а не имя: имя меняют.
SETTING_DIGEST_CHAT = "telegram_digest_chat"
#: Куда слать тревоги о сервере. Читает его наблюдатель
#: (`core/services/trevogi_service.py`), а живёт ключ здесь, вместе с остальными
#: настройками бота, и по двум причинам.
#:
#: Первая — это адрес, по которому пишет НАШ бот, то есть свойство канала, а не
#: наблюдения: сменили бота — сменились оба чата разом.
#:
#: Вторая важнее и она про порядок зависимостей. Наблюдатель уже знает про канал
#: (`trevogi_service` импортирует этот модуль ради отправки), обратной стрелки
#: нет и заводить её нельзя — вышло бы кольцо, и приём тревог начал бы зависеть
#: от того, что в нём не участвует.
SETTING_ALERTS_CHAT = "telegram_alerts_chat"
#: Секрет вебхука. Телеграм присылает его заголовком на каждый запрос, и это
#: единственное, чем приём отличает настоящий телеграм от того, кто узнал адрес.
SETTING_WEBHOOK_SECRET = "telegram_webhook_secret"
#: Имя бота — для ссылки `t.me/имя?start=метка`, которую владелец кладёт на сайт.
SETTING_USERNAME = "telegram_bot_username"

#: Токен выглядит как `123456789:AA...`: цифры, двоеточие, буквенно-цифровой
#: хвост. Проверяем форму, а не длину: telegram её менял, и жёсткая длина
#: однажды отвергнет настоящий токен.
MIN_TOKEN_LEN = 20
MAX_TOKEN_LEN = 200


def _hvost(token: str) -> str:
    """Чем показать токен, не показывая его.

    Последние четыре знака: их хватает, чтобы владелец узнал «тот ли токен я
    вставил», и не хватает ни для чего больше.
    """
    return token[-4:] if len(token) >= 4 else ""


def _vse(db: Session) -> dict[str, str]:
    """Настройки канала одним чтением, по приставке.

    По приставке, а не по одному ключу за раз: экран настроек спрашивает их все
    разом, и четыре отдельных запроса здесь были бы четырьмя обращениями к базе
    ради четырёх строк.
    """
    return {row.key: row.value for row in settings_repo.rows_with_prefix(db, SETTINGS_PREFIX)}


def nastroyki(db: Session) -> dict:
    """Что показать на экране настроек. БЕЗ секретов.

    Отдаётся признак «настроено» и хвост токена, а не токен. Причина простая:
    экран настроек открывает тот, у кого есть право `telegram.manage`, но
    ответ ручки уезжает в браузер, попадает в историю запросов и в отладчик, и
    оттуда его достать проще, чем из базы.
    """
    vse = _vse(db)
    token = vse.get(SETTING_TOKEN, "")
    return {
        "configured": bool(token),
        "token_tail": _hvost(token),
        "digest_chat": vse.get(SETTING_DIGEST_CHAT, ""),
        # Пустой чат тревог — рабочее состояние, а не пробел: наблюдатель шлёт
        # их тогда в чат сводки. Экран говорит об этом вслух, иначе пустое поле
        # читается как «тревоги никуда не идут».
        "alerts_chat": vse.get(SETTING_ALERTS_CHAT, ""),
        "bot_username": vse.get(SETTING_USERNAME, ""),
        "webhook_secret_set": bool(vse.get(SETTING_WEBHOOK_SECRET, "")),
        # Рост переписки: сколько лежит, во что обойдётся каждый срок хранения
        # и что сделал прошлый прогон уборки.
        #
        # Считает это отдельная служба (`core/services/telegram_uborka.py`):
        # «сколько хранить» и «как разговаривать с Bot API» — разные предметы, и
        # в одном файле правка одного задевала бы другое.
        #
        # Отдаётся здесь, а не своей ручкой, потому что читается ровно одним
        # экраном и ровно вместе с остальным: вторая ручка означала бы второй
        # запрос на каждое открытие настроек.
        "retention_months": telegram_uborka.srok(db),
        "rost": telegram_uborka.ves(db),
        "last_cleanup": telegram_uborka.posledniy_progon(db),
    }


def token(db: Session) -> str:
    """Токен для обращения к телеграму. Внутреннее употребление."""
    return _vse(db).get(SETTING_TOKEN, "")


def webhook_secret(db: Session) -> str:
    return _vse(db).get(SETTING_WEBHOOK_SECRET, "")


def digest_chat(db: Session) -> str:
    return _vse(db).get(SETTING_DIGEST_CHAT, "")


def _chat_id(value, pole: str) -> str:
    """Проверить идентификатор чата. Пусто — законно, это «не задан».

    Одной проверкой на оба чата: правило у них общее, а разъехавшись, они дали
    бы отказ на значении, которое соседнее поле принимает.

    Отрицательное число — не опечатка: у групп и каналов идентификаторы такие
    (`-1001234567890`), а служебный чат обычно как раз группа.
    """
    chat = str(value or "").strip()
    if chat and not chat.lstrip("-").isdigit():
        raise errors.ValidationError(
            f"{pole} must be a number, like 123456789 or -1001234567890",
            code="bad_chat_id",
        )
    return chat


def zadat(db: Session, data: dict) -> dict:
    """Записать настройки бота.

    Пустой токен в запросе означает «не меняй», а не «сотри»: экран показывает
    только хвост, и отправить обратно то, чего он не получал, он не может.
    Стирание — отдельным действием (`otklyuchit`), потому что «случайно
    сохранил пустую форму» не должно отключать канал.
    """
    izmeneniya: dict[str, str] = {}

    if "token" in data:
        syroy = str(data.get("token") or "").strip()
        if syroy:
            if not (MIN_TOKEN_LEN <= len(syroy) <= MAX_TOKEN_LEN) or ":" not in syroy:
                raise errors.ValidationError(
                    "Bot token looks wrong: expected the value from @BotFather, "
                    "like 123456789:AA...",
                    code="bad_bot_token",
                )
            izmeneniya[SETTING_TOKEN] = syroy
            # Секрет вебхука заводим вместе с токеном и не спрашиваем у
            # человека: он не выбирается, он генерируется. Спросить — значит
            # получить «12345» и открыть приём всем, кто угадает адрес.
            if not webhook_secret(db):
                izmeneniya[SETTING_WEBHOOK_SECRET] = secrets.token_urlsafe(32)

    if "digest_chat" in data:
        izmeneniya[SETTING_DIGEST_CHAT] = _chat_id(data.get("digest_chat"), "Digest chat id")

    if "alerts_chat" in data:
        izmeneniya[SETTING_ALERTS_CHAT] = _chat_id(data.get("alerts_chat"), "Alerts chat id")

    if "bot_username" in data:
        izmeneniya[SETTING_USERNAME] = str(data.get("bot_username") or "").strip().lstrip("@")[:64]

    if "retention_months" in data:
        # Отдельной службой, а не строкой рядом с токеном, и это не педантизм:
        # срок хранения — единственная настройка канала, которая СТИРАЕТ данные.
        # Проверка у неё своя: значение берётся из названного списка, потому что
        # экран показывает цену каждого срока, а спросить базу можно только про
        # заранее названные.
        telegram_uborka.zadat_srok(db, data.get("retention_months"))

    if izmeneniya:
        settings_repo.write_many(db, izmeneniya)
    return nastroyki(db)


def otklyuchit(db: Session) -> dict:
    """Убрать токен и секрет. Переписка при этом остаётся.

    Тот же довод, что у выключенного блока системы: отключение канала — это про
    связь, а не про данные. Стереть переписку с клиентами заодно значило бы
    сделать необратимым действие, которое человек считает обратимым.
    """
    settings_repo.write_many(db, {SETTING_TOKEN: "", SETTING_WEBHOOK_SECRET: ""})
    return nastroyki(db)


# --- разговор с телеграмом ---------------------------------------------------
#
# Своими руками на `urllib`, без новой зависимости. Довод тот же, по которому
# так сделан деплойный отправитель: библиотека ради четырёх вызовов — это ещё
# один пакет, который надо обновлять, и ещё одна причина, по которой сборка
# однажды не соберётся. Вызовов действительно четыре: послать текст, послать
# файл, узнать адрес файла, скачать его.

API = "https://api.telegram.org"

#: Сколько ждать телеграм. Секунды, а не минуты: приём вебхука обязан ответить
#: быстро — телеграм считает медленный ответ недоставкой и повторяет.
TIMEOUT = 10

#: Что забираем себе сразу, а что оставляем в телеграме.
#:
#: Картинки и документы до потолка — сразу: без них переписка в CRM неполная,
#: а менеджер полезет в свой телефон, то есть ровно туда, откуда общение и
#: уводили. Видео — по нажатию: гигабайт переписки за месяц съест диск, а за
#: диском в этом проекте уже однажды никто не следил, пока не стало поздно.
MAX_AUTO_DOWNLOAD = 20 * 1024 * 1024

#: Сколько телеграм принимает у бота НА ОТПРАВКУ.
#:
#: Пятьдесят мегабайт, и это предел самого телеграма — не наш и не подвинуть.
#: Пределов вокруг три и они разные: nginx пропускает 220 МБ,
#: `max_upload_mb` разрешает 200, а Bot API отказывает после 50.
#:
#: Отсюда правило: проверять НАШИМ пределом мало. Файл в сто мегабайт проходит
#: и nginx, и нашу проверку, человек ждёт всю загрузку — и получает отказ
#: телеграма чужими словами: «Request Entity Too Large». Так и случилось на
#: показе с видео.
#:
#: Поэтому предел назван здесь и проверяется ДО обращения к телеграму.
MAX_TELEGRAM_FILE = 50 * 1024 * 1024


class TelegramOtkaz(Exception):
    """Телеграм отказал. Отдельным видом, чтобы отличать от нашей поломки."""


def _vyzov(token: str, metod: str, polya: dict, opener=None) -> dict:
    """Один вызов Bot API. Возвращает `result` или бросает `TelegramOtkaz`."""
    import json
    import urllib.error
    import urllib.request

    dannye = json.dumps(polya).encode("utf-8")
    zapros = urllib.request.Request(
        f"{API}/bot{token}/{metod}",
        data=dannye,
        headers={"Content-Type": "application/json"},
    )
    otkryt = opener or urllib.request.urlopen
    try:
        with otkryt(zapros, timeout=TIMEOUT) as otvet:
            telo = json.loads(otvet.read().decode("utf-8"))
    except urllib.error.HTTPError as beda:
        # Телеграм объясняет отказ в теле, и объяснение нужно человеку: «бот
        # заблокирован пользователем» и «неверный токен» чинятся по-разному.
        try:
            telo = json.loads(beda.read().decode("utf-8"))
        except Exception:
            raise TelegramOtkaz(f"HTTP {beda.code}") from None
        raise TelegramOtkaz(str(telo.get("description") or f"HTTP {beda.code}")) from None
    except Exception as beda:  # noqa: BLE001 — сеть, DNS, таймаут
        raise TelegramOtkaz(str(beda)) from None

    if not telo.get("ok"):
        raise TelegramOtkaz(str(telo.get("description") or "unknown error"))
    return telo.get("result") or {}


#: По чему узнаётся отказ ИМЕННО из-за разметки.
#:
#: Телеграм отвечает на такое кодом 400 и описанием вида «can't parse entities:
#: Unsupported start tag ...». Кода у нас под рукой нет — `TelegramOtkaz` несёт
#: только описание, — поэтому узнаём по нему.
#:
#: Узко, а не «любой отказ», и это важно. Запасной путь шлёт сообщение ВТОРОЙ
#: раз; сработай он на таймауте, когда первое уже доехало, — в чат ушло бы два
#: сообщения об одной аварии. На отказе по разбору такого не бывает: телеграм
#: отбивает сообщение целиком, ничего не доставив.
_OTKAZ_RAZBORA = "can't parse"


def _bez_razmetki(text: str) -> str:
    """Тот же текст без тегов. Снимаем ровно теги, содержимое остаётся."""
    import re as _re

    return _re.sub(r"<[^>]+>", "", text)


def _s_razmetkoy(token: str, metod: str, dannye: dict, opener=None) -> dict:
    """Отправить оформленное, а на отказ по разбору — то же самое без тегов.

    **Запасной путь обязателен, и он один на всех.** Ошибка в оформлении не
    имеет права заглушить сообщение: молчание сводки читается как «всё
    сломалось», а неотправленная авария — хуже вдвойне, потому что о ней узнают
    не из чата, а от рассерженного клиента.

    Всё остальное (сеть, 429, отозванный токен) пробрасываем как есть: это не
    про оформление, и вторая попытка тем же телом ничего не изменит.
    """
    try:
        return _vyzov(token, metod, dannye, opener)
    except TelegramOtkaz as beda:
        if _OTKAZ_RAZBORA not in str(beda).lower():
            raise
        logger.warning("телеграм не разобрал разметку, шлём плоским текстом: %s", beda)
        ploskie = {k: v for k, v in dannye.items() if k != "parse_mode"}
        ploskie["text"] = _bez_razmetki(str(dannye.get("text", "")))
        return _vyzov(token, metod, ploskie, opener)


def poslat_tekst(
    token: str, chat_id: int, text: str, opener=None, otvet_na: int | None = None
) -> dict:
    """Отправить текст. Разметки нет намеренно.

    В сообщении клиенту разметка не нужна, а вред от неё реальный: текст пишет
    менеджер, в нём бывают `<`, `&` и звёздочки, и телеграм отбил бы сообщение
    целиком с `can't parse entities`. Здесь это означало бы, что клиент не
    получил ответ, а менеджер об этом не узнал.
    """
    dannye: dict = {"chat_id": chat_id, "text": text}
    if otvet_na is not None:
        # `allow_sending_without_reply` не ставим: если сообщение, на которое
        # отвечают, у клиента удалено, лучше узнать об этом отказом, чем молча
        # отправить ответ, повисший в воздухе. Менеджер увидит причину и
        # отправит заново без привязки.
        dannye["reply_to_message_id"] = otvet_na
    return _vyzov(token, "sendMessage", dannye, opener)


def _mnogochastnoe(polya: dict, fayl: tuple[str, str, bytes]) -> tuple[bytes, str]:
    """Тело `multipart/form-data` руками.

    Руками, потому что `urllib` этого не умеет, а тащить ради одного вызова
    целую библиотеку запросов незачем. Граница берётся случайной и длинной:
    совпади она с содержимым файла — тело разберётся неверно, и это тот отказ,
    который воспроизводится раз в жизни и не воспроизводится в тестах.
    """
    import secrets as _secrets

    granitsa = "----OpenCRM" + _secrets.token_hex(16)
    imya_polya, imya_fayla, soderzhimoe = fayl
    kuski: list[bytes] = []
    for klyuch, znachenie in polya.items():
        kuski.append(
            f'--{granitsa}\r\nContent-Disposition: form-data; name="{klyuch}"\r\n\r\n'
            f"{znachenie}\r\n".encode("utf-8")
        )
    kuski.append(
        f'--{granitsa}\r\nContent-Disposition: form-data; name="{imya_polya}"; '
        f'filename="{imya_fayla}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode("utf-8")
    )
    kuski.append(soderzhimoe)
    kuski.append(f"\r\n--{granitsa}--\r\n".encode("utf-8"))
    return b"".join(kuski), f"multipart/form-data; boundary={granitsa}"


def poslat_fayl(
    token: str,
    chat_id: int,
    vid: str,
    imya: str,
    soderzhimoe: bytes,
    podpis: str = "",
    opener=None,
    otvet_na: int | None = None,
) -> dict:
    """Отправить картинку, видео или документ."""
    import json
    import urllib.error
    import urllib.request

    metod = {"photo": "sendPhoto", "video": "sendVideo"}.get(vid, "sendDocument")
    imya_polya = {"photo": "photo", "video": "video"}.get(vid, "document")

    polya = {"chat_id": str(chat_id)}
    if podpis:
        polya["caption"] = podpis
    if otvet_na is not None:
        # В multipart всё уезжает строками — число здесь превратилось бы в
        # `TypeError` при сборке тела, а не в отказ телеграма.
        polya["reply_to_message_id"] = str(otvet_na)
    telo, tip = _mnogochastnoe(polya, (imya_polya, imya, soderzhimoe))

    zapros = urllib.request.Request(
        f"{API}/bot{token}/{metod}", data=telo, headers={"Content-Type": tip}
    )
    otkryt = opener or urllib.request.urlopen
    try:
        with otkryt(zapros, timeout=TIMEOUT * 6) as otvet:
            razobrano = json.loads(otvet.read().decode("utf-8"))
    except urllib.error.HTTPError as beda:
        try:
            razobrano = json.loads(beda.read().decode("utf-8"))
        except Exception:
            raise TelegramOtkaz(f"HTTP {beda.code}") from None
        raise TelegramOtkaz(str(razobrano.get("description") or f"HTTP {beda.code}")) from None
    except Exception as beda:  # noqa: BLE001
        raise TelegramOtkaz(str(beda)) from None

    if not razobrano.get("ok"):
        raise TelegramOtkaz(str(razobrano.get("description") or "unknown error"))
    return razobrano.get("result") or {}


def skachat_fayl(token: str, file_id: str, opener=None, srok: int | None = None) -> bytes:
    """Забрать файл к себе: сначала узнать путь, потом скачать.

    Два обращения, потому что так устроен Bot API: `getFile` отдаёт временный
    путь, и только по нему файл доступен. Путь живёт около часа — хранить его
    негде и незачем.

    `srok` укорачивают там, где ждать нельзя. Вложение клиента бывает в
    десятки мегабайт, и минута на него законна; аватар весит десятки килобайт,
    а тянут его внутри приёма сообщения — там минута ожидания означала бы, что
    телеграм сочтёт доставку неудавшейся и пришлёт сообщение повторно.
    """
    import urllib.request

    svedeniya = _vyzov(token, "getFile", {"file_id": file_id}, opener)
    put = svedeniya.get("file_path")
    if not put:
        raise TelegramOtkaz("телеграм не назвал путь к файлу")

    otkryt = opener or urllib.request.urlopen
    zapros = urllib.request.Request(f"{API}/file/bot{token}/{put}")
    try:
        with otkryt(zapros, timeout=srok or TIMEOUT * 6) as otvet:
            return otvet.read()
    except Exception as beda:  # noqa: BLE001
        raise TelegramOtkaz(str(beda)) from None


def zadat_vebkhuk(token: str, adres: str, sekret: str, opener=None) -> dict:
    """Сказать телеграму, куда слать обновления.

    `secret_token` — то единственное, чем приём отличает настоящий телеграм от
    того, кто узнал адрес. Телеграм присылает его заголовком на каждом запросе.
    """
    return _vyzov(
        token,
        "setWebhook",
        {
            "url": adres,
            "secret_token": sekret,
            # Только сообщения: остального нам не нужно, а лишние обновления —
            # это лишние запросы к нашему приёму.
            "allowed_updates": ["message", "callback_query"],
            # Старые необработанные обновления при подключении выбрасываем:
            # иначе первое включение вывалит в CRM всё, что копилось у
            # телеграма, задним числом и одним залпом.
            "drop_pending_updates": True,
        },
        opener,
    )


# --- приём входящих ----------------------------------------------------------

def _kod_strany(db: Session) -> str:
    """Код страны для нормализации номера — общая настройка контактов.

    Та же, что у телефонии и заявок с сайта: «067…» и «+380 67…» обязаны
    считаться одним человеком во всех трёх каналах, иначе переписка сядет не на
    ту карточку.
    """
    return {row.key: row.value for row in settings_repo.rows_with_prefix(db, "default_")}.get(
        "default_country_code", ""
    )


def _imya_iz(otpravitel: dict) -> str:
    """Как подписать диалог, если клиент ещё не привязан к карточке."""
    chasti = [str(otpravitel.get("first_name") or ""), str(otpravitel.get("last_name") or "")]
    imya = " ".join(c for c in chasti if c).strip()
    return imya or str(otpravitel.get("username") or "") or "Telegram"


def _vlozhenie(soobshchenie: dict) -> tuple[str, str, str, int]:
    """Что за вложение: (вид, file_id, имя, размер). Текст — вид `text`.

    Картинка приходит НАБОРОМ размеров, от миниатюры до оригинала. Берём
    последний: телеграм отдаёт их по возрастанию, и предпоследний — это
    предпросмотр, который в переписке выглядит как испорченное фото.
    """
    if soobshchenie.get("photo"):
        krupneyshee = soobshchenie["photo"][-1]
        return (
            KIND_PHOTO,
            str(krupneyshee.get("file_id") or ""),
            "photo.jpg",
            int(krupneyshee.get("file_size") or 0),
        )
    for klyuch, vid, imya_po_umolchaniyu in (
        ("video", KIND_VIDEO, "video.mp4"),
        ("voice", KIND_VOICE, "voice.ogg"),
        ("document", KIND_DOCUMENT, "file"),
    ):
        chast = soobshchenie.get(klyuch)
        if chast:
            return (
                vid,
                str(chast.get("file_id") or ""),
                str(chast.get("file_name") or imya_po_umolchaniyu),
                int(chast.get("file_size") or 0),
            )
    return KIND_TEXT, "", "", 0


# --- кнопки под сообщением ----------------------------------------------------
#
# Кнопки нужны там, где ответ должен быть В ОДНО ДЕЙСТВИЕ и без переключения
# окна: «принял тревогу», «заглушить на час». Всё, что сложнее, — это разговор,
# а разговор ведут словами, а не кнопками.


def poslat_s_knopkami(
    token: str,
    chat_id: int,
    text: str,
    knopki: list[list[dict]],
    opener=None,
    tiho: bool = False,
    razmetka: str | None = None,
) -> dict:
    """Сообщение с клавиатурой под ним.

    `knopki` — ряды кнопок; у каждой `text` и `callback_data`. В `callback_data`
    телеграм отводит **64 байта**, и это не рекомендация: длиннее — отказ всего
    сообщения. Поэтому туда кладут короткий опознаватель («ack:42»), а не
    состояние.

    `razmetka` — необязательная и по умолчанию пустая. Умолчание здесь не
    лень, а защита: тем же вызовом отвечают клиенту, а его текст сочиняет
    менеджер, и первое же «<» в нём отбило бы сообщение целиком. Включают
    разметку там, где текст сочиняем мы сами и всё чужое в нём экранируем, —
    сейчас это тревоги мониторинга.
    """
    dannye: dict = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps({"inline_keyboard": knopki}),
    }
    if tiho:
        # Без звука. Нужно предупреждениям: звенящее ночью «место на диске к
        # концу месяца кончится» учит выключать звук всему чату — вместе с
        # авариями, ради которых чат и заведён.
        dannye["disable_notification"] = True
    if not razmetka:
        return _vyzov(token, "sendMessage", dannye, opener)
    dannye["parse_mode"] = razmetka
    return _s_razmetkoy(token, "sendMessage", dannye, opener)


def otvetit_na_nazhatie(
    token: str, callback_id: str, tekst: str = "", opener=None
) -> dict:
    """Погасить «часики» у нажатой кнопки.

    Обязательный шаг, а не вежливость: пока телеграм не получил ответ на
    `callback_query`, у нажавшего крутится ожидание, и через несколько секунд
    он нажимает второй раз. Отвечать надо ДАЖЕ на нажатие, которое мы решили
    не выполнять, — иначе человек думает, что не сработало.
    """
    dannye: dict = {"callback_query_id": callback_id}
    if tekst:
        # Всплывающая подсказка вместо тишины: «принято» и «уже принято другим»
        # различаются только этим текстом.
        dannye["text"] = tekst[:200]
    return _vyzov(token, "answerCallbackQuery", dannye, opener)


def perepisat_soobshchenie(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    knopki=None,
    opener=None,
    razmetka: str | None = None,
) -> dict:
    """Переписать уже отправленное сообщение — обычно чтобы убрать кнопки.

    Тревога, которую приняли, обязана перестать выглядеть непринятой: иначе
    второй дежурный жмёт «принять» по той же самой, а третий идёт её чинить.

    `razmetka` — та же, что при отправке, и передавать её обязательно: сообщение
    собирается заново целиком, и переписать оформленное неоформленным значит
    показать читателю голые теги вместо аварии.
    """
    dannye: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    dannye["reply_markup"] = json.dumps({"inline_keyboard": knopki or []})
    if not razmetka:
        return _vyzov(token, "editMessageText", dannye, opener)
    dannye["parse_mode"] = razmetka
    return _s_razmetkoy(token, "editMessageText", dannye, opener)


#: Обработчики нажатий: приставка `callback_data` → что сделать.
#:
#: Реестром, а не цепочкой `if` в приёме, и это не украшение. Приём обновлений —
#: место, куда телеграм стучится каждым сообщением клиента, и всякая новая
#: кнопка там означала бы правку самого горячего кода канала. Реестр позволяет
#: соседнему разделу (тревогам, например) подписаться у себя и не трогать приём
#: вовсе.
#:
#: Обработчик получает `(db, nazhatie)` и возвращает текст всплывающей подсказки
#: либо пустую строку.
NAZHATIYA: dict[str, object] = {}


def podpisatsya_na_knopku(pristavka: str, obrabotchik) -> None:
    """Заявить обработчик нажатий с такой приставкой в `callback_data`."""
    NAZHATIYA[pristavka] = obrabotchik


def _prinyat_nazhatie(db: Session, nazhatie: dict, opener=None) -> dict:
    """Разобрать нажатие кнопки и позвать того, кто его ждал.

    Ответ телеграму уходит В ЛЮБОМ случае, включая неизвестную кнопку: пока
    ответа нет, у нажавшего крутятся часики. Неизвестная кнопка — обычное дело
    после обновления: сообщение со старыми кнопками осталось в чате навсегда.
    """
    dannye = str(nazhatie.get("data") or "")
    pristavka = dannye.split(":", 1)[0]
    obrabotchik = NAZHATIYA.get(pristavka)

    podskazka = ""
    if obrabotchik is None:
        podskazka = "This button is no longer available"
    else:
        try:
            podskazka = obrabotchik(db, nazhatie) or ""
        except Exception as beda:  # noqa: BLE001
            logger.exception("обработчик нажатия упал: %r", beda)
            podskazka = "Could not do that"

    kluch = token(db)
    if kluch and nazhatie.get("id"):
        try:
            otvetit_na_nazhatie(kluch, str(nazhatie["id"]), podskazka, opener)
        except Exception as beda:  # noqa: BLE001
            # Часики у нажавшего погаснут сами через несколько секунд. Ронять
            # из-за этого разбор нажатия незачем.
            logger.warning("не ответили на нажатие: %r", beda)

    return {"status": "callback", "data": dannye}


def prinyat(db: Session, obnovlenie: dict, opener=None) -> dict:
    """Одно обновление от телеграма. Отвечает, что с ним сделали.

    **Идемпотентность обязательна, а не желательна.** Телеграм повторяет
    доставку, пока не получит 200: обрыв сети, наш перезапуск, медленный ответ —
    и то же сообщение приходит снова. Без защиты клиент увидел бы в CRM две
    копии своей фразы, а менеджер ответил бы дважды.

    Защит две, и они разные по природе. Строка диалога берётся ПОД ЗАМКОМ
    (`vzyat_pod_pravku`) — иначе два одновременных обновления оба увидят «чата
    нет» и заведут два. А само сообщение отсеивается по идентификатору
    телеграма: он у сообщения уникален и не меняется.
    """
    # Нажатие кнопки приходит тем же вебхуком, что и сообщение, но устроено
    # иначе: у него нет ни текста, ни вида, ни чата в привычном смысле. Разбор
    # у него свой и живёт отдельно — здесь только развилка.
    if obnovlenie.get("callback_query"):
        return _prinyat_nazhatie(db, obnovlenie["callback_query"], opener)

    soobshchenie = obnovlenie.get("message") or {}
    chat = soobshchenie.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id or chat.get("type") != "private":
        # Групп и каналов у этого канала нет по устройству: общение один на
        # один. Молча пропускаем, а не отказываем: телеграм на отказ ответит
        # повтором, и мы получим тот же мусор ещё раз.
        return {"status": "ignored", "reason": "not_private"}

    otpravitel = soobshchenie.get("from") or {}
    kogda = datetime.utcfromtimestamp(int(soobshchenie.get("date") or 0)) if soobshchenie.get(
        "date"
    ) else now_utc().replace(tzinfo=None)

    dialog = telegram_repo.vzyat_pod_pravku(db, int(chat_id))
    if dialog is None:
        try:
            dialog = telegram_repo.create_chat(
                db,
                chat_id=int(chat_id),
                username=str(otpravitel.get("username") or "")[:64],
                title=_imya_iz(otpravitel)[:200],
            )
            # Фиксируем сразу, чтобы уникальность сработала ЗДЕСЬ, а не на
            # выходе из запроса, где ловить её уже поздно.
            db.flush()
        except IntegrityError:
            # Диалог завёл кто-то одновременно с нами. `FOR UPDATE` этого не
            # предотвращает: запирать нечего, строки ещё нет, — и оба обновления
            # видят «чата нет». Первое сообщение нового клиента приходит именно
            # так: «/start» и вопрос двумя обновлениями подряд.
            #
            # Откатываемся и перечитываем уже под замком: чужая строка к этому
            # моменту зафиксирована.
            db.rollback()
            dialog = telegram_repo.vzyat_pod_pravku(db, int(chat_id))
            if dialog is None:
                raise
    else:
        # Имя и логин обновляем: человек их меняет, а переписка обязана
        # оставаться узнаваемой.
        dialog.username = str(otpravitel.get("username") or "")[:64] or dialog.username
        dialog.title = _imya_iz(otpravitel)[:200] or dialog.title
    # Премиум — с каждым сообщением, у нового диалога тоже: он кончается и
    # продлевается, а стоит нам ноль вызовов, потому что едет в самом сообщении.
    dialog.is_premium = bool(otpravitel.get("is_premium"))

    vneshniy = soobshchenie.get("message_id")
    if vneshniy is not None and telegram_repo.po_vneshnemu_id(db, dialog.id, int(vneshniy)):
        return {"status": "duplicate", "chat_id": dialog.id}

    # Клиент поделился номером кнопкой — единственная точная привязка.
    #
    # Номер записывается в диалог ВСЕГДА, даже если карточка уже привязана: это
    # факт о собеседнике, а не способ его найти. Прежде разбор стоял целиком
    # под условием «карточки ещё нет», и у привязанного диалога телефон не
    # появлялся никогда — а именно им и хотят потом дополнить карточку.
    kontakt = soobshchenie.get("contact") or {}
    if kontakt.get("phone_number"):
        nomer = str(kontakt["phone_number"])
        dialog.phone = nomer[:64]
        dialog.phone_norm = normalize_phone(nomer, _kod_strany(db))[:32]
        # А вот ПРИВЯЗКА по номеру — только у непривязанного. Иначе поделившийся
        # чужим контактом (кнопка отдаёт любой из адресной книги) уводил бы
        # разговор на чужую карточку.
        if not dialog.client_id:
            klient = telegram_repo.find_client_by_phone(db, dialog.phone_norm)
            if klient is not None:
                dialog.client_id = klient.id

    tekst = str(soobshchenie.get("text") or soobshchenie.get("caption") or "")

    # Метка из ссылки `t.me/бот?start=метка` — откуда клиент пришёл.
    if tekst.startswith("/start") and not dialog.source:
        chasti = tekst.split(maxsplit=1)
        if len(chasti) == 2:
            dialog.source = chasti[1].strip()[:64]

    vid, file_id, imya_fayla, razmer = _vlozhenie(soobshchenie)

    # Файл здесь НЕ качаем. Строка заводится без него, а вложение приезжает
    # следом, вторым шагом, уже без открытой транзакции — см. `_dokachat` ниже.
    put_fayla = ""

    # На что отвечает клиент. Телеграм присылает целое сообщение, но нам нужен
    # только его номер: по нему находим СВОЮ запись. Не нашли — привязки не
    # будет, и это законно: отвечать могут на сообщение, которого у нас нет
    # (переписка началась до подключения канала).
    otvet_na = None
    otvechayut = soobshchenie.get("reply_to_message") or {}
    if otvechayut.get("message_id"):
        nayden = telegram_repo.po_vneshnemu_id(db, dialog.id, int(otvechayut["message_id"]))
        otvet_na = nayden.id if nayden else None

    stroka = telegram_repo.dobavit_soobshchenie(
        db,
        chat_id=dialog.id,
        reply_to_id=otvet_na,
        external_id=int(vneshniy) if vneshniy is not None else None,
        direction=DIRECTION_IN,
        kind=vid,
        body=tekst,
        file_id=file_id,
        file_path=put_fayla,
        # Имя нужно и у незабранного файла: без него в переписке стояло бы
        # безымянное «видео», и человек не понял бы, что именно ему прислали.
        file_name=imya_fayla if (put_fayla or file_id) else "",
        file_size=razmer or None,
        happened_at=kogda,
        send_state=SEND_SENT,
    )
    _zapis_v_lentu(db, dialog, stroka)
    # В шину — ПОСЛЕ ФИКСАЦИИ, а не после записи. Разница ровно та, о которой
    # говорил прежний комментарий на этом месте: «объявить раньше значило бы
    # разослать „пришло новое“, по которому экран сходил бы в базу и ничего не
    # нашёл». Довод был верен, а код ему не соответствовал: объявление стояло
    # здесь, а `db.commit()` — двадцатью строками ниже.
    #
    # Отказ шины сюда не поднимается: сообщение уже записано, и ронять приём
    # из-за недоступного Redis значило бы менять надёжное на быстрое. Телеграм
    # на такой отказ ответил бы повтором.
    realtime.obyavit_posle_fiksatsii(
        db,
        "message",
        chat_id=dialog.id,
        message_id=stroka.id,
        direction=DIRECTION_IN,
        preview=(stroka.body or "")[:80],
        # Имя собеседника едет в событии, а не добывается экраном по chat_id:
        # уведомление показывается сразу, а список диалогов приезжает позже, и
        # ждать его значило бы показать «Telegram» вместо имени.
        title=dialog.title or dialog.username or str(dialog.chat_id),
    )

    # ЗДЕСЬ транзакция закрывается, и это главное решение всей функции.
    #
    # Всё, что выше, — короткая работа с базой под замком строки диалога.
    # Всё, что ниже, — разговор с телеграмом, и он идёт уже БЕЗ замка и без
    # занятого соединения. Держали бы, как раньше: три клиента с фотографиями —
    # три соединения из десяти на минуту, телеграм повторяет недождавшуюся
    # доставку, повторы встают на тот же `FOR UPDATE`, и «QueuePool limit»
    # накрывает всё приложение вместе с проверкой здоровья.
    db.commit()

    nomera = {"chat_id": dialog.id, "message_id": stroka.id}
    kluch = token(db)

    # Вложение вторым шагом. Событие в шину уже ушло — экран покажет сообщение
    # сразу, а картинка появится следующим событием, которое шлёт `_dokachat`.
    if kluch and file_id and vid != KIND_VIDEO and 0 < razmer <= MAX_AUTO_DOWNLOAD:
        _dokachat(db, dialog, stroka, kluch, file_id, imya_fayla, opener)

    # Аватар и именной эмодзи — третьим шагом, не чаще раза в сутки на диалог.
    if kluch and avatar_ustarel(dialog):
        obnovit_avatar(db, dialog, kluch, opener)
        # Эмодзи только у премиума: у остальных его не бывает вовсе, и два
        # вызова ушли бы впустую на каждого.
        if dialog.is_premium:
            obnovit_status_emodzi(db, dialog, kluch, opener)
        db.commit()

    return {"status": "accepted", **nomera}


def _dokachat(db, dialog, stroka, kluch, file_id, imya_fayla, opener=None) -> None:
    """Забрать вложение и дописать его к уже записанному сообщению.

    Отдельным шагом ПОСЛЕ фиксации сообщения, а не внутри неё. Порядок стоит
    объяснить, потому что он выглядит лишней работой:

    - сообщение уже записано и уже объявлено — клиента слышно сразу, даже если
      его фотография едет минуту;
    - соединение к базе на время скачивания свободно;
    - не забрали (сеть, 429, файл пропал) — текст всё равно на месте, а вложение
      остаётся забираемым по кнопке, как видео.

    Второе объявление в шину нужно затем, что первое ушло без файла: лента на
    экране складывается по `id` идемпотентно, и повторное событие просто
    перечитывает ту же строку — теперь уже с картинкой.
    """
    try:
        soderzhimoe = skachat_fayl(kluch, file_id, opener)
        stroka.file_path = _polozhit_fayl(dialog, imya_fayla, soderzhimoe)
        db.commit()
    except Exception as beda:  # noqa: BLE001
        # Откатываем СВОЮ незавершённую правку: без этого сессия остаётся в
        # сломанной транзакции, и следующий же запрос по ней получает отказ.
        db.rollback()
        logger.warning("не забрали файл из телеграма: %r", beda)
        return

    realtime.obyavit(
        "message",
        chat_id=dialog.id,
        message_id=stroka.id,
        direction=DIRECTION_IN,
        preview=(stroka.body or "")[:80],
        title=dialog.title or dialog.username or str(dialog.chat_id),
    )


def _polozhit_fayl(
    dialog, imya: str, soderzhimoe: bytes, imya_kak_est: bool = False
) -> str:
    """Положить файл переписки на диск и вернуть относительный путь.

    В своём каталоге канала, а не в общем хранилище клиентских файлов, и это
    решение. Файл из переписки принадлежит ДИАЛОГУ, а диалог бывает не привязан
    ни к какой карточке — то есть класть его в «файлы клиента» просто некуда.
    Привяжут диалог позже — файл никуда не переедет, ссылка не сломается.
    """
    import uuid as _uuid

    koren = get_settings().storage_dir / "telegram" / str(dialog.chat_id)
    koren.mkdir(parents=True, exist_ok=True)
    # Имя своё, а не присланное: имя из телеграма приходит от постороннего и
    # бывает чем угодно, включая `../`. Расширение сохраняем — по нему браузер
    # понимает, чем открыть.
    rasshirenie = ("." + imya.rsplit(".", 1)[-1][:10]) if "." in imya else ""
    # Аватар кладётся под постоянным именем: он у диалога один, и случайное имя
    # означало бы новую копию на диске раз в сутки на каждого собеседника.
    # Для всего остального имя своё — присланное приходит от постороннего и
    # бывает чем угодно, включая `../`.
    svoyo = imya if imya_kak_est else f"{_uuid.uuid4().hex}{rasshirenie}"
    (koren / svoyo).write_bytes(soderzhimoe)
    return f"telegram/{dialog.chat_id}/{svoyo}"


def _zapis_v_lentu(db: Session, dialog, stroka) -> None:
    """Сообщение попадает в ленту клиента — если диалог к карточке привязан.

    Не привязан — записи нет: лента принадлежит карточке, а класть переписку
    неизвестно чью неизвестно куда нельзя.

    Автор пуст у входящих: их написал клиент, человека с нашей стороны за
    записью нет. Это тот же законный случай, что у звонка из АТС.
    """
    if not dialog.client_id:
        return
    telo = stroka.body or {
        KIND_PHOTO: "Photo",
        KIND_VIDEO: "Video",
        KIND_VOICE: "Voice message",
        KIND_DOCUMENT: "File",
    }.get(stroka.kind, "Message")
    db.add(
        ClientNote(
            client_id=dialog.client_id,
            author_id=stroka.author_id,
            kind=NOTE_KIND,
            direction=stroka.direction,
            body=telo[:2000],
            happened_at=stroka.happened_at,
        )
    )


# --- отправка ----------------------------------------------------------------

def otpravit(
    db: Session,
    chat_row_id: int,
    *,
    tekst: str = "",
    author=None,
    fayl: tuple[str, bytes] | None = None,
    opener=None,
    otvet_na: int | None = None,
):
    """Ответить клиенту. Строка заводится ДО обращения к телеграму.

    Порядок здесь — вся защита, и он противоположен привычному «сделал —
    записал».

    **Почему сначала запись.** Отправка идёт по сети и длится сотни
    миллисекунд. Нажали дважды — второй запрос приходит, пока первый ещё в
    пути. Записав ПОСЛЕ отправки, мы отправили бы дважды, и это увидел бы
    клиент: в переписке двойное сообщение выглядит как невнимательность фирмы.
    Записав ДО, мы имеем строку, по которой видно, что отправка уже идёт.

    **Почему отказ телеграма не откатывает запись.** Менеджер обязан увидеть,
    что его ответ не ушёл, — иначе он уверен, что ответил, и ждёт реакции.
    Строка остаётся с пометкой «не отправлено» и причиной от телеграма
    («бот заблокирован пользователем» чинится не так, как «неверный токен»).
    """
    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")

    tekst = (tekst or "").strip()
    if not tekst and fayl is None:
        raise errors.ValidationError("Message is empty", code="message_empty")

    kluch = token(db)
    if not kluch:
        raise errors.ValidationError(
            "Telegram bot is not configured", code="telegram_not_configured"
        )

    vid = KIND_TEXT
    imya_fayla = ""
    put_fayla = ""
    if fayl is not None:
        imya_fayla, soderzhimoe = fayl
        # ДО того, как класть файл на диск и заводить строку: иначе в переписке
        # останется сообщение «не доставлено» о том, что и не могло уйти, и
        # место на диске займёт файл, которого клиент никогда не увидит.
        if len(soderzhimoe) > MAX_TELEGRAM_FILE:
            raise errors.ValidationError(
                f"Telegram accepts files up to {_v_megabaytah(MAX_TELEGRAM_FILE)} MB, "
                f"and this one is {_v_megabaytah(len(soderzhimoe))} MB. "
                "Send a link or a smaller file.",
                code="telegram_file_too_large",
            )
        vid = _vid_po_imeni(imya_fayla)
        put_fayla = _polozhit_fayl(dialog, imya_fayla, soderzhimoe)

    # На что отвечаем. Проверяем ЗДЕСЬ, а не доверяем номеру из запроса: чужой
    # номер иначе привязал бы ответ к переписке другого клиента, и цитата в
    # ленте показала бы чужие слова.
    otvet_na_stroku = None
    if otvet_na is not None:
        nayden = telegram_repo.po_id(db, otvet_na)
        if nayden is None or nayden.chat_id != dialog.id:
            raise errors.ValidationError(
                "Message to reply to is not in this chat", code="telegram_reply_not_here"
            )
        otvet_na_stroku = nayden

    stroka = telegram_repo.dobavit_soobshchenie(
        db,
        chat_id=dialog.id,
        reply_to_id=otvet_na_stroku.id if otvet_na_stroku else None,
        direction=DIRECTION_OUT,
        kind=vid,
        body=tekst,
        file_path=put_fayla,
        file_name=imya_fayla if put_fayla else "",
        file_size=len(fayl[1]) if fayl is not None else None,
        author_id=getattr(author, "id", None),
        happened_at=now_utc().replace(tzinfo=None),
        send_state=SEND_PENDING,
    )

    try:
        # Номер в телеграме, а не наш: привязку понимает он, и у него своя
        # нумерация. У сообщения, которое к нам приехало без номера (такое
        # бывает у старых записей), привязки не будет — лучше отправить без
        # цитаты, чем не отправить вовсе.
        vneshniy_otveta = otvet_na_stroku.external_id if otvet_na_stroku else None
        if fayl is not None:
            itog = poslat_fayl(
                kluch, dialog.chat_id, vid, imya_fayla, fayl[1], tekst, opener,
                otvet_na=vneshniy_otveta,
            )
        else:
            itog = poslat_tekst(
                kluch, dialog.chat_id, tekst, opener, otvet_na=vneshniy_otveta
            )
    except TelegramOtkaz as beda:
        stroka.send_state = SEND_FAILED
        stroka.send_error = str(beda)[:255]
        logger.warning("телеграм не принял ответ в диалоге %s: %s", dialog.id, beda)
        return stroka

    stroka.send_state = SEND_SENT
    stroka.send_error = ""
    vneshniy = itog.get("message_id")
    if vneshniy is not None:
        stroka.external_id = int(vneshniy)
    _zapis_v_lentu(db, dialog, stroka)
    # Отложенно: своей фиксации у этой функции нет вовсе — транзакцию
    # закрывает запрос (`web/api/deps._edinica_raboty`), уже после возврата
    # отсюда. Объявить прямо здесь значило бы сказать «отправлено» о строке,
    # которой в базе ещё нет.
    realtime.obyavit_posle_fiksatsii(
        db,
        "message",
        chat_id=dialog.id,
        message_id=stroka.id,
        direction=DIRECTION_OUT,
        preview=(stroka.body or "")[:80],
    )
    return stroka


def _v_megabaytah(baytov: int) -> str:
    """Байты в мегабайты с одним знаком — для человеческого сообщения.

    Целочисленным делением файл в 50.5 МБ назывался бы «50 МБ», то есть отказ
    сообщал бы «нельзя больше 50» про число, которое сам же назвал пятьюдесятью.
    Десятые доли считаем целыми: делить на float здесь незачем, а привычка
    держать деньги и количества в целых стоит того, чтобы не заводить исключений
    по мелочи.
    """
    desyatye = baytov * 10 // (1024 * 1024)
    return f"{desyatye // 10}.{desyatye % 10}"


def _vid_po_imeni(imya: str) -> str:
    """Картинка, видео или документ — по расширению.

    По расширению, а не по содержимому, и это осознанное упрощение: здесь файл
    приходит от СВОЕГО сотрудника через свою же форму, а не от постороннего.
    Ошибка вида означает, что телеграм покажет картинку файлом, — неприятно и
    безвредно. У входящих всё иначе, там вид называет сам телеграм.
    """
    hvost = imya.rsplit(".", 1)[-1].lower() if "." in imya else ""
    if hvost in ("jpg", "jpeg", "png", "gif", "webp"):
        return KIND_PHOTO
    if hvost in ("mp4", "mov", "webm"):
        return KIND_VIDEO
    return KIND_DOCUMENT


def obnovit_status_emodzi(db: Session, dialog, kluch: str, opener=None) -> str:
    """Забрать картинку именного эмодзи собеседника.

    **Два вызова, и оба только для премиума.** Именной эмодзи бывает только у
    него — значит у остальных спрашивать нечего, и `is_premium` из самого
    сообщения работает здесь отбором, который ничего не стоит.

    Первый вызов — `getChat`: в объекте пользователя (`message.from`) именного
    эмодзи НЕТ, он приходит только у чата. Второй — `getCustomEmojiStickers`:
    он превращает идентификатор в стикер, у которого есть статичная миниатюра.

    Тянем именно миниатюру, а не сам стикер: стикер — это Lottie, и рисовать
    его нечем без новой библиотеки во фронтенде.

    Отказы наружу не поднимаются: значок у имени — украшение, и уронить из-за
    него приём сообщения значило бы обменять важное на неважное.
    """
    try:
        chat = _vyzov(kluch, "getChat", {"chat_id": dialog.chat_id}, opener)
        opoznavatel = chat.get("emoji_status_custom_emoji_id")
        if not opoznavatel:
            # Сняли значок — снимаем и у себя. Иначе он висел бы у имени вечно.
            dialog.emoji_status_path = ""
            return ""

        stikery = _vyzov(
            kluch,
            "getCustomEmojiStickers",
            {"custom_emoji_ids": json.dumps([opoznavatel])},
            opener,
        )
        if not stikery:
            dialog.emoji_status_path = ""
            return ""

        miniatyura = (stikery[0] or {}).get("thumbnail") or {}
        if not miniatyura.get("file_id"):
            # Стикер без миниатюры рисовать нечем. Не отказ: просто нечего
            # показать, и значок останется прежним видом — премиумной звёздочкой.
            dialog.emoji_status_path = ""
            return ""

        soderzhimoe = skachat_fayl(kluch, miniatyura["file_id"], opener, srok=TIMEOUT)
        dialog.emoji_status_path = _polozhit_fayl(
            dialog, "emoji.webp", soderzhimoe, imya_kak_est=True
        )
        return dialog.emoji_status_path
    except Exception as beda:  # noqa: BLE001
        logger.warning("не забрали именной эмодзи: %r", beda)
        return dialog.emoji_status_path or ""


def emodzi_fayl(db: Session, chat_row_id: int):
    """Путь к забранной картинке именного эмодзи. Только чтение с диска.

    Тот же довод, что у аватара: разговор с телеграмом живёт в приёме сообщения,
    а ручка показа читает файл. Иначе первый показ списка означал бы стадо
    запросов, занятый пул соединений и 429 в ответ.
    """
    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")
    if not dialog.emoji_status_path:
        raise errors.NotFoundError("No emoji status", code="telegram_no_emoji")

    put = get_settings().storage_dir / dialog.emoji_status_path
    if not put.exists():
        raise errors.NotFoundError("No emoji status", code="telegram_no_emoji")
    return put


def privyazat_klienta(db: Session, chat_row_id: int, client_id: int | None):
    """Связать диалог с карточкой — руками, по решению человека.

    Руками и есть главное. Автоматическая привязка возможна ровно одна: точное
    совпадение нормализованного номера, когда клиент сам поделился контактом.
    Всё остальное — совпадение имени, похожий логин — запрещено, и это
    оплаченный урок: в заказах привязка по частичному совпадению имени уводила
    деньги и товар на чужую карточку. Здесь ценой была бы переписка, которую
    читает не тот человек.
    """
    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")
    dialog.client_id = client_id
    return dialog


def podklyuchit(db: Session, opener=None) -> dict:
    """Сказать телеграму, куда доставлять. Без этого канал молчит.

    **Почему отдельным действием, а не при сохранении токена.** Адрес приёма
    зависит от того, как сайт виден снаружи, а не от токена. Владелец меняет
    домен, ставит сертификат, переезжает на другой сервер — и каждый раз надо
    сказать телеграму заново. Прицепи мы это к сохранению токена, единственным
    способом переподключиться было бы перевводить токен.

    Адрес берётся из `base_url` — того самого, которым владелец объявил, как
    сайт виден снаружи (по нему же строятся публичные ссылки на доски).
    Телеграм требует HTTPS и проверяет сертификат: по http он вебхук не
    примет, и отказ придёт понятный.
    """
    kluch = token(db)
    if not kluch:
        raise errors.ValidationError(
            "Telegram bot is not configured", code="telegram_not_configured"
        )
    sekret = webhook_secret(db)
    if not sekret:
        raise errors.ValidationError(
            "Webhook secret is missing — save the bot token again",
            code="telegram_secret_missing",
        )

    osnova = get_settings().base_url.rstrip("/")
    if not osnova.startswith("https://"):
        # Телеграм принимает вебхук только по HTTPS. Сказать об этом здесь
        # честнее, чем отдать его же отказ: человек читает наш экран, а не
        # документацию Bot API.
        raise errors.ValidationError(
            f"Telegram delivers only over HTTPS, and the site address is {osnova}. "
            "Set OPENCRM_BASE_URL to your https address first.",
            code="telegram_needs_https",
        )

    adres = f"{osnova}/api/v1/telegram/webhook"
    try:
        zadat_vebkhuk(kluch, adres, sekret, opener)
    except TelegramOtkaz as beda:
        raise errors.ValidationError(
            f"Telegram refused the webhook: {beda}", code="telegram_webhook_refused"
        ) from None
    return {"connected": True, "url": adres}


def otklyuchit_vebkhuk(db: Session, opener=None) -> None:
    """Снять вебхук у телеграма. Молча терпит отказ.

    Терпит намеренно: отключение бота не должно упираться в доступность
    телеграма. Токен мы уже стёрли, доставлять он всё равно будет некуда — а
    висящий у телеграма адрес перестанет работать сам.
    """
    kluch = token(db)
    if not kluch:
        return
    try:
        _vyzov(kluch, "deleteWebhook", {"drop_pending_updates": True}, opener)
    except TelegramOtkaz as beda:
        logger.warning("не сняли вебхук у телеграма: %s", beda)


def zabrat_pozzhe(db: Session, chat_row_id: int, message_id: int, opener=None):
    """Забрать у телеграма файл, который сразу не тянули. Про видео.

    **Почему видео не забирается сразу.** Переписка с картинками съест диск за
    месяцы, а с видео — за недели; в этом проекте за диском уже однажды никто не
    следил, пока не стало поздно. Поэтому видео помечается, а тянется тогда,
    когда его действительно попросили посмотреть.

    Забранное остаётся у нас: второй раз то же видео качать неоткуда и незачем,
    а ссылка телеграма живёт около часа. Повторный вызов на уже забранном файле
    ничего не делает — он идемпотентен, и это важно: кнопку нажимают дважды.
    """
    stroka = telegram_repo.po_id(db, message_id)
    if stroka is None or stroka.chat_id != chat_row_id:
        raise errors.NotFoundError("Message not found", code="telegram_file_not_found")
    if stroka.file_path:
        return stroka
    if not stroka.file_id:
        raise errors.NotFoundError("Nothing to download", code="telegram_file_not_found")

    dialog = telegram_repo.get_chat(db, chat_row_id)
    try:
        soderzhimoe = skachat_fayl(token(db), stroka.file_id, opener)
    except TelegramOtkaz as beda:
        # Телеграм хранит файлы не вечно, и у старой переписки его может уже не
        # быть. Говорим об этом прямо: «не скачалось» человеку понятнее, чем
        # пустая страница.
        raise errors.ValidationError(
            f"Telegram did not give the file: {beda}", code="telegram_file_gone"
        ) from None

    stroka.file_path = _polozhit_fayl(dialog, stroka.file_name or "video.mp4", soderzhimoe)
    stroka.file_size = len(soderzhimoe)
    return stroka


def fayl_soobshcheniya(db: Session, chat_row_id: int, message_id: int):
    """Путь к вложению на диске — для отдачи наружу.

    Проверяем ДВА условия, а не одно: сообщение принадлежит названному диалогу,
    и путь не уводит за пределы каталога канала. Второе не паранойя: имя файла
    у входящего приходит от постороннего, и хотя мы его не используем (кладём
    под своим), полагаться на это в месте, отдающем файлы наружу, нельзя. Такой
    проверки не хватало ровно в этом классе мест по всей отрасли.
    """
    stroka = telegram_repo.po_id(db, message_id)
    if stroka is None or stroka.chat_id != chat_row_id or not stroka.file_path:
        raise errors.NotFoundError("File not found", code="telegram_file_not_found")

    koren = (get_settings().storage_dir / "telegram").resolve()
    put = (get_settings().storage_dir / stroka.file_path).resolve()
    if not put.is_relative_to(koren) or not put.exists():
        raise errors.NotFoundError("File not found", code="telegram_file_not_found")
    return put, stroka


def poslat_tekst_razmetkoy(token: str, chat_id: int, text: str, opener=None) -> dict:
    """Отправить текст С РАЗМЕТКОЙ и с запасным путём на случай отказа разбора.

    Отдельно от `poslat_tekst`, и разница принципиальная. Клиенту мы пишем БЕЗ
    разметки: текст сочиняет менеджер, в нём бывают `<` и `&`, и телеграм отбил
    бы сообщение целиком. Здесь текст сочиняем мы сами и всё чужое в нём
    экранируем — значит разметку можно и нужно: сводка со сворачиваемым блоком
    читается, а сплошная простыня нет.
    """
    return _s_razmetkoy(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            # Ссылок в сводке нет, а предпросмотр к ним превратил бы её в
            # простыню картинок.
            "disable_web_page_preview": True,
        },
        opener,
    )


# --- то, ради чего канал внутри CRM ------------------------------------------

def zavesti_zayavku(db: Session, chat_row_id: int, data: dict, author):
    """Завести заявку прямо из диалога.

    **Ради этого мессенджер и внутри CRM, а не в соседней вкладке.** Иначе
    менеджер читает переписку здесь, а заявку заводит отдельно, перенося
    сведения руками — то есть делает ровно ту работу, которую перенос и должен
    был убрать.

    Диалог обязан быть привязан к карточке: заявка без клиента бессмысленна,
    и подставлять «кого-нибудь» нельзя — это тот же оплаченный урок про чужую
    карточку. Не привязан — говорим об этом кодом, а не заводим клиента сами:
    решение «этот диалог — вот этот человек» принимает человек.

    Название по умолчанию берём из последнего входящего: девять раз из десяти
    заявка называется тем, с чего клиент начал. Переименовать её потом — одно
    движение, а вспомнить, о чём был разговор, по названию «Заявка из
    телеграма» — нельзя.
    """
    from core.services import deal_service

    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")
    if not dialog.client_id:
        raise errors.ValidationError(
            "Link this conversation to a client card first",
            code="telegram_chat_not_linked",
        )

    nazvanie = (data.get("title") or "").strip()
    if not nazvanie:
        posledneye = telegram_repo.lenta(db, chat_row_id, limit=20)
        vhodyashchie = [s for s in posledneye if s.direction == DIRECTION_IN and s.body]
        nazvanie = (vhodyashchie[0].body if vhodyashchie else "").strip()[:200]
    if not nazvanie:
        nazvanie = dialog.title or "Telegram"

    return deal_service.create_deal(
        db,
        {
            "title": nazvanie,
            "client_id": dialog.client_id,
            "description": (data.get("description") or "").strip(),
        },
        author,
    )


def zavesti_zadachu(db: Session, chat_row_id: int, data: dict, author):
    """Завести напоминание из диалога.

    В отличие от заявки, задача НЕ требует привязки к карточке: «перезвонить
    этому человеку» осмысленно и до того, как выяснили, кто он. Привязан
    диалог — клиент подставится; нет — задача всё равно заведётся, и это
    честнее, чем требовать сначала разобраться, а потом уже не забыть.
    """
    from core.services import task_service

    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")

    nazvanie = (data.get("title") or "").strip()
    if not nazvanie:
        nazvanie = f"Telegram: {dialog.title or dialog.username or dialog.chat_id}"[:200]

    telo = {"title": nazvanie, "due_at": data.get("due_at")}
    if dialog.client_id:
        telo["client_id"] = dialog.client_id
    return task_service.create(db, telo, author)


# --- карточка клиента прямо из переписки -------------------------------------


def _svedeniya_iz_dialoga(dialog) -> dict:
    """Что телеграм знает о собеседнике — уже в полях карточки клиента.

    Одно место на оба действия: и заведение карточки, и перенос в уже
    заведённую берут отсюда. Порознь они разъехались бы на первой же правке — и
    «завести» клало бы логин, а «обновить» про него уже не знало.
    """
    return {
        # Имя телеграма, а если человек его не задал — логин. Последним доводом
        # номер чата: карточка без имени не заводится вовсе, и отказ ручки в
        # ответ на «заведи карточку» был бы хуже некрасивого имени, которое
        # правится в одно движение.
        "name": (dialog.title or dialog.username or f"Telegram {dialog.chat_id}")[:200],
        # Только тот номер, которым клиент поделился КНОПКОЙ. Номера из текста
        # сообщений здесь нет и не будет: «позвоните маме на 067…» — это не его
        # телефон, а привязка по такому номеру и есть тот самый оплаченный урок.
        "phone": dialog.phone or "",
        # Логин — в поле мессенджера, со «собакой», как его пишут везде.
        # Карточку читает человек, и по этой строке он находит собеседника в
        # самом телеграме, не возвращаясь в переписку.
        "messenger": f"@{dialog.username}" if dialog.username else "",
        # Откуда пришёл. Метка ссылки (`t.me/бот?start=naklejka`) отвечает на
        # тот же вопрос и отвечает точнее, поэтому побеждает слово «телеграм».
        # Метки нет — остаётся сам канал: «неизвестно откуда» было бы неправдой,
        # откуда — известно.
        "source": dialog.source or "telegram",
    }


def zavesti_kartochku(db: Session, chat_row_id: int, author):
    """Завести карточку клиента по переписке и привязать к ней диалог — разом.

    **Одним действием, и в этом вся суть.** До этой ручки менеджер, у которого
    написал незнакомый человек, уходил в раздел клиентов, заводил карточку
    руками, возвращался и выбирал её в списке. Три перехода и потерянная мысль
    посреди разговора — то есть карточку заводили потом. «Потом» не наступает.

    **Вторую карточку тому же человеку эта ручка не заводит.** Диалог уже
    привязан — отказ (`telegram_chat_already_linked`), и это не придирка:
    размножение карточек одного клиента — беда, от которой страдает всякая CRM,
    а лечится она только тем, что второй карточке неоткуда взяться.

    Ещё одна развилка на том же месте: номер, которым клиент поделился, уже
    стоит в чьей-то карточке. Тогда заводить нечего — надо привязать к
    найденной. Молча так решать можно ровно по одной примете: точное совпадение
    нормализованного номера. Это единственная разрешённая в канале автопривязка
    (см. `privyazat_klienta`), и разрешена она потому, что номер человек назвал
    сам, кнопкой. Совпадение имён здесь по-прежнему не значит ничего.

    Возвращает пару «карточка, завели ли её»: привязка к найденной и заведение
    новой — разные вести, и сказать о них человеку надо по-разному.
    """
    from core.services import client_service

    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")
    if dialog.client_id:
        raise errors.ConflictError(
            "This conversation already has a client card. Open it or update it "
            "from Telegram — a second card for the same person only splits their "
            "history in two.",
            code="telegram_chat_already_linked",
        )

    uzhe = telegram_repo.find_client_by_phone(db, dialog.phone_norm)
    if uzhe is not None:
        dialog.client_id = uzhe.id
        return uzhe, False

    klient = client_service.create_client(db, _svedeniya_iz_dialoga(dialog), author)
    dialog.client_id = klient.id
    return klient, True


def obnovit_kartochku(db: Session, chat_row_id: int):
    """Перенести в привязанную карточку то, что телеграм узнал о человеке позже.

    **Правило одно, и оно короткое: заполняется только ПУСТОЕ поле карточки.**

    Обратное — «телеграм свежее, значит он и прав» — выглядит разумно ровно до
    первого случая. В карточке лежит то, что внесли руками: имя с отчеством,
    номер, продиктованный по телефону, логин, уточнённый у самого клиента. В
    телеграме лежит то, что человек выбрал себе сам и меняет когда вздумается:
    «🔥Ваня🔥», второй номер, снятый логин. Перенос поверх непустого означал бы,
    что смена ника у клиента переименовывает карточку в CRM, — молча и ровно в
    ту секунду, когда менеджер нажал «обновить», думая, что дополняет.

    Отсюда следствие, которое стоит назвать прямо: **имя карточки этой кнопкой
    не меняется никогда.** Имя обязательно и пустым не бывает — значит правило
    его не пропустит ни разу. Потери здесь нет: свежее имя из телеграма стоит в
    шапке того же разговора, в полутора сантиметрах от имени карточки, и решить,
    какое из двух правда, может только человек. Кнопка «затереть имя» — это
    правка карточки, и она уже есть там, где ей место.

    Переносится на деле: телефон, логин и метка источника — каждое в своё
    пустое поле. Переносить нечего — не отказ: карточка возвращается как есть, и
    второе нажатие ничего не портит.

    Возвращает пару «карточка, что именно перенесли»: список полей нужен тому,
    кто захочет сказать человеку не «готово», а «перенесли номер».
    """
    from core.services import client_service

    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")
    if not dialog.client_id:
        raise errors.ValidationError(
            "Link this conversation to a client card first",
            code="telegram_chat_not_linked",
        )

    klient = client_service.get_client(db, dialog.client_id)
    pravki = {
        pole: znachenie
        for pole, znachenie in _svedeniya_iz_dialoga(dialog).items()
        if znachenie and not (getattr(klient, pole, "") or "")
    }
    if not pravki:
        return klient, []
    # Через общую правку карточки, а не присваиванием полей здесь: там
    # пересчитывается `phone_norm` (без него звонок с этого номера пришёл бы
    # мимо карточки) и двигается `updated_at`. Второе место, где это надо
    # помнить, — первое, где об этом забудут.
    client_service.update_client(db, klient.id, pravki)
    return klient, sorted(pravki)


# --- аватар собеседника ------------------------------------------------------


#: Как часто спрашивать телеграм об аватаре одного человека, секунды.
#:
#: Сутки. Замечание владельца звучало как «обновлять в онлайн-режиме», и
#: буквально это означало бы вызов на каждого собеседника при каждом показе
#: списка — то есть десятки вызовов в минуту у конторы с полусотней диалогов.
#: Телеграм за такое ограничивает бота целиком, и тогда перестанут ходить не
#: аватары, а сообщения.
#:
#: Сутки выбраны как цена ошибки в обе стороны: сменивший фотографию клиент
#: будет узнаваем по вчерашней (беда невелика), а лишних вызовов — один на
#: человека в день.
SROK_AVATARA = 24 * 3600


def avatar_ustarel(dialog) -> bool:
    """Пора ли спрашивать телеграм об аватаре этого человека."""
    kogda = getattr(dialog, "avatar_checked_at", None)
    if kogda is None:
        return True
    proshlo = (now_utc().replace(tzinfo=None) - kogda).total_seconds()
    # Отрицательное `proshlo` — время на машине переставили назад. Тогда
    # считаем устаревшим: лишний вызов раз в жизни дешевле аватара, застрявшего
    # до тех пор, пока часы не догонят.
    return proshlo >= SROK_AVATARA or proshlo < 0


def obnovit_avatar(db: Session, dialog, kluch: str, opener=None) -> str:
    """Забрать фотографию профиля и вернуть путь к ней (или пустую строку).

    **Отметка ставится в любом случае, включая неудачу.** Аватара может не быть
    вовсе — человек его не поставил или закрыл настройками приватности, и это
    обычное состояние, а не отказ. Без отметки «спросили и не нашли» такой
    собеседник стоил бы нам вызова при каждом показе списка.

    Отказ телеграма наружу не поднимается по той же причине: аватар — украшение
    поверх переписки, и уронить из-за него показ диалогов было бы обменом
    важного на неважное.
    """
    dialog.avatar_checked_at = now_utc().replace(tzinfo=None)
    try:
        svedeniya = _vyzov(
            kluch,
            "getUserProfilePhotos",
            {"user_id": dialog.chat_id, "limit": 1},
            opener,
        )
        snimki = svedeniya.get("photos") or []
        if not snimki or not snimki[0]:
            dialog.avatar_path = ""
            return ""

        # Размеры приходят по возрастанию. Берём САМЫЙ МЕЛКИЙ, который не меньше
        # 160 точек, а если таких нет — просто самый крупный из мелких: аватар
        # показывается кружком в 36 точек, и тянуть ради него исходник в
        # несколько мегабайт незачем.
        razmery = snimki[0]
        podhodit = next((r for r in razmery if (r.get("width") or 0) >= 160), razmery[-1])
        soderzhimoe = skachat_fayl(kluch, podhodit["file_id"], opener, srok=TIMEOUT)
        # Имя постоянное, без случайной части: аватар у диалога один, и старые
        # копии иначе копились бы по одной в сутки на человека.
        dialog.avatar_path = _polozhit_fayl(dialog, "avatar.jpg", soderzhimoe, imya_kak_est=True)
        return dialog.avatar_path
    except Exception as beda:  # noqa: BLE001
        logger.warning("не забрали аватар из телеграма: %r", beda)
        return dialog.avatar_path or ""



def avatar_fayl(db: Session, chat_row_id: int):
    """Путь к забранному аватару. ТОЛЬКО чтение с диска.

    **Здесь нет ни одного обращения к телеграму, и это главное в этой функции.**
    Первая редакция освежала аватар прямо тут, «по требованию». Стоило это
    дорого: соединение к базе держится всё время разговора с телеграмом (до
    семидесяти секунд на скачивание), а первый показ списка после обновления
    просит до сотни аватаров разом — у всех строк отметка пуста. Пул в десять
    соединений занимался целиком, и в «QueuePool limit reached» упиралось всё
    остальное, включая проверку здоровья, на которой обновление откатывается.

    Забор переехал туда, где мы и так разговариваем с телеграмом, — в приём
    входящего сообщения (`prinyat`). Частоту там ограничивает сама переписка.

    Нет файла — 404, и это НЕ ошибка: у человека может не быть аватара вовсе.
    Экран рисует инициалы.
    """
    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")

    if not dialog.avatar_path:
        raise errors.NotFoundError("No avatar", code="telegram_no_avatar")

    put = get_settings().storage_dir / dialog.avatar_path
    if not put.exists():
        # Файл потеряли (переезд, чистка диска). Строку не правим: править её
        # здесь бесполезно — отказ откатит транзакцию вместе с правкой. Само
        # заживёт при следующем сообщении: отметка состарится, и аватар
        # заберётся заново.
        raise errors.NotFoundError("No avatar", code="telegram_no_avatar")
    return put
