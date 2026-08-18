"""Переписка с клиентами через бота фирмы.

Настройки подключения, приём от телеграма, список диалогов, лента, ответ,
вложения, привязка к карточке, присутствие и поток живых событий.

Все рабочие ручки закрыты блоком ``telegram`` — выключили блок, и раздела нет
ни в меню, ни в API. Это правило проекта, а не удобство: спрятать пункт меню
недостаточно, адрес остаётся рабочим и лежит в закладках.

Приём вынесен в отдельный роутер БЕЗ зависимости от блока — по той же причине,
по какой так сделан вебхук АТС: телеграм не умеет узнать, что канал выключили,
и продолжит доставлять; отвечать ему пятисоткой значило бы копить у него
очередь повторов.
"""

import asyncio
import hmac
import json
import logging
import time

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from core import exceptions as errors
from core import realtime
from core.ratelimit import SlidingWindowLimiter
from core.services import codes, svodka_service, telegram_service
from database.models import User
from database.repositories import clients as clients_repo
from database.repositories import telegram as telegram_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, client_ip, get_db, require_module, require_perm

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/telegram",
    tags=["telegram"],
    dependencies=[Depends(require_module("telegram"))],
)


class NastroykiVhod(BaseModel):
    """Что можно задать с экрана.

    Все поля необязательные, и это не небрежность: экран сохраняет по разделам,
    а пустой токен означает «не меняй». Отправить обратно значение, которого он
    не получал, экран не может — токен ему не отдают.
    """

    token: str | None = None
    digest_chat: str | None = None
    bot_username: str | None = None
    #: Сколько месяцев хранить переписку. Ноль — вечно, и это умолчание.
    #:
    #: Числом, а не строкой, в отличие от соседей: экран выбирает его из списка,
    #: и приведение к строке ради единообразия завело бы ещё одно место, где «0»
    #: и «» надо считать одним и тем же.
    retention_months: int | None = None


@router.get("/settings")
def nastroyki(
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    """Состояние подключения. Токен наружу не отдаётся — только его хвост."""
    return telegram_service.nastroyki(db)


@router.put("/settings")
def zadat_nastroyki(
    data: NastroykiVhod,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    # `exclude_unset` важен: без него незаполненные поля приехали бы как `None`
    # и стёрли то, чего человек не трогал. Ровно та же беда, что у частичной
    # правки карточки.
    return telegram_service.zadat(db, data.model_dump(exclude_unset=True))


@router.delete("/settings")
def otklyuchit(
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    """Отключить бота. Переписка остаётся — отключение про связь, а не про данные.

    Вебхук снимаем ДО стирания токена: после стирания обращаться к телеграму
    уже нечем, и адрес висел бы у него до первой ошибки доставки.
    """
    telegram_service.otklyuchit_vebkhuk(db)
    return telegram_service.otklyuchit(db)


@router.post("/connect")
def podklyuchit(
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    """Сказать телеграму, куда доставлять сообщения.

    Отдельным действием, а не при сохранении токена: адрес приёма зависит от
    того, как сайт виден снаружи, и меняется при переезде или смене домена.
    Прицепи это к токену — и единственным способом переподключиться было бы
    перевводить токен.
    """
    return telegram_service.podklyuchit(db)


def _dialog_naruzhu(row) -> dict:
    """Диалог для экрана."""
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "title": row.title,
        "username": row.username,
        "phone": row.phone,
        "client_id": row.client_id,
        "source": row.source,
        "last_message_at": row.last_message_at,
        # Есть ли что показывать. Ровно так: файл забран или нет.
        #
        # В первой редакции сюда входило ещё и «не спрашивали ни разу», чтобы
        # экран сходил и тем самым запустил забор. Признак запирал сам себя:
        # спросили, не нашли — признак ложный, экран больше не зовёт, а звать
        # было единственным способом обновить. Собеседник, поставивший
        # фотографию назавтра, оставался без лица навсегда.
        #
        # Теперь забор живёт в приёме сообщения и от показа не зависит вовсе.
        "has_avatar": bool(row.avatar_path),
        "is_premium": row.is_premium,
        "has_emoji": bool(row.emoji_status_path),
    }


def _predprosmotr(stroka) -> dict:
    """Последняя фраза для строки списка. Пусто — диалог ещё молчит.

    Тело режется до 80 знаков ЗДЕСЬ, а не на экране: наружу незачем отдавать
    простыню, из которой строка списка покажет одну строчку.
    """
    if stroka is None:
        return {"last_preview": "", "last_direction": "", "last_kind": ""}
    return {
        "last_preview": (stroka.body or "")[:80],
        "last_direction": stroka.direction,
        "last_kind": stroka.kind,
    }


def _soobshchenie_naruzhu(row) -> dict:
    """Сообщение для экрана.

    `send_error` отдаётся наружу намеренно: менеджер обязан увидеть, ПОЧЕМУ его
    ответ не ушёл. «Бот заблокирован пользователем» и «неверный токен» чинятся
    по-разному, и скрыть разницу значило бы заставить гадать.
    """
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "direction": row.direction,
        "kind": row.kind,
        "body": row.body,
        "file_name": row.file_name,
        "file_size": row.file_size,
        "has_file": bool(row.file_path),
        # Есть что забрать, но ещё не забрали — про видео.
        "can_fetch": bool(row.file_id) and not row.file_path,
        "author_id": row.author_id,
        "reply_to_id": row.reply_to_id,
        "send_state": row.send_state,
        "send_error": row.send_error,
        "happened_at": row.happened_at,
    }


# --- приём от телеграма ------------------------------------------------------
#
# Отдельным роутером и БЕЗ зависимости от блока — по той же причине, что у
# вебхука АТС: телеграм не умеет узнать, что канал выключили. Отвечай мы ему
# отказом, он копил бы у себя очередь повторов и вывалил её разом при включении.
#
# **Выключенный блок приём НЕ останавливает: сообщение записывается.** Это
# решение, а не недосмотр. Выключение блока — про нас: меню, экран, ручки,
# настройки. Бот при этом остаётся подключённым, и клиент продолжает писать, не
# зная о наших переключателях. Уронить его слова значило бы: человек написал,
# телеграм показал «доставлено», ответа нет и не будет, а сообщения нет нигде.
# Хуже этого здесь ничего нет, и происходит оно молча.
#
# Записанное при этом никому не показывается — раздела не существует. Включат
# обратно — переписка на месте, ровно как обещает правило блоков «данные не
# стираются».
#
# Перестать ПРИНИМАТЬ — отдельное и явное действие: `DELETE /telegram/settings`
# снимает вебхук у телеграма. Это «отключить канал», а не «убрать раздел».
webhook_router = APIRouter(prefix="/telegram", tags=["telegram"])

#: Поток приёма. Обычная переписка — единицы сообщений в секунду; всё, что
#: выше, либо шторм от сломавшегося телеграма, либо чужой перебор, и то и
#: другое лучше притормозить, чем разбирать по логам.
webhook_limiter = SlidingWindowLimiter(600, 60, name="tgwebhook")

#: Заголовок, которым телеграм подтверждает, что это он.
SECRET_HEADER = "x-telegram-bot-api-secret-token"


@webhook_router.post("/webhook", status_code=200)
async def vebkhuk(request: Request, db: Session = Depends(get_db)) -> dict:
    """Обновление от телеграма.

    **Отвечаем 200 почти всегда, и это не небрежность.** Телеграм считает любой
    иной ответ недоставкой и повторяет — с нарастающей задержкой, часами. То
    есть наша ошибка в разборе одного сообщения превратилась бы в поток
    повторов этого же сообщения. Отказом отвечаем ровно на одно: неверный
    секрет. Там повтор как раз уместен — это либо чужой, либо мы сами сменили
    секрет и не сказали телеграму.
    """
    sekret = telegram_service.webhook_secret(db)
    if not sekret:
        # Канал не настроен. Не 200: пусть телеграм повторит — вдруг настройка
        # прямо сейчас и появится.
        raise errors.ValidationError(
            "Telegram bot is not configured", code="telegram_not_configured"
        )

    # Перебор секрета притормаживаем ДО сравнения: иначе ограничитель считает
    # только тех, кто уже промахнулся, а подбирающий шлёт запросы свободно.
    adres = client_ip(request)
    try:
        zablokirovan = webhook_limiter.is_blocked(adres)
    except errors.LimiterUnavailableError:
        # Redis лежит. Ограничитель бережёт от ПОДБОРА секрета, а не от потока
        # сообщений, и закрыть из-за него приём значило бы отбивать 503 всем
        # клиентам сразу: телеграм копит повторы часами и, если не дождётся,
        # теряет их. Подбор при этом остаётся невозможен — секрет сверяется
        # ниже постоянным временем, и угадать его с одной попытки нельзя.
        #
        # То есть выбор был между «перебор станет чуть дешевле, пока лежит
        # Redis» и «переписка с клиентами встала целиком». Второе дороже.
        logger.warning("ограничитель приёма недоступен — пропускаем без счёта")
        zablokirovan = False
    if zablokirovan:
        raise errors.RateLimitedError(
            "Too many webhook attempts", code="webhook_flooded"
        )

    prislannyy = request.headers.get(SECRET_HEADER, "")
    # Сравнение постоянного времени: обычное `==` отвечает тем быстрее, чем
    # раньше расходятся строки, и по этому времени секрет подбирается побайтно.
    if not hmac.compare_digest(prislannyy, sekret):
        webhook_limiter.record_failure(adres)
        raise errors.AuthError("Bad webhook secret", code="bad_webhook_secret")

    try:
        obnovlenie = await request.json()
    except Exception:  # noqa: BLE001 — телеграм прислал не JSON
        return {"status": "ignored", "reason": "bad_json"}

    try:
        # В отдельном потоке, а не прямо здесь. Ручка объявлена `async def`, а
        # внутри разбора живёт синхронный `urllib` с таймаутами до минуты:
        # выполненный в корутине, он останавливает ЦИКЛ СОБЫТИЙ, то есть все
        # запросы этого процесса разом — включая `/healthz`, по которому
        # обновление решает, откатывать ли живой релиз.
        return await run_in_threadpool(telegram_service.prinyat, db, obnovlenie)
    except errors.DomainError:
        # Наши доменные отказы разбирает общий обработчик: у них свой код и своё
        # объяснение, и подменять их безликим «error» незачем.
        raise
    except (OperationalError, DBAPIError) as beda:
        # Беда базы — ПРЕХОДЯЩАЯ: замок не дождался, соединение оборвалось,
        # сервер перезапускают. Отвечать на это 200 значит сказать телеграму
        # «принято» о сообщении, которого мы не записали: повтора не будет, и
        # слова клиента исчезнут молча. Пусть повторит — идемпотентность у нас
        # своя, по `external_id`.
        logger.exception("база не дала записать обновление телеграма: %r", beda)
        raise errors.PrehodyashchayaBedaError(
            "Could not store the update, please retry", code="telegram_store_failed"
        ) from None
    except Exception as beda:  # noqa: BLE001
        # А вот наша ошибка РАЗБОРА повтором не чинится: то же сообщение
        # приедет и разберётся так же. Отвечаем 200, чтобы не копить очередь.
        logger.exception("не разобрали обновление телеграма: %r", beda)
        return {"status": "error"}


# --- переписка ---------------------------------------------------------------


@router.get("/invite")
def priglashenie(
    label: str = Query("site", max_length=64, pattern=r"^[A-Za-z0-9_-]*$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Ссылка и QR-код, которыми клиента приводят к боту.

    **Без этого канал не работает вовсе, и это не украшение.** Телеграм не
    позволяет боту написать первым тому, кто его не запускал: клиент обязан
    начать разговор сам. Значит нужен способ его привести — ссылка на сайте,
    QR на квитанции, кнопка «написать нам».

    Метка (`?start=метка`) едет в диалог и отвечает на вопрос «откуда пришёл»:
    с сайта, с наклейки, из письма. Разрешены только буквы, цифры, дефис и
    подчёркивание — таково требование самого телеграма, и отдать сюда что-то
    другое значило бы выдать ссылку, которая молча не работает.
    """
    imya = telegram_service.nastroyki(db)["bot_username"]
    if not imya:
        raise errors.ValidationError(
            "Bot username is not set — fill it in the Telegram settings",
            code="telegram_username_missing",
        )
    ssylka = f"https://t.me/{imya}" + (f"?start={label}" if label else "")
    return {"url": ssylka, "qr_svg": codes.qr_svg(ssylka)}


@router.post("/digest/send")
def poslat_svodku(
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "manage")),
) -> dict:
    """Отправить сводку прямо сейчас.

    Нужна не вместо расписания, а рядом с ним, и ровно для одного: посмотреть,
    как сводка выглядит, не дожидаясь утра. Без этого единственный способ
    проверить оформление — подождать сутки, а оформление правят итерациями.

    Отвечает тем же, чем и расписание: «ушла», «не настроено», «не ушла и
    почему». Ненастроенный канал — не отказ ручки: человек мог открыть экран
    раньше, чем ввёл токен.
    """
    return svodka_service.otpravit(db)


@router.get("/chats")
def dialogi(
    q: str = Query("", max_length=MAX_SEARCH),
    source: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Список диалогов, свежие сверху, с личным счётчиком непрочитанного."""
    stroki, vsego = telegram_repo.spisok_dialogov(
        db, q=q.strip(), source=source.strip(), page=page, per_page=per_page
    )
    # Непрочитанное — своё у каждого сотрудника: граница «дочитал до сюда»
    # личная. Считается на всю страницу разом, а не по диалогу: список открыт
    # весь день и перезапрашивается живьём.
    nomera = [row.id for row in stroki]
    granitsy = realtime.granitsy_prochitannogo(nomera, user.id)
    neprochitano = telegram_repo.neprochitannye(db, nomera, granitsy)
    poslednie = telegram_repo.poslednie_soobshcheniya(db, [row.id for row in stroki])
    return {
        "items": [
            {
                **_dialog_naruzhu(row),
                "unread": neprochitano.get(row.id, 0),
                **_predprosmotr(poslednie.get(row.id)),
            }
            for row in stroki
        ],
        "total": vsego,
        "page": page,
        "per_page": per_page,
    }


@router.get("/chats/{chat_row_id}/messages")
def soobshcheniya(
    chat_row_id: int,
    before: int | None = Query(None, ge=1),
    after: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Лента переписки.

    Два способа спросить, и они про разное. `before` — листание вглубь, свежие
    сверху. `after` — дочитать пропущенное после обрыва связи: браузер называет
    последнее, что у него есть, и получает всё, что появилось потом. Второе
    существует именно потому, что обрыв неизбежен, а начинать с чистого листа в
    переписке с клиентом нельзя.
    """
    dialog = telegram_repo.get_chat(db, chat_row_id)
    if dialog is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")

    if after is not None:
        stroki = telegram_repo.novee(db, chat_row_id, after)
    else:
        stroki = list(reversed(telegram_repo.lenta(db, chat_row_id, do_id=before)))

    # Прочитанное отмечается ЧТЕНИЕМ ленты, а не отдельной кнопкой: человек
    # открыл диалог и увидел сообщения — значит он их прочитал. Отдельная
    # кнопка «прочитано» существует ровно затем, чтобы её забывали нажимать.
    #
    # Только при листании вглубь и первом показе: дочитывание после обрыва
    # (`after`) границу не двигает — оно приносит то, что человек мог и не
    # видеть, если вкладка была свёрнута.
    if after is None and stroki:
        realtime.otmetit_prochitano(chat_row_id, user.id, max(s.id for s in stroki))
    return {"items": [_soobshchenie_naruzhu(row) for row in stroki]}


class OtvetVhod(BaseModel):
    #: На какое сообщение отвечаем. Своё, не телеграмово: наружу телеграмовы
    #: номера не отдаются вовсе.
    reply_to_id: int | None = None
    text: str = ""


@router.post("/chats/{chat_row_id}/messages", status_code=201)
def otvetit(
    chat_row_id: int,
    data: OtvetVhod,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "create")),
) -> dict:
    """Ответить клиенту текстом."""
    stroka = telegram_service.otpravit(
        db, chat_row_id, tekst=data.text, author=user, otvet_na=data.reply_to_id
    )
    return _soobshchenie_naruzhu(stroka)


@router.post("/chats/{chat_row_id}/files", status_code=201)
async def otpravit_fayl(
    chat_row_id: int,
    file: UploadFile = File(...),
    caption: str = Form(""),
    # Форма, а не JSON: файл и его спутники едут одним multipart. Пустая строка
    # означает «без привязки» — так проще на стороне браузера, где `null` в
    # форму не положить.
    reply_to_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "create")),
) -> dict:
    """Отправить клиенту файл: картинку, видео или документ."""
    soderzhimoe = await file.read()
    # Отправка в телеграм — синхронный `urllib` с таймаутом в минуту на
    # пятидесяти мегабайтах. В корутине он остановил бы цикл событий вместе со
    # всеми запросами процесса, поэтому уходит в поток.
    if not soderzhimoe:
        raise errors.ValidationError("File is empty", code="file_empty")
    if len(soderzhimoe) > get_settings().max_upload_bytes:
        raise errors.ValidationError("File is too large", code="file_too_large")
    stroka = await run_in_threadpool(
        lambda: telegram_service.otpravit(
            db,
            chat_row_id,
            tekst=caption,
            author=user,
            fayl=(file.filename or "file", soderzhimoe),
            otvet_na=int(reply_to_id) if reply_to_id.strip().isdigit() else None,
        )
    )
    return _soobshchenie_naruzhu(stroka)


@router.get("/chats/{chat_row_id}/messages/{message_id}/file")
def vlozhenie(
    chat_row_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "view")),
):
    """Отдать вложение переписки.

    Через приложение, а не отдачей каталога прямо из nginx, и это решение.
    Файл из переписки с клиентом — не публичная картинка витрины: его вправе
    видеть тот, у кого есть право на раздел, и никто больше. Отдай мы каталог
    статикой, ссылка работала бы у всякого, кто её узнал, а узнаётся она из
    истории браузера, пересланного сообщения или чужого экрана.
    """
    put, stroka = telegram_service.fayl_soobshcheniya(db, chat_row_id, message_id)
    return FileResponse(
        put,
        # Имя для сохранения — то, под которым файл прислали. На диске он лежит
        # под своим (имя от постороннего доверия не заслуживает), но человеку
        # показать надо привычное.
        filename=stroka.file_name or put.name,
        # Фотографию — показать, остальное — сохранить. По этой же ссылке
        # переписка рисует картинку, и `attachment` на ней означал бы, что
        # щелчок по фото начинает скачивание вместо просмотра.
        #
        # Показывать так всё подряд нельзя: присланный посторонним HTML,
        # открытый с нашего домена, — это чужой скрипт в нашем происхождении.
        # Отсюда белый список из одного вида, а не «инлайн для всего, что
        # похоже на картинку».
        content_disposition_type=(
            "inline" if stroka.kind == telegram_service.KIND_PHOTO else "attachment"
        ),
    )


@router.get("/chats/{chat_row_id}/avatar")
def avatar(
    chat_row_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "view")),
):
    """Отдать забранную фотографию профиля. Только с диска.

    **К телеграму эта ручка не обращается.** Первая редакция освежала аватар
    здесь же, «по требованию», и это оказалось дорого сразу с двух сторон: пул
    в десять соединений занимался целиком, пока сотня одновременных запросов
    ждала api.telegram.org, а сам телеграм на сотню вызовов подряд отвечает 429
    и придерживает бота — то есть перестают ходить не аватары, а сообщения.

    Забор живёт в приёме входящего (`telegram_service.prinyat`): там частоту
    ограничивает сама переписка — не чаще раза в сутки на диалог.

    Нет аватара — 404, и это НЕ ошибка: человек мог его не поставить или закрыть
    настройками приватности. Экран рисует заглушку с инициалами, как в карточке
    клиента.
    """
    put = telegram_service.avatar_fayl(db, chat_row_id)
    return FileResponse(
        put,
        # Вид содержимого выводится из имени файла, а не назначается жёстко:
        # телеграм отдаёт аватары в jpeg, но обещать это за него незачем —
        # `nosniff` стоит на всём, и разойдись имя с обещанием, браузер
        # откажется показывать картинку вовсе.
        content_disposition_type="inline",
        # Час в кэше браузера: список диалогов перерисовывается на каждое
        # событие, и без этого один аватар ехал бы по сети десятки раз за смену.
        # `private` — потому что переписка с клиентом не должна оседать в
        # промежуточных кэшах по дороге.
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/chats/{chat_row_id}/emoji")
def emodzi(
    chat_row_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "view")),
):
    """Картинка именного эмодзи собеседника.

    Статичная миниатюра стикера: сам эмодзи — Lottie, и браузер его не рисует.
    Забирается при приёме сообщения не чаще раза в сутки и только у премиума.
    """
    put = telegram_service.emodzi_fayl(db, chat_row_id)
    return FileResponse(
        put,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/chats/{chat_row_id}/messages/{message_id}/fetch")
def zabrat_vlozhenie(
    chat_row_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Забрать видео, которое сразу не тянули.

    Право на ПРОСМОТР, а не на запись: нажатие «посмотреть» — это чтение
    переписки, а не изменение её. Данных оно не меняет и клиенту ничего не
    отправляет; всё, что происходит, — файл переезжает к нам с серверов
    телеграма.
    """
    stroka = telegram_service.zabrat_pozzhe(db, chat_row_id, message_id)
    return _soobshchenie_naruzhu(stroka)


class IzDialogaVhod(BaseModel):
    """Что можно уточнить, заводя заявку или задачу из переписки.

    Всё необязательное: смысл кнопки в том, чтобы нажать её и вернуться к
    разговору. Заставь она заполнять форму — ею перестанут пользоваться и
    вернутся к переносу руками, то есть ровно к тому, от чего уходили.
    """

    title: str = ""
    description: str = ""
    due_at: str | None = None


@router.post("/chats/{chat_row_id}/deal", status_code=201)
def zayavka_iz_dialoga(
    chat_row_id: int,
    data: IzDialogaVhod,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("deals", "create")),
) -> dict:
    """Завести заявку по этой переписке.

    Право спрашивается на ЗАЯВКИ, а не на телеграм, и это не описка: заводится
    заявка, и решать, кому это можно, должен раздел заявок. Иначе право на
    переписку тихо давало бы право заводить работу.
    """
    zayavka = telegram_service.zavesti_zayavku(db, chat_row_id, data.model_dump(), user)
    return {"id": zayavka.id, "title": zayavka.title, "client_id": zayavka.client_id}


@router.post("/chats/{chat_row_id}/task", status_code=201)
def zadacha_iz_dialoga(
    chat_row_id: int,
    data: IzDialogaVhod,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("tasks", "create")),
) -> dict:
    """Завести напоминание по этой переписке. Привязка к карточке не обязательна."""
    zadacha = telegram_service.zavesti_zadachu(db, chat_row_id, data.model_dump(), user)
    return {"id": zadacha.id, "title": zadacha.title}


@router.post("/chats/{chat_row_id}/client", status_code=201)
def kartochka_iz_dialoga(
    chat_row_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("clients", "create")),
) -> dict:
    """Завести карточку клиента по этой переписке и сразу её привязать.

    Право спрашивается на КЛИЕНТОВ, ровно как у соседей: у заявки из диалога —
    `deals.create`, у напоминания — `tasks.create`. Правило общее: спрашиваем
    право на то, что заводится, а не на телеграм. Иначе право читать переписку
    тихо давало бы право заводить карточки.

    Тела у запроса нет намеренно. Всё, что нужно, уже знает диалог, а форма
    посреди разговора означала бы, что кнопкой перестанут пользоваться и
    вернутся к переносу руками — то есть ровно к тому, от чего уходили.

    Ответ несёт `created`: карточку могли не завести, а привязать к найденной по
    точному совпадению номера (см. службу). Код 201 при этом честен в обоих
    случаях — у диалога появилась карточка, которой у него не было.
    """
    klient, zavedena = telegram_service.zavesti_kartochku(db, chat_row_id, user)
    return {**schemas.client_out(klient), "created": zavedena}


@router.post("/chats/{chat_row_id}/client/refresh")
def obnovit_kartochku_iz_dialoga(
    chat_row_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("clients", "edit")),
) -> dict:
    """Перенести в карточку то, что телеграм узнал о человеке позже.

    Право на ПРАВКУ клиентов, а не на заведение: правится существующая карточка.
    Заполняется только пустое — что и почему, расписано в службе.

    Ответ несёт `updated` — список перенесённых полей. Пустой список законен и
    отказом не является: нажали, а переносить оказалось нечего.
    """
    klient, perenesli = telegram_service.obnovit_kartochku(db, chat_row_id)
    return {**schemas.client_out(klient), "updated": perenesli}


class PrivyazkaVhod(BaseModel):
    client_id: int | None = None


@router.patch("/chats/{chat_row_id}")
def privyazat(
    chat_row_id: int,
    data: PrivyazkaVhod,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "create")),
) -> dict:
    """Сказать, чей это диалог. Только руками — молча система не привязывает."""
    if data.client_id is not None and clients_repo.get(db, data.client_id) is None:
        raise errors.NotFoundError("Client not found", code="client_not_found")
    dialog = telegram_service.privyazat_klienta(db, chat_row_id, data.client_id)
    return _dialog_naruzhu(dialog)


# --- живое состояние ---------------------------------------------------------


class PrisutstvieVhod(BaseModel):
    """Подтверждение «я в этом чате». Пустое тело — человек ушёл."""

    present: bool = True


@router.post("/chats/{chat_row_id}/presence")
def prisutstvie(
    chat_row_id: int,
    data: PrisutstvieVhod,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Отметиться в чате и узнать, кто здесь ещё.

    **Отметка со сроком годности, а не запись в базу.** Присутствие живёт
    секунды и по правилу проекта не хранится: оно производное от того, кто
    сейчас на связи. Ключ в Redis истекает сам — уборщик здесь был бы третьим
    местом, которое надо чинить, и первым, которое сломается молча.

    Ответ содержит ВСЕХ, кто в чате, включая спросившего: экрану проще
    отфильтровать себя, чем догадываться, что его в списке нет намеренно.
    """
    if telegram_repo.get_chat(db, chat_row_id) is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")

    if data.present:
        realtime.otmetit_prisutstvie(chat_row_id, user.id, user.name or user.email)
    else:
        realtime.ubrat_prisutstvie(chat_row_id, user.id)

    kto = realtime.kto_v_chate(chat_row_id)
    # Объявляем смену состава — иначе сосед узнает о новом человеке только на
    # своём следующем подтверждении, то есть через несколько секунд. Ровно тот
    # конфликт, ради которого баннер и заводится, успел бы случиться.
    realtime.obyavit("presence", chat_id=chat_row_id, watchers=kto)
    return {"watchers": kto}


class ProchitanoVhod(BaseModel):
    up_to: int


@router.post("/chats/{chat_row_id}/read")
def prochitano(
    chat_row_id: int,
    data: ProchitanoVhod,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Отметить, что сообщения до указанного увидели.

    **Зачем отдельная ручка, когда чтение ленты и так двигает границу.** Первый
    показ диалога границу двигает, а дочитывание после события — намеренно нет:
    оно приносит сообщения в свёрнутую вкладку, которых человек не видел. Из-за
    этого счётчик у ОТКРЫТОГО диалога рос и не снимался: менеджер смотрит прямо
    на сообщение, а слева от него горит «1» — и снять её можно только уйдя в
    другой диалог и вернувшись.

    Разницу знает только браузер: видна вкладка или свёрнута. Поэтому решение
    принимает он, а ручка принимает решение — и остаётся честной, потому что
    граница по-прежнему только растёт (`otmetit_prochitano` меньшее число
    игнорирует), то есть повторный вызов или отставший запрос не могут показать
    непрочитанным то, что уже прочитано.
    """
    if telegram_repo.get_chat(db, chat_row_id) is None:
        raise errors.NotFoundError("Chat not found", code="telegram_chat_not_found")

    realtime.otmetit_prochitano(chat_row_id, user.id, data.up_to)
    return {"ok": True}


#: Сколько живёт одно соединение потока, секунды.
#:
#: Предел нужен, и не ради тестов. Долгое соединение без него живёт, пока живо
#: приложение: посредники, мобильные операторы и сам браузер рвут такие молча,
#: а на нашей стороне остаётся висеть генератор, который об этом не знает.
#: Пять минут — это переподключение раз в пять минут, которое `EventSource`
#: делает сам и незаметно, и гарантия, что забытых соединений не накопится.
MAX_ZHIZN_POTOKA = 300


@router.get("/stream")
async def potok(
    request: Request,
    _: User = Depends(require_perm("telegram", "view")),
):
    """Поток событий: новые сообщения и смена присутствия, без перезагрузки.

    **Почему поток от сервера, а не веб-сокеты.** Одно долгое соединение в одну
    сторону — ровно то, что здесь нужно: браузер только слушает, а говорит
    обычными запросами. Веб-сокеты потребовали бы переговоров об апгрейде
    протокола через nginx и своего поведения при обрывах, а взамен дали бы
    двусторонность, которой тут применения нет.

    **Почему не длинная блокировка на подписке.** Синхронная подписка Redis
    заняла бы поток из общего пула на всё время соединения. Пул один на всё
    приложение, и полтора десятка открытых мессенджеров выели бы его целиком —
    остальные запросы встали бы в очередь. Поэтому чтение неблокирующее, а
    пауза между попытками своя: четверть секунды — это «мгновенно» для глаза и
    четыре пробуждения в секунду для машины.

    **`X-Accel-Buffering: no` обязателен.** Без него nginx копит ответ в буфере
    и отдаёт его пачкой — то есть поток перестаёт быть потоком, а становится
    очень медленным запросом. Заметить это на локальной машине без nginx нельзя
    вовсе: там всё работает.
    """
    podpiska = realtime.podpisatsya()

    async def sobytiya():
        # Первое событие — сразу, не дожидаясь новостей. Оно говорит браузеру,
        # что соединение живо: без него вкладка полминуты не знает, подключилась
        # она или висит.
        gotovnost = json.dumps({"type": "ready", "bus": podpiska is not None})
        yield f"data: {gotovnost}\n\n"

        nachalo = time.monotonic()
        posledniy_signal = nachalo
        try:
            while True:
                if await request.is_disconnected():
                    break
                if time.monotonic() - nachalo > MAX_ZHIZN_POTOKA:
                    # Кончился срок — прощаемся сами. `EventSource` в браузере
                    # переподключится без единой строки кода с нашей стороны, а
                    # экран дочитает пропущенное обычным запросом «что после».
                    break

                if podpiska is not None:
                    while True:
                        # `timeout=0` — забрать то, что уже пришло, и вернуться.
                        # Ждать здесь нельзя: см. про пул потоков выше.
                        soobshchenie = podpiska.get_message(timeout=0)
                        if not soobshchenie:
                            break
                        dannye = soobshchenie.get("data")
                        if isinstance(dannye, bytes):
                            dannye = dannye.decode("utf-8", "replace")
                        if dannye:
                            yield f"data: {dannye}\n\n"

                # Пульс раз в двадцать секунд. Не украшение: молчащее соединение
                # рвут посредники и мобильные операторы, а браузер узнаёт об
                # этом только когда попробует прочитать. Комментарий SSE
                # (строка с двоеточия) до приложения не доходит и трафика почти
                # не стоит.
                if time.monotonic() - posledniy_signal > 20:
                    posledniy_signal = time.monotonic()
                    yield ": pulse\n\n"

                await asyncio.sleep(0.25)
        finally:
            if podpiska is not None:
                try:
                    podpiska.close()
                except Exception:  # noqa: BLE001 — соединение и так закрывается
                    pass

    return StreamingResponse(
        sobytiya(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # Соединение долгое и своё: посреднику незачем его переиспользовать.
            "Connection": "keep-alive",
        },
    )
