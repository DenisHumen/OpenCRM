"""Живое состояние: шина событий и присутствие.

**Зачем это вообще нужно и почему в памяти процесса нельзя.** Сообщение от
клиента приходит вебхуком в ОДИН процесс приложения. Соединение менеджера,
который ждёт новых сообщений, держит ДРУГОЙ процесс. Второй о первом не знает
ничего. Пока процесс один, память работает и выглядит правильной — а на втором
процессе половина сотрудников перестаёт видеть половину событий, и никакой
ошибки при этом не возникает.

Это тот же класс беды, что несошедшаяся схема базы и неприменённый конфиг
nginx: **работает, но не то**, и молчит об этом. Поэтому правило записано
отдельной строкой: живое состояние либо в общем хранилище, либо не существует.

**Присутствие не хранится на диске** — оно производное от того, кто сейчас на
связи, и живёт секунды. Ключ с коротким сроком годности: перестал подтверждать
— исчез сам, без уборщика. Уборщик здесь был бы третьим местом, которое надо
чинить, и первым, которое сломается молча.

**Что происходит, когда Redis недоступен.** Живое перестаёт быть живым, и
только. Ни одна ручка не отказывает, ни одно сообщение не теряется: они лежат в
MySQL, и экран дочитывает их обычным запросом «что появилось после». Обратное
решение — «нет шины, нет мессенджера» — означало бы, что перезапуск Redis
роняет общение с клиентами.
"""

from __future__ import annotations

import json
import logging
import time

from core import redis_client

logger = logging.getLogger(__name__)

#: Канал шины. Один на весь мессенджер, а не по каналу на диалог.
#:
#: По диалогу было бы «эффективнее» ровно до первого экрана со списком: он
#: должен слышать про ВСЕ диалоги разом, иначе новый диалог в списке не
#: появится, пока не перезагрузишь страницу. Подписка на сотню каналов ради
#: этого — сотня подписок на каждого менеджера.
KANAL = f"{redis_client.PREFIX}tg:events"

#: Сколько живёт отметка присутствия без подтверждения.
#:
#: Пятнадцать секунд при подтверждении раз в пять. Запас втрое: одно
#: пропущенное подтверждение (сеть моргнула, вкладка притормозила) не должно
#: выгонять человека из чата на глазах у соседа. Меньше — присутствие мигает;
#: больше — ушедший висит в чате, и баннер «здесь ещё Марина» врёт.
PRISUTSTVIE_TTL = 15

#: Приставка ключей присутствия: `…tg:v:<диалог>:<сотрудник>`.
PRISUTSTVIE = f"{redis_client.PREFIX}tg:v:"


def obyavit(vid: str, **polya) -> bool:
    """Сказать всем процессам, что случилось. Отвечает, услышал ли кто-нибудь.

    Отказ шины НЕ поднимается наверх и не откатывает работу. Сообщение уже
    записано в базу, и уронить запрос из-за того, что не удалось разослать
    уведомление, значило бы поменять надёжное на быстрое.
    """
    client = redis_client.get_client()
    if client is None:
        return False
    try:
        client.publish(KANAL, json.dumps({"type": vid, **polya}, default=str))
        return True
    except Exception as beda:  # noqa: BLE001 — шина не обязана быть живой
        logger.warning("шина событий недоступна: %r", beda)
        return False


def podpisatsya():
    """Подписка на шину. `None`, если Redis недоступен.

    Возвращается сырой объект подписки: разбирать сообщения будет тот, кто
    держит соединение с браузером, и делать это здесь значило бы решать за него,
    как часто он готов их забирать.
    """
    client = redis_client.get_client()
    if client is None:
        return None
    try:
        podpiska = client.pubsub(ignore_subscribe_messages=True)
        podpiska.subscribe(KANAL)
        return podpiska
    except Exception as beda:  # noqa: BLE001
        logger.warning("не подписались на шину событий: %r", beda)
        return None


def otmetit_prisutstvie(chat_row_id: int, user_id: int, imya: str) -> None:
    """«Я в этом чате». Ключ со сроком годности — уборки не требует."""
    client = redis_client.get_client()
    if client is None:
        return
    try:
        client.setex(
            f"{PRISUTSTVIE}{chat_row_id}:{user_id}",
            PRISUTSTVIE_TTL,
            json.dumps({"user_id": user_id, "name": imya, "at": int(time.time())}),
        )
    except Exception as beda:  # noqa: BLE001
        logger.warning("не отметили присутствие: %r", beda)


def ubrat_prisutstvie(chat_row_id: int, user_id: int) -> None:
    """Ушёл из чата — убрать сразу, не дожидаясь срока.

    Срок годности снял бы отметку и сам, но через пятнадцать секунд. Всё это
    время сосед видел бы баннер «здесь ещё Пётр» про человека, который уже
    закрыл вкладку, — то есть предупреждение о конфликте, которого нет.
    """
    client = redis_client.get_client()
    if client is None:
        return
    try:
        client.delete(f"{PRISUTSTVIE}{chat_row_id}:{user_id}")
    except Exception as beda:  # noqa: BLE001
        logger.warning("не убрали присутствие: %r", beda)


def kto_v_chate(chat_row_id: int) -> list[dict]:
    """Кто сейчас смотрит этот диалог.

    Перебором ключей по образцу (`SCAN`), а не множеством с ручной уборкой.
    Множество пришлось бы чистить самим: выпавший из сети браузер ничего не
    сообщает, и его имя висело бы в чате до перезапуска. Ключи со сроком
    годности снимают этот вопрос целиком.

    `SCAN`, а не `KEYS`: последний блокирует Redis на всё время обхода, а он
    здесь общий с ограничителем входа — то есть `KEYS` на большой базе означал
    бы задержку входа в систему.
    """
    client = redis_client.get_client()
    if client is None:
        return []
    try:
        obrazets = f"{PRISUTSTVIE}{chat_row_id}:*"
        nayd = []
        for klyuch in client.scan_iter(match=obrazets, count=100):
            znachenie = client.get(klyuch)
            if not znachenie:
                continue
            try:
                nayd.append(json.loads(znachenie))
            except ValueError:
                continue
        return sorted(nayd, key=lambda z: z.get("name", ""))
    except Exception as beda:  # noqa: BLE001
        logger.warning("не собрали присутствие: %r", beda)
        return []
