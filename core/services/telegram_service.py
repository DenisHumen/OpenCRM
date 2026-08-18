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
        "bot_username": vse.get(SETTING_USERNAME, ""),
        "webhook_secret_set": bool(vse.get(SETTING_WEBHOOK_SECRET, "")),
    }


def token(db: Session) -> str:
    """Токен для обращения к телеграму. Внутреннее употребление."""
    return _vse(db).get(SETTING_TOKEN, "")


def webhook_secret(db: Session) -> str:
    return _vse(db).get(SETTING_WEBHOOK_SECRET, "")


def digest_chat(db: Session) -> str:
    return _vse(db).get(SETTING_DIGEST_CHAT, "")


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
        chat = str(data.get("digest_chat") or "").strip()
        # Идентификатор чата бывает отрицательным (группы) — это законно.
        if chat and not chat.lstrip("-").isdigit():
            raise errors.ValidationError(
                "Chat id must be a number, like 123456789 or -1001234567890",
                code="bad_chat_id",
            )
        izmeneniya[SETTING_DIGEST_CHAT] = chat

    if "bot_username" in data:
        izmeneniya[SETTING_USERNAME] = str(data.get("bot_username") or "").strip().lstrip("@")[:64]

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
            "allowed_updates": ["message"],
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
        dialog = telegram_repo.create_chat(
            db,
            chat_id=int(chat_id),
            username=str(otpravitel.get("username") or "")[:64],
            title=_imya_iz(otpravitel)[:200],
        )
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
    kontakt = soobshchenie.get("contact") or {}
    if kontakt.get("phone_number") and not dialog.client_id:
        nomer = str(kontakt["phone_number"])
        dialog.phone = nomer[:64]
        dialog.phone_norm = normalize_phone(nomer, _kod_strany(db))[:32]
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

    put_fayla = ""
    if file_id and vid != KIND_VIDEO and 0 < razmer <= MAX_AUTO_DOWNLOAD:
        # Видео не забираем сразу намеренно: см. `MAX_AUTO_DOWNLOAD`.
        try:
            soderzhimoe = skachat_fayl(token(db), file_id, opener)
            put_fayla = _polozhit_fayl(dialog, imya_fayla, soderzhimoe)
        except Exception as beda:  # noqa: BLE001
            # Не сумели забрать файл — сообщение всё равно записываем. Потерять
            # текст из-за неудавшейся загрузки картинки значило бы потерять
            # больше, чем сохранить.
            logger.warning("не забрали файл из телеграма: %r", beda)

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
    # В шину — ПОСЛЕ записи, чтобы услышавший нашёл сообщение в базе. Объявить
    # раньше значило бы разослать «пришло новое», по которому экран сходил бы в
    # базу и ничего не нашёл: транзакция ещё не зафиксирована.
    #
    # Отказ шины сюда не поднимается: сообщение уже записано, и ронять приём
    # из-за недоступного Redis значило бы менять надёжное на быстрое. Телеграм
    # на такой отказ ответил бы повтором.
    realtime.obyavit(
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

    # Аватар — ПОСЛЕ объявления в шину, и это порядок, а не случайность: экраны
    # получают «пришло новое» немедленно, а разговор об аватаре идёт следом и
    # ничего не задерживает.
    #
    # Здесь, а не в ручке показа, потому что тут частоту ограничивает сама
    # переписка: одно сообщение — не более одного захода, и не чаще раза в
    # сутки на диалог. Ручка показа при первом же открытии списка попросила бы
    # сотню аватаров разом и заняла бы весь пул соединений.
    if avatar_ustarel(dialog):
        kluch = token(db)
        if kluch:
            obnovit_avatar(db, dialog, kluch, opener)
            # Именной эмодзи — той же отметкой и только у премиума: у остальных
            # его не бывает вовсе, и два вызова ушли бы впустую на каждого.
            if dialog.is_premium:
                obnovit_status_emodzi(db, dialog, kluch, opener)

    return {"status": "accepted", "chat_id": dialog.id, "message_id": stroka.id}


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
    realtime.obyavit(
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

    **Запасной путь обязателен.** Телеграм отбивает сообщение по разбору
    разметки кодом 400. Получив его, шлём то же самое плоским текстом. Свойство,
    ради которого это написано: ошибка в оформлении не имеет права заглушить
    сводку — а молчание сводки читается как «всё сломалось».
    """
    try:
        return _vyzov(
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
    except TelegramOtkaz as beda:
        logger.warning("сводка не разобралась как HTML, шлём плоским текстом: %s", beda)
        import re as _re

        ploskiy = _re.sub(r"<[^>]+>", "", text)
        return _vyzov(token, "sendMessage", {"chat_id": chat_id, "text": ploskiy}, opener)


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
