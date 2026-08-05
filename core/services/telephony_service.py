"""Телефония: приём событий АТС, склейка их в звонки и попадание в ленту.

Главное правило блока: звонок не заводит параллельную историю. Завершённый
звонок оставляет запись в той же ленте общения, что заметки и письма
(``ClientNote``, ``kind='call'``) — иначе менеджер читает карточку в двух
местах и одно из них всегда пропускает.

Второе правило: клиента определяем сами, по номеру. Тело вебхука приходит
снаружи, и любое поле «это клиент №5» в нём — предложение станции, а не факт.
Номер — единственное, что АТС знает достоверно.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    modules_service,
    settings_service,
    task_service,
    telephony_providers,
)
from core.utils import normalize_phone, now_utc
from database.models import ClientNote, Deal, PhoneCall, SiteSetting, User
from database.models.telephony import CALL_DIRECTIONS, CALL_OUTCOMES
from database.repositories import telephony as telephony_repo

# --- настройки подключения ---
#
# Живут отдельными строками site_settings и намеренно не входят в
# SETTING_DEFAULTS: секреты не должны уезжать в общий `GET /settings`, а общий
# `PATCH /settings` не должен уметь их менять. У блока своя ручка, только root.
SETTINGS_PREFIX = "telephony_"
SETTING_PROVIDER = "telephony_provider"
SETTING_API_URL = "telephony_api_url"
SETTING_API_TOKEN = "telephony_api_token"
SETTING_WEBHOOK_SECRET = "telephony_webhook_secret"
SETTING_UTC_OFFSET = "telephony_utc_offset"
SETTING_DEFAULT_EXT = "telephony_default_ext"

# Код страны — общая настройка контактов (лежит в SETTING_DEFAULTS), но
# настраивают её здесь: без неё звонок с местного номера не находит карточку,
# и человеку незачем знать, что это «настройка из другого раздела».
SETTING_COUNTRY_CODE = "default_country_code"
EDITABLE_SETTINGS = (
    SETTING_PROVIDER,
    SETTING_API_URL,
    SETTING_API_TOKEN,
    SETTING_UTC_OFFSET,
    SETTING_DEFAULT_EXT,
    SETTING_COUNTRY_CODE,
)

# --- подпись вебхука ---
SIGNATURE_HEADER = "x-opencrm-signature"
TIMESTAMP_HEADER = "x-opencrm-timestamp"
# Окно, в котором подпись считается свежей. Меньше — и звонок теряется на
# ретрае станции после сетевого сбоя; больше — и перехваченный запрос можно
# переигрывать полдня.
SIGNATURE_TOLERANCE_SECONDS = 300


# --- настройки ---

def get_settings_values(db: Session) -> dict[str, str]:
    rows = db.scalars(select(SiteSetting).where(SiteSetting.key.like(f"{SETTINGS_PREFIX}%")))
    values = {
        SETTING_PROVIDER: "",
        SETTING_API_URL: "",
        SETTING_API_TOKEN: "",
        SETTING_WEBHOOK_SECRET: "",
        SETTING_UTC_OFFSET: "",
        SETTING_DEFAULT_EXT: "",
    }
    for row in rows:
        if row.key in values:
            values[row.key] = row.value
    return values


def public_settings(db: Session) -> dict:
    """Настройки для интерфейса: секреты не отдаём, только факт их наличия."""
    values = get_settings_values(db)
    site = settings_service.get_all(db)
    return {
        "provider": values[SETTING_PROVIDER],
        "api_url": values[SETTING_API_URL],
        "has_api_token": bool(values[SETTING_API_TOKEN]),
        "has_webhook_secret": bool(values[SETTING_WEBHOOK_SECRET]),
        "utc_offset": values[SETTING_UTC_OFFSET],
        "default_ext": values[SETTING_DEFAULT_EXT],
        # код страны — общая настройка контактов, но настраивают её здесь:
        # без неё звонок с местного номера не находит карточку
        "default_country_code": site.get("default_country_code", ""),
        "webhook_path": "/api/v1/telephony/webhook",
    }


def update_settings(db: Session, changes: dict[str, str]) -> dict:
    unknown = set(changes) - set(EDITABLE_SETTINGS)
    if unknown:
        raise errors.ValidationError(f"Unknown settings: {sorted(unknown)}", code="unknown_setting")
    if SETTING_PROVIDER in changes:
        provider = (changes[SETTING_PROVIDER] or "").strip()
        if provider and provider not in telephony_providers.PROVIDER_KEYS:
            raise errors.ValidationError(
                f"provider must be one of {telephony_providers.PROVIDER_KEYS}",
                code="bad_provider",
            )
    if SETTING_UTC_OFFSET in changes:
        _parse_offset(changes[SETTING_UTC_OFFSET])  # проверка формата на входе
    if SETTING_COUNTRY_CODE in changes:
        code = "".join(ch for ch in changes[SETTING_COUNTRY_CODE] if ch.isdigit())
        if code != (changes[SETTING_COUNTRY_CODE] or "").strip().lstrip("+"):
            raise errors.ValidationError(
                "Country code must be digits only", code="bad_country_code"
            )
        # чужая настройка — правим её же сервисом, чтобы проверки не разъехались
        settings_service.update(db, {SETTING_COUNTRY_CODE: code})
    for key, value in changes.items():
        if key == SETTING_COUNTRY_CODE:
            continue
        _write_setting(db, key, (value or "").strip())
    return public_settings(db)


def regenerate_webhook_secret(db: Session) -> str:
    """Новый общий секрет. Показывается ровно один раз — как PIN у доски."""
    secret = secrets.token_urlsafe(32)
    _write_setting(db, SETTING_WEBHOOK_SECRET, secret)
    return secret


def _write_setting(db: Session, key: str, value: str) -> None:
    row = db.scalar(select(SiteSetting).where(SiteSetting.key == key))
    if row is None:
        db.add(SiteSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = now_utc()
    db.flush()


def country_code(db: Session) -> str:
    return settings_service.get_all(db).get("default_country_code", "")


# --- подпись ---

def signature_for(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 от «метка времени + тело».

    Метка входит в подписываемое значение, а не идёт рядом просто так: иначе
    перехваченный запрос можно было бы переиграть с любой новой меткой.
    """
    payload = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    """Подпись верна и не протухла.

    Общий секрет, а не токен в заголовке, выбран сознательно:

    * токен — это пароль, который едет в каждом запросе целиком. Его видит
      каждый, кто стоит на пути (nginx, балансировщик, WAF), и он оседает в их
      логах; утёкший лог = рабочий доступ к ручке;
    * подпись доказывает знание секрета, не пересылая его, и **привязана к
      телу**: подменить номер или длительность в перехваченном запросе нельзя —
      подпись перестанет сходиться;
    * метка времени внутри подписи закрывает повтор запроса «как есть».

    Сравнение — ``compare_digest``: обычное сравнение строк отвечает тем
    быстрее, чем раньше разошлись байты, и по времени ответа подпись
    подбирается посимвольно.
    """
    if not secret or not signature or not timestamp:
        return False
    try:
        sent_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    if abs((now_utc() - sent_at).total_seconds()) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(signature_for(secret, timestamp, body), signature.strip().lower())


# --- время АТС ---

def _parse_offset(value: str | None) -> timedelta:
    """Смещение зоны АТС из настройки вида ``+03:00`` / ``-05:30`` / пусто.

    Именованные зоны (``Europe/Kyiv``) не берём намеренно: базы часовых поясов
    на сервере может не быть вовсе (Windows, slim-образы), и настройка молча
    превращалась бы в UTC — а это сдвиг всей ленты на два часа, который никто
    не заметит. Правильный путь всё равно другой: просить АТС слать время со
    смещением прямо в строке, тогда эта настройка не нужна.
    """
    raw = (value or "").strip()
    if not raw:
        return timedelta(0)
    sign = 1
    if raw[0] in "+-":
        sign = -1 if raw[0] == "-" else 1
        raw = raw[1:]
    parts = raw.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError as exc:
        raise errors.ValidationError(
            "UTC offset must look like +03:00", code="bad_utc_offset"
        ) from exc
    if len(parts) > 2 or not (0 <= hours <= 14) or not (0 <= minutes < 60):
        raise errors.ValidationError("UTC offset must look like +03:00", code="bad_utc_offset")
    return sign * timedelta(hours=hours, minutes=minutes)


def parse_started_at(value: Any, offset: timedelta) -> datetime:
    """Момент начала звонка в naive UTC.

    В базе время всегда naive UTC (docs/03), а АТС живёт в своей зоне. Если в
    строке есть смещение или ``Z`` — верим ей: это самый надёжный источник.
    Если время пришло без зоны — считаем его местным для станции и вычитаем
    настроенное смещение.
    """
    if value in (None, ""):
        return now_utc()
    if isinstance(value, (int, float)):
        # unix-время всегда в UTC по определению — смещение к нему не применяем
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise errors.ValidationError("started_at is not a valid datetime", code="bad_started_at") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed - offset


# --- приём событий ---

def ingest_event(db: Session, payload: dict) -> PhoneCall:
    """Обрабатывает одно событие звонка от АТС.

    По звонку событий несколько (начался → ответили → завершился), и все они
    правят одну и ту же строку, найденную по ``external_id``. Поэтому повтор
    того же события ничего не удваивает: ни звонок, ни запись в ленте.
    """
    external_id = str(payload.get("call_id") or payload.get("id") or "").strip()
    if not external_id:
        raise errors.ValidationError("call_id is required", code="call_id_required")
    direction = str(payload.get("direction") or "").strip()
    outcome = _clean_outcome(payload.get("outcome") or payload.get("status"))
    settings_values = get_settings_values(db)
    offset = _parse_offset(settings_values[SETTING_UTC_OFFSET])
    code = country_code(db)

    call = telephony_repo.get_by_external_id(db, external_id)
    if call is None:
        if direction not in CALL_DIRECTIONS:
            raise errors.ValidationError(
                f"direction must be one of {CALL_DIRECTIONS}", code="bad_direction"
            )
        call = PhoneCall(
            external_id=external_id,
            direction=direction,
            started_at=parse_started_at(payload.get("started_at"), offset),
        )
        db.add(call)
    elif direction in CALL_DIRECTIONS:
        call.direction = direction

    if "started_at" in payload and payload["started_at"]:
        call.started_at = parse_started_at(payload["started_at"], offset)

    from_number = str(payload.get("from") or payload.get("caller") or "").strip()
    to_number = str(payload.get("to") or payload.get("callee") or "").strip()
    if from_number:
        call.from_number = from_number[:64]
        call.from_number_norm = normalize_phone(from_number, code)[:32]
    if to_number:
        call.to_number = to_number[:64]
        call.to_number_norm = normalize_phone(to_number, code)[:32]

    if "duration" in payload or "duration_sec" in payload:
        call.duration_sec = _clean_duration(payload.get("duration", payload.get("duration_sec")))
    if outcome is not None:
        call.outcome = outcome
    recording = str(payload.get("recording") or payload.get("recording_path") or "").strip()
    if recording:
        call.recording_path = recording[:500]

    # Кто говорил — можно принять от станции: у неё есть внутренние номера и
    # почты операторов, а подпись вебхука подтверждает, что запрос от неё.
    # Клиента так брать нельзя (см. ниже): про клиентов станция ничего не знает.
    operator = str(payload.get("operator_email") or "").strip().lower()
    if operator and call.user_id is None:
        user = db.scalar(select(User).where(User.email == operator))
        if user is not None:
            call.user_id = user.id

    _link_client(db, call)
    db.flush()
    _sync_feed_note(db, call)
    return call


def _link_client(db: Session, call: PhoneCall) -> None:
    """Определяет клиента по номеру собеседника.

    Клиент по звонку **не создаётся**: телефон обзвонили роботы, ошиблись
    номером, позвонил курьер — и справочник за неделю превращается в свалку
    из карточек без имени, которые потом никто не разберёт. Кто из звонивших
    клиент, решает человек: звонок с неизвестного номера остаётся в журнале
    непривязанным, и из него карточка заводится одним нажатием.
    """
    if call.client_id is not None:
        return
    number = call.from_number_norm if call.direction == "in" else call.to_number_norm
    client = telephony_repo.find_client_by_number(db, number)
    if client is not None:
        call.client_id = client.id


def _clean_outcome(value: Any) -> str | None:
    outcome = str(value or "").strip().lower()
    if not outcome:
        return None
    if outcome not in CALL_OUTCOMES:
        raise errors.ValidationError(f"outcome must be one of {CALL_OUTCOMES}", code="bad_outcome")
    return outcome


def _clean_duration(value: Any) -> int | None:
    """NULL и 0 — разные вещи.

    NULL — «длительность неизвестна» (звонок ещё идёт, станция её не прислала),
    0 — «соединились и сразу положили трубку». Поэтому пустое значение не
    превращается в ноль, а остаётся пустым.
    """
    if value in (None, ""):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise errors.ValidationError("duration must be a number", code="bad_duration") from exc
    return max(seconds, 0)


# --- единая лента ---

def _sync_feed_note(db: Session, call: PhoneCall) -> None:
    """Завершённый звонок попадает в ленту клиента — один раз.

    Пока звонок идёт (``outcome`` пуст), в ленте ему делать нечего: запись
    появилась бы раньше, чем стало известно, состоялся разговор или нет.
    Клиент неизвестен — записи тоже нет: лента принадлежит карточке.
    """
    if call.outcome is None or call.client_id is None:
        return
    body = feed_body(call)
    if call.note_id is not None:
        note = db.get(ClientNote, call.note_id)
        if note is not None:
            # событие может уточнить итог и длительность — правим существующую
            # запись, а не добавляем в ленту вторую
            note.body = body
            note.happened_at = call.started_at
            note.direction = call.direction
            note.deal_id = call.deal_id
            return
    note = ClientNote(
        client_id=call.client_id,
        author_id=call.user_id,
        kind="call",
        direction=call.direction,
        deal_id=call.deal_id,
        body=body,
        # Время ленты — момент начала разговора, а не момент прихода события:
        # событие о завершении приходит с задержкой, а иногда и повтором через
        # час, и лента разъезжалась бы с тем, что помнит менеджер.
        happened_at=call.started_at,
    )
    db.add(note)
    db.flush()
    call.note_id = note.id


def feed_body(call: PhoneCall) -> str:
    """Текст записи в ленте.

    По-английски: язык интерфейса у каждого сотрудника свой, а тело заметки
    хранится строкой и переводу задним числом не подлежит — английский здесь
    язык продукта по умолчанию, как и в остальном UI.

    Пропущенный звонок начинается со слова «Missed» — в ленте, где всё
    выглядит одинаково, он обязан цепляться взглядом.
    """
    number = call.from_number if call.direction == "in" else call.to_number
    number = number or "unknown number"
    preposition = "from" if call.direction == "in" else "to"
    if call.outcome == "missed":
        return f"Missed call {preposition} {number}"
    kind = "Incoming call" if call.direction == "in" else "Outgoing call"
    if call.outcome != "answered":
        return f"{kind} {preposition} {number} — {call.outcome}"
    if call.duration_sec is None:
        return f"{kind} {preposition} {number}"
    return f"{kind} {preposition} {number} — {_format_duration(call.duration_sec)}"


def _format_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- звонок из CRM ---

def click_to_call(
    db: Session,
    user: User,
    number: str,
    from_ext: str = "",
    deal_id: int | None = None,
) -> PhoneCall:
    """Просит станцию соединить сотрудника с номером.

    Строку звонка заводим сразу, ещё до событий вебхука: мы уже знаем, кто
    звонит и кому, — а станция потом дополнит её тем же ``external_id``.
    Иначе исходящий звонок оставался бы без сотрудника: во входящих событиях
    АТС не знает, кто нажал кнопку.

    ``deal_id`` приходит из карточки заявки, откуда нажали кнопку. Это знание
    из первых рук — от сотрудника, а не от станции, — поэтому ему верим и
    сразу кладём звонок в нужную заявку.
    """
    number = (number or "").strip()
    if not number:
        raise errors.ValidationError("Number is required", code="number_required")
    values = get_settings_values(db)
    ext = (from_ext or values[SETTING_DEFAULT_EXT] or "").strip()
    if not ext:
        raise errors.ValidationError(
            "Operator extension is not set", code="telephony_no_extension"
        )
    provider = telephony_providers.build(
        values[SETTING_PROVIDER], values[SETTING_API_URL], values[SETTING_API_TOKEN]
    )
    result = provider.originate(ext, number)

    call = telephony_repo.get_by_external_id(db, result.external_id)
    code = country_code(db)
    if call is None:
        call = PhoneCall(external_id=result.external_id, direction="out", started_at=now_utc())
        db.add(call)
    call.from_number = ext[:64]
    call.from_number_norm = normalize_phone(ext, code)[:32]
    call.to_number = number[:64]
    call.to_number_norm = normalize_phone(number, code)[:32]
    call.user_id = user.id
    _link_client(db, call)
    if deal_id is not None:
        attach_deal(db, call, deal_id)
    db.flush()
    return call


# --- привязка к заявке ---

def attach_deal(db: Session, call: PhoneCall, deal_id: int | None) -> PhoneCall:
    """Связывает звонок с заявкой (или снимает связь).

    Делает это человек, а не станция: АТС знает номера, но не знает, к какому
    заказу относился разговор. Запись в ленте переезжает вместе со звонком —
    иначе в ленте заявки звонка не было бы, а ради этого всё и затевалось.
    """
    if deal_id is None:
        call.deal_id = None
    else:
        deal = db.get(Deal, deal_id)
        if deal is None:
            raise errors.NotFoundError("Deal not found", code="deal_not_found")
        if call.client_id is not None and deal.client_id != call.client_id:
            raise errors.ValidationError(
                "Deal belongs to another client", code="deal_other_client"
            )
        # Звонок с незнакомого номера привязали к заявке — заодно узнали и
        # клиента: решение принял человек, гадать не пришлось.
        call.client_id = deal.client_id
        call.deal_id = deal.id
    db.flush()
    _sync_feed_note(db, call)
    return call


# --- напоминание перезвонить ---

def create_callback_task(db: Session, call: PhoneCall, user: User):
    """Заводит задачу «перезвонить» по пропущенному звонку.

    Пропущенный звонок — это несостоявшийся разговор, и вся его ценность в
    том, чтобы он не потерялся. Само напоминание живёт в блоке ``tasks``:
    заводить свой список дел рядом с чужим — верный способ получить два места,
    где ищут одно и то же.

    Блок напоминаний выключен — возможности просто нет: интерфейс её не
    показывает, а ручка отвечает понятной ошибкой, а не пятисоткой.
    """
    if call.outcome != "missed":
        raise errors.ValidationError(
            "Callback reminders are for missed calls", code="call_not_missed"
        )
    if not modules_service.is_enabled(db, "tasks"):
        raise errors.ConflictError("Tasks module is off", code="module_disabled")
    number = call.from_number if call.direction == "in" else call.to_number
    return task_service.create(
        db,
        {
            "title": f"Перезвонить {number}",
            # Час — не догма, а разумное «сегодня же»: пропущенный звонок
            # протухает быстро, но перезванивать через минуту после гудка не
            # всегда уместно. Срок правится в самой задаче.
            "due_at": now_utc() + timedelta(hours=1),
            "client_id": call.client_id,
            "deal_id": call.deal_id,
        },
        user,
    )
