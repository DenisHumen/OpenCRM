"""Телефония: журнал звонков, click-to-call, настройки и вебхук АТС.

Все рабочие ручки закрыты блоком ``telephony`` — выключили блок, и раздела нет
ни в меню, ни в API. Вебхук закрыт иначе, и почему — написано у него.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.ratelimit import SlidingWindowLimiter
from core.security import tokens
from core.services import modules_service, telephony_service
from core.utils import normalize_phone, to_utc_naive
from database.models import PhoneCall, User
from database.models.telephony import CALL_DIRECTIONS, CALL_OUTCOMES
from database.repositories import telephony as telephony_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, client_ip, get_db, require_module, require_perm

# Рабочие ручки: сотрудник + включённый блок.
router = APIRouter(
    prefix="/telephony",
    tags=["telephony"],
    dependencies=[Depends(require_module("telephony"))],
)

# Вебхук вынесен в отдельный роутер без зависимости блока — см. его описание.
webhook_router = APIRouter(prefix="/telephony", tags=["telephony"])

# Поток вебхука: станция шлёт по 3–5 событий на звонок, десяток параллельных
# разговоров — это единицы запросов в секунду. Всё, что выше, — либо шторм от
# сломавшейся станции, либо чужой перебор подписи; и то и другое лучше
# притормозить, чем разбирать по логам.
webhook_limiter = SlidingWindowLimiter(600, 60, name="webhook")

# Значения фильтров журнала проверяются списком, как в почте
# (`web/api/routes/mail.py`), но список берётся из модели, а не переписывается
# здесь: переписанный он разъедется с `CALL_OUTCOMES` на первом же новом итоге,
# и разойдётся молча. Незнакомое значение — отказ 422, а не пустая выдача:
# пустой список неотличим от «звонков нет», и опечатку в фильтре («outcome=miss»,
# «direction=incoming») ищут потом в данных, которых там и не было.
DIRECTION_FILTER = f"^({'|'.join(CALL_DIRECTIONS)})$"
OUTCOME_FILTER = f"^({'|'.join(CALL_OUTCOMES)})$"


class ClickToCallIn(BaseModel):
    number: str
    #: внутренний номер сотрудника; пусто — берётся из настроек
    from_ext: str = ""
    #: заявка, из карточки которой звонят
    deal_id: int | None = None


class CallPatchIn(BaseModel):
    #: заявка, к которой относится разговор; null — снять связь
    deal_id: int | None = None


class TelephonySettingsIn(BaseModel):
    values: dict[str, str]


# --- журнал ---

@router.get("/calls")
def list_calls(
    direction: str | None = Query(default=None, pattern=DIRECTION_FILTER),
    outcome: str | None = Query(default=None, pattern=OUTCOME_FILTER),
    client_id: int | None = None,
    deal_id: int | None = None,
    user_id: int | None = None,
    number: str | None = Query(default=None, max_length=MAX_SEARCH),
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_perm("telephony", "view")),
    db: Session = Depends(get_db),
):
    # номер из поля поиска приводим так же, как хранимый, — иначе «+380 67»
    # не найдёт «380671234567»
    if number:
        number = normalize_phone(number, telephony_service.country_code(db)) or number
    items, total = telephony_repo.list_calls(
        db,
        direction=direction,
        outcome=outcome,
        client_id=client_id,
        deal_id=deal_id,
        user_id=user_id,
        number=number,
        # Границы приводим к тому виду, в каком время лежит в базе. Без этого
        # «13:00+03:00» сравнивалось с naive UTC как 13:00 — фильтр молча врал
        # ровно на смещение зоны и терял звонки, попадающие в период.
        since=to_utc_naive(since),
        until=to_utc_naive(until),
        page=page,
        per_page=per_page,
    )
    return schemas.paginated([schemas.call_out(c) for c in items], total, page, per_page)


@router.get("/calls/{call_id}")
def get_call(
    call_id: int,
    _: User = Depends(require_perm("telephony", "view")),
    db: Session = Depends(get_db),
):
    return schemas.call_out(_call(db, call_id))


@router.patch("/calls/{call_id}")
def update_call(
    call_id: int,
    payload: CallPatchIn,
    _: User = Depends(require_perm("telephony", "edit")),
    db: Session = Depends(get_db),
):
    """Привязать разговор к заявке или отвязать. Больше в звонке править нечего:
    остальное — факт, пришедший от станции."""
    call = _call(db, call_id)
    # Пустой PATCH не должен отвязывать заявку: `null` означает «отвязать»
    # только тогда, когда его прислали явно.
    if "deal_id" in payload.model_fields_set:
        call = telephony_service.attach_deal(db, call, payload.deal_id)
    return schemas.call_out(call)


@router.post("/calls/{call_id}/callback-task", status_code=201)
def create_callback_task(
    call_id: int,
    user: User = Depends(require_perm("telephony", "create")),
    db: Session = Depends(get_db),
):
    """Напоминание перезвонить по пропущенному звонку (блок ``tasks``)."""
    task = telephony_service.create_callback_task(db, _call(db, call_id), user)
    return schemas.task_out(task)


@router.post("/click-to-call", status_code=201)
def click_to_call(
    payload: ClickToCallIn,
    user: User = Depends(require_perm("telephony", "create")),
    db: Session = Depends(get_db),
):
    call = telephony_service.click_to_call(
        db, user, payload.number, payload.from_ext, payload.deal_id
    )
    return schemas.call_out(call)


# --- настройки подключения (root) ---

@router.get("/settings")
def get_telephony_settings(_: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)):
    return telephony_service.public_settings(db)


@router.patch("/settings")
def update_telephony_settings(
    payload: TelephonySettingsIn,
    _: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    return telephony_service.update_settings(db, payload.values)


@router.post("/settings/secret", status_code=201)
def regenerate_secret(_: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)):
    """Новый секрет подписи. Возвращается один раз — записать и вставить в АТС."""
    return {"secret": telephony_service.regenerate_webhook_secret(db)}


# --- вебхук АТС ---

@webhook_router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Точка приёма событий звонка от АТС.

    Сессии здесь нет и быть не может: запрос шлёт станция, а не браузер.
    Единственное доказательство отправителя — подпись тела общим секретом
    (``core/services/telephony_service.verify_signature``, там же обоснование
    выбора подписи вместо токена).

    **Почему вебхук не закрыт блоком.** У станции нет способа узнать, что
    владелец CRM выключил телефонию: она продолжит слать события, а на 403
    начнёт ретраить и заваливать себе очередь и наш лог. Поэтому запрос
    принимается всегда — но при выключенном блоке ничего не записывается и
    ответ честно говорит об этом. Так станция получает окончательный ответ и
    успокаивается, а база не копит данные блока, которым не пользуются.

    Подпись при этом проверяется в любом случае: иначе выключенный блок
    превращался бы в открытую ручку, принимающую что угодно от кого угодно.
    """
    ip_key = tokens.hash_ip(client_ip(request))
    if webhook_limiter.is_blocked(ip_key):
        raise errors.RateLimitedError("Too many webhook calls", code="webhook_rate_limited")
    # считаем каждый запрос, а не только неудачный: ограничиваем поток, а не
    # подбор — подбор подписи ограничивается им же заодно
    webhook_limiter.record_failure(ip_key)

    body = await request.body()
    secret = telephony_service.get_settings_values(db)[telephony_service.SETTING_WEBHOOK_SECRET]
    if not secret:
        # секрет не задан — принимать анонимные события неоткуда и незачем
        raise errors.AuthError("Webhook is not configured", code="webhook_not_configured")
    signature = request.headers.get(telephony_service.SIGNATURE_HEADER, "")
    timestamp = request.headers.get(telephony_service.TIMESTAMP_HEADER, "")
    if not telephony_service.verify_signature(secret, timestamp, signature, body):
        raise errors.AuthError("Bad webhook signature", code="bad_signature")

    if not modules_service.is_enabled(db, "telephony"):
        return {"status": "ignored", "reason": "module_disabled"}

    try:
        payload = await request.json()
    except ValueError as exc:
        # Оборванная передача или мусор вместо тела — отказ с объяснением, а не
        # 500. Станция на ошибку сервера считает событие недоставленным и шлёт
        # его снова: одно кривое тело раскручивало петлю повторов.
        raise errors.ValidationError(
            "Event must be valid JSON", code="bad_payload"
        ) from exc
    if not isinstance(payload, dict):
        raise errors.ValidationError("Event must be a JSON object", code="bad_payload")
    call = telephony_service.ingest_event(db, payload)
    return {"status": "ok", "call": schemas.call_out(call)}


def _call(db: Session, call_id: int) -> PhoneCall:
    call = telephony_repo.get(db, call_id)
    if call is None:
        raise errors.NotFoundError("Call not found", code="call_not_found")
    return call
