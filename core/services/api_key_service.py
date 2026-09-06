"""Ключи доступа для сайта: выдача, проверка, области, отзыв, ротация.

Разбор — `docs/ustroystvo/16-api-sayta.md` §8–§9. Коротко: в базе только отпечаток, ключ
показывается один раз; отзыв — отметка, а не удаление; ротация оставляет старый
ключ жить сутки; областей шесть, и блок проверяется раньше области.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.security import tokens
from core.services import audit_service, modules_service
from core.utils import PRESENCE_TOUCH_SECONDS, now_utc
from database.models import ApiKey, User
from database.models.api_key import SCOPE_MODULE, SCOPE_STOCK_READ, SCOPES, STOCK_MODES
from database.models.audit import SOURCE_MANUAL
from database.models.warehouse import WH_SHOP
from database.repositories import api_keys as keys_repo
from database.repositories import warehouses as places_repo

#: Именной заголовок, как у всех внешних ручек проекта: `X-API-Key` без владельца
#: на сервере с несколькими службами за одним nginx уезжает не в ту.
HEADER = "X-OpenCRM-Api-Key"
PREFIX_LEN = 8
#: Сколько живёт старый ключ после ротации — иначе смена ключа значит простой сайта.
GRACE_HOURS = 24
DEFAULT_DAYS = 365


def _otpechatok(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def porodit() -> str:
    """Сам ключ: 32 байта случайности, url-safe. Показывается один раз."""
    return secrets.token_urlsafe(32)


def _proverit_oblasti(db: Session, scopes, warehouse_id: int | None) -> tuple[set[str], int | None]:
    oblasti = {s.strip() for s in scopes if s and s.strip()}
    lishnie = sorted(oblasti - set(SCOPES))
    if lishnie:
        raise errors.ValidationError(f"Unknown scopes: {', '.join(lishnie)}", code="unknown_scope")
    if not oblasti:
        raise errors.ValidationError("At least one scope is required", code="scope_required")
    if not warehouse_id:
        # Склад обязателен только при `stock.read`: услуги нигде не лежат, и
        # ключ с одним `catalog.read` отдаёт прайс без всякого зала.
        if SCOPE_STOCK_READ in oblasti:
            raise errors.ValidationError(
                "stock.read needs a warehouse of kind shop", code="warehouse_required"
            )
        return oblasti, None
    sklad = places_repo.get(db, warehouse_id)
    if sklad is None:
        raise errors.NotFoundError("Warehouse not found", code="warehouse_not_found")
    if sklad.kind != WH_SHOP:
        # Отказ на месте, а не 409 через неделю, когда сайт уже написан.
        raise errors.ValidationError(
            f"Warehouse '{sklad.name}' is not a shop (kind={sklad.kind})", code="warehouse_not_shop"
        )
    return oblasti, sklad.id


def _chislo(data: dict, imya: str, umolchanie: int, *, minimum: int) -> int:
    znachenie = data.get(imya)
    if znachenie is None:
        return umolchanie
    try:
        znachenie = int(znachenie)
    except (TypeError, ValueError):
        raise errors.ValidationError(f"{imya} must be an integer", code="bad_number") from None
    if znachenie < minimum:
        raise errors.ValidationError(f"{imya} must be at least {minimum}", code="bad_number")
    return znachenie


def create(db: Session, actor: User | None, data: dict) -> tuple[ApiKey, str]:
    """Выдать ключ. Возвращает строку ключа — единственный раз, когда она существует."""
    name = (data.get("name") or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    oblasti, warehouse_id = _proverit_oblasti(db, data.get("scopes") or (), data.get("warehouse_id"))
    stock_mode = data.get("stock_mode") or "bucket"
    if stock_mode not in STOCK_MODES:
        raise errors.ValidationError(f"Unknown stock mode: {stock_mode}", code="unknown_stock_mode")
    days = _chislo(data, "days", DEFAULT_DAYS, minimum=0)
    raw = porodit()
    key = ApiKey(
        name=name[:120],
        prefix=raw[:PREFIX_LEN],
        token_hash=_otpechatok(raw),
        warehouse_id=warehouse_id,
        stock_mode=stock_mode,
        few_threshold_milli=_chislo(data, "few_threshold_milli", 5000, minimum=0),
        rate_per_min=_chislo(data, "rate_per_min", 120, minimum=1),
        max_reserve_minutes=_chislo(data, "max_reserve_minutes", 1440, minimum=1),
        ttl_sec=_chislo(data, "ttl_sec", 60, minimum=5),
        # Бессрочный ключ надо запросить явно (`days=0`): это ключ, который
        # никто никогда не отзовёт.
        expires_at=(now_utc() + timedelta(days=days)) if days else None,
        created_by=actor.id if actor else None,
    )
    keys_repo.add(db, key, oblasti)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_APIKEY_CREATED,
        entity_type=audit_service.ENTITY_APIKEY,
        entity_id=key.id,
        entity_label=key.name,
        after=", ".join(sorted(oblasti)),
    )
    return key, raw


def get(db: Session, key_id: int) -> ApiKey:
    key = keys_repo.get(db, key_id)
    if key is None:
        raise errors.NotFoundError("API key not found", code="api_key_not_found")
    return key


def revoke(db: Session, actor: User | None, key_id: int) -> ApiKey:
    key = get(db, key_id)
    if key.revoked_at is None:
        key.revoked_at = now_utc()
        db.flush()
        audit_service.record(
            db,
            actor=actor,
            source=SOURCE_MANUAL,
            action=audit_service.ACTION_APIKEY_REVOKED,
            entity_type=audit_service.ENTITY_APIKEY,
            entity_id=key.id,
            entity_label=key.name,
        )
    return key


def rotate(db: Session, actor: User | None, key_id: int, grace_hours: int = GRACE_HOURS) -> tuple[ApiKey, str]:
    """Новый ключ с теми же полями; старый живёт ещё `grace_hours`, а не умирает сразу."""
    staryy = get(db, key_id)
    if staryy.revoked_at is not None:
        raise errors.ConflictError("A revoked key cannot be rotated", code="api_key_revoked")
    oblasti = keys_repo.scopes_of(db, staryy.id)
    raw = porodit()
    novyy = ApiKey(
        name=staryy.name,
        prefix=raw[:PREFIX_LEN],
        token_hash=_otpechatok(raw),
        warehouse_id=staryy.warehouse_id,
        stock_mode=staryy.stock_mode,
        few_threshold_milli=staryy.few_threshold_milli,
        rate_per_min=staryy.rate_per_min,
        max_reserve_minutes=staryy.max_reserve_minutes,
        ttl_sec=staryy.ttl_sec,
        expires_at=staryy.expires_at,
        created_by=actor.id if actor else None,
    )
    keys_repo.add(db, novyy, oblasti)
    lgota = now_utc() + timedelta(hours=max(0, grace_hours))
    if staryy.expires_at is None or staryy.expires_at > lgota:
        staryy.expires_at = lgota
    db.flush()
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_APIKEY_ROTATED,
        entity_type=audit_service.ENTITY_APIKEY,
        entity_id=staryy.id,
        entity_label=staryy.name,
        after=f"new key {novyy.id}, old one expires {lgota.isoformat(timespec='seconds')}",
    )
    return novyy, raw


IZMENYAEMYE = ("name", "stock_mode", "few_threshold_milli", "rate_per_min", "max_reserve_minutes", "ttl_sec")


def update(db: Session, actor: User | None, key_id: int, data: dict) -> ApiKey:
    """Правка полей ключа. Области и склад не правятся — на них выпускают новый."""
    key = get(db, key_id)
    bylo = {imya: getattr(key, imya) for imya in IZMENYAEMYE}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise errors.ValidationError("Name is required", code="name_required")
        key.name = name[:120]
    if "stock_mode" in data and data["stock_mode"] is not None:
        if data["stock_mode"] not in STOCK_MODES:
            raise errors.ValidationError(f"Unknown stock mode: {data['stock_mode']}", code="unknown_stock_mode")
        key.stock_mode = data["stock_mode"]
    key.few_threshold_milli = _chislo(data, "few_threshold_milli", key.few_threshold_milli, minimum=0)
    key.rate_per_min = _chislo(data, "rate_per_min", key.rate_per_min, minimum=1)
    key.max_reserve_minutes = _chislo(data, "max_reserve_minutes", key.max_reserve_minutes, minimum=1)
    key.ttl_sec = _chislo(data, "ttl_sec", key.ttl_sec, minimum=5)
    db.flush()
    stalo = {imya: getattr(key, imya) for imya in IZMENYAEMYE}
    if stalo != bylo:
        audit_service.record(
            db,
            actor=actor,
            source=SOURCE_MANUAL,
            action=audit_service.ACTION_APIKEY_UPDATED,
            entity_type=audit_service.ENTITY_APIKEY,
            entity_id=key.id,
            entity_label=key.name,
            before=", ".join(f"{k}={v}" for k, v in bylo.items() if stalo[k] != v),
            after=", ".join(f"{k}={v}" for k, v in stalo.items() if bylo[k] != v),
        )
    return key


def list_keys(db: Session) -> list[dict]:
    keys = keys_repo.list_all(db)
    oblasti = keys_repo.scopes_by_keys(db, [k.id for k in keys])
    return [key_out(k, oblasti.get(k.id, set())) for k in keys]


def sostoyanie(key: ApiKey, now=None) -> str:
    now = now or now_utc()
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= now:
        return "expired"
    return "active"


def key_out(key: ApiKey, scopes) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "scopes": sorted(scopes),
        "warehouse_id": key.warehouse_id,
        "stock_mode": key.stock_mode,
        "few_threshold_milli": key.few_threshold_milli,
        "rate_per_min": key.rate_per_min,
        "max_reserve_minutes": key.max_reserve_minutes,
        "ttl_sec": key.ttl_sec,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "created_by": key.created_by,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "last_used_ip": key.last_used_ip,
        "state": sostoyanie(key),
    }


def alive_count(db: Session) -> int:
    return keys_repo.alive_count(db, now_utc())


# --- проверка при запросе ------------------------------------------------------


def authenticate(db: Session, raw: str, ip: str) -> ApiKey:
    """Ключ из заголовка → строка ключа. Отказ — один код на «нет» и «не тот»."""
    raw = (raw or "").strip()
    # ASCII проверяется ДО хэширования: заголовки Starlette декодирует как
    # latin-1, и байт 0xFF уже стоил проекту пятисотки на телефонии.
    if not raw or not raw.isascii() or len(raw) > 128:
        raise errors.AuthError("Bad API key", code="bad_api_key")
    key = keys_repo.get_by_hash(db, _otpechatok(raw))
    if key is None:
        raise errors.AuthError("Bad API key", code="bad_api_key")
    now = now_utc()
    if key.revoked_at is not None:
        raise errors.AuthError("API key is revoked", code="api_key_revoked")
    if key.expires_at is not None and key.expires_at <= now:
        raise errors.AuthError("API key has expired", code="api_key_expired")
    # Не чаще раза в минуту: запись на самом частом пути системы ради поля,
    # которое смотрят раз в месяц. Тот же приём, что у присутствия сотрудника.
    if key.last_used_at is None or (now - key.last_used_at).total_seconds() >= PRESENCE_TOUCH_SECONDS:
        keys_repo.touch(db, key, ip, now)
    return key


def require_scope(db: Session, key: ApiKey, scope: str) -> None:
    """Блок включён → есть область. Не наоборот: «блок выключен» отправляет к
    переключателю, «нет области» — искать несуществующую ошибку в ключе."""
    module = SCOPE_MODULE[scope]
    if module is not None and not modules_service.is_enabled(db, module):
        raise errors.ForbiddenError(f"Module '{module}' is switched off", code="module_disabled")
    if scope not in keys_repo.scopes_of(db, key.id):
        raise errors.ForbiddenError(f"Scope required: {scope}", code="scope_required")


def serving_warehouse(db: Session, key: ApiKey):
    """Склад ключа: существует, жив и по-прежнему магазин. Иначе 409, а не подмена.

    `None` — у ключа склада нет вовсе (только услуги и прайс); это не отказ.
    """
    if key.warehouse_id is None:
        return None
    sklad = places_repo.get(db, key.warehouse_id)
    if sklad is None or sklad.deleted_at is not None or sklad.kind != WH_SHOP:
        raise errors.ConflictError(
            "The key's warehouse is closed or no longer a shop", code="warehouse_not_serving"
        )
    return sklad


def customer_ref(key: ApiKey, client_id: int) -> str:
    """Ссылка на клиента для сайта: производная от пары (ключ, клиент), не наш id."""
    return hmac.new(
        get_settings().secret_key.encode("utf-8"),
        f"{key.id}:{client_id}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]


def hash_ip(ip: str) -> str:
    return tokens.hash_ip(ip)
