"""Переписка с клиентами через бота фирмы: пока настройки подключения.

Все ручки закрыты блоком ``telegram`` — выключили блок, и раздела нет ни в
меню, ни в API. Это правило проекта, а не удобство: спрятать пункт меню
недостаточно, адрес остаётся рабочим и лежит в закладках.

Приём сообщений от телеграма появится отдельной ручкой и БЕЗ зависимости от
блока — по той же причине, по какой так сделан вебхук АТС: телеграм не умеет
узнать, что канал выключили, и продолжит доставлять; отвечать ему пятисоткой
значило бы копить у него очередь повторов.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.ratelimit import SlidingWindowLimiter
from core.services import telegram_service
from database.models import User
from database.repositories import clients as clients_repo
from database.repositories import telegram as telegram_repo
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
    """Отключить бота. Переписка остаётся — отключение про связь, а не про данные."""
    return telegram_service.otklyuchit(db)


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
        "author_id": row.author_id,
        "send_state": row.send_state,
        "send_error": row.send_error,
        "happened_at": row.happened_at,
    }


# --- приём от телеграма ------------------------------------------------------
#
# Отдельным роутером и БЕЗ зависимости от блока — по той же причине, что у
# вебхука АТС: телеграм не умеет узнать, что канал выключили. Отвечай мы ему
# отказом, он копил бы у себя очередь повторов и вывалил её разом при
# включении. Молчаливое «принято» на выключенном блоке честнее: сообщения при
# этом не теряются, они просто не приходят.
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
    if webhook_limiter.is_blocked(adres):
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
        return telegram_service.prinyat(db, obnovlenie)
    except Exception as beda:  # noqa: BLE001
        # Разбор не удался — сообщаем себе в лог и отвечаем 200. Повтор того же
        # сообщения не починит наш разбор, а очередь у телеграма вырастет.
        logger.exception("не разобрали обновление телеграма: %r", beda)
        return {"status": "error"}


# --- переписка ---------------------------------------------------------------


@router.get("/chats")
def dialogi(
    q: str = Query("", max_length=MAX_SEARCH),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("telegram", "view")),
) -> dict:
    """Список диалогов, свежие сверху."""
    stroki, vsego = telegram_repo.spisok_dialogov(db, q=q.strip(), page=page, per_page=per_page)
    return {
        "items": [_dialog_naruzhu(row) for row in stroki],
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
    _: User = Depends(require_perm("telegram", "view")),
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
    return {"items": [_soobshchenie_naruzhu(row) for row in stroki]}


class OtvetVhod(BaseModel):
    text: str = ""


@router.post("/chats/{chat_row_id}/messages", status_code=201)
def otvetit(
    chat_row_id: int,
    data: OtvetVhod,
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "create")),
) -> dict:
    """Ответить клиенту текстом."""
    stroka = telegram_service.otpravit(db, chat_row_id, tekst=data.text, author=user)
    return _soobshchenie_naruzhu(stroka)


@router.post("/chats/{chat_row_id}/files", status_code=201)
async def otpravit_fayl(
    chat_row_id: int,
    file: UploadFile = File(...),
    caption: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_perm("telegram", "create")),
) -> dict:
    """Отправить клиенту файл: картинку, видео или документ."""
    soderzhimoe = await file.read()
    if not soderzhimoe:
        raise errors.ValidationError("File is empty", code="file_empty")
    if len(soderzhimoe) > get_settings().max_upload_bytes:
        raise errors.ValidationError("File is too large", code="file_too_large")
    stroka = telegram_service.otpravit(
        db,
        chat_row_id,
        tekst=caption,
        author=user,
        fayl=(file.filename or "file", soderzhimoe),
    )
    return _soobshchenie_naruzhu(stroka)


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
