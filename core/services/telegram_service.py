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

import secrets

from sqlalchemy.orm import Session

from core import exceptions as errors
from database.repositories import settings as settings_repo

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
