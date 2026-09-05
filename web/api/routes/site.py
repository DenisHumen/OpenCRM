"""API сайта магазина: всё под `/api/v1/site/`, всё по ключу `X-OpenCRM-Api-Key`.

Сессии здесь нет и быть не может: запрос шлёт сервер сайта, а не браузер. Вместо
неё — ключ с областями (`core/services/api_key_service.py`) и два ограничителя,
которые считают разное (`docs/16-api-sayta.md` §9): ключ известен — обращения на
ключ; ключ неизвестен — промахи по адресу. Порядок: сначала поток, потом ключ,
иначе подбирающий ключ не считался бы вовсе.

Префикс `site`, а не `public`: `public` в проекте значит «сессии не спрашивают
никогда», здесь же спрашивают ключ, и путать две поверхности нельзя.
"""

import hashlib
import json
import threading

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.ratelimit import SlidingWindowLimiter
from core.security import tokens
from core.services import api_stats_service, api_key_service, lead_service, site_service
from database.models import ApiKey
from database.models.api_key import (
    SCOPE_CATALOG_READ,
    SCOPE_CUSTOMERS_WRITE,
    SCOPE_LEADS_WRITE,
    SCOPE_ORDERS_READ,
    SCOPE_ORDERS_WRITE,
    SCOPE_STOCK_READ,
)
from web.api.deps import client_ip, get_db

router = APIRouter(prefix="/site", tags=["site"])

#: Промахи неизвестным ключом — по адресу: 30 за 10 минут, как у приёма заявок.
_neizvestnye = SlidingWindowLimiter(30, 600, name="site_unknown")
#: Ограничители по ключу: потолок у каждого ключа свой, поэтому экземпляр на
#: (ключ, потолок). Счётчик живёт в Redis под именем, а не в экземпляре.
_po_klyucham: dict[tuple[int, int], SlidingWindowLimiter] = {}
_zamok = threading.Lock()


def _limiter(key: ApiKey) -> SlidingWindowLimiter:
    metka = (key.id, key.rate_per_min)
    with _zamok:
        limiter = _po_klyucham.get(metka)
        if limiter is None:
            limiter = _po_klyucham[metka] = SlidingWindowLimiter(key.rate_per_min, 60, name=f"site_key_{key.id}")
    return limiter


def s_klyuchom(scope: str):
    """Зависимость «запрос по ключу с такой областью»."""

    def dependency(request: Request, db: Session = Depends(get_db)) -> ApiKey:
        ip = client_ip(request)
        raw = request.headers.get(api_key_service.HEADER, "")
        posetitel = tokens.hash_ip(ip)
        if _neizvestnye.is_blocked(posetitel):
            raise errors.RateLimitedError("Too many failed attempts from this address", code="rate_limited")
        try:
            key = api_key_service.authenticate(db, raw, ip)
        except errors.AuthError:
            _neizvestnye.record_failure(posetitel)
            raise
        if _limiter(key).proverit_i_zanyat(str(key.id)):
            # Отказ по потолку — тоже обращение, и владельцу оно нужнее прочих:
            # по нему видно, что потолок мал. Отказ откатит сессию запроса,
            # поэтому счётчик фиксируется здесь, до него.
            api_stats_service.zapisat(db, key, scope, rejected=True)
            db.commit()
            raise errors.RateLimitedError(
                f"Rate limit of {key.rate_per_min} requests per minute exceeded", code="rate_limited"
            )
        api_key_service.require_scope(db, key, scope)
        api_stats_service.zapisat(db, key, scope)
        return key

    dependency.opencrm_api_scope = scope  # type: ignore[attr-defined]
    return dependency


def _s_etag(request: Request, telo: dict) -> Response:
    """ETag поверх ответа — добавка, а не механизм синхронизации (§5)."""
    syroy = json.dumps(telo, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha256(syroy.encode("utf-8")).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(telo, headers={"ETag": etag})


# --- чтение --------------------------------------------------------------------


@router.get("/catalog")
def site_catalog(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=200),
    key: ApiKey = Depends(s_klyuchom(SCOPE_CATALOG_READ)),
    db: Session = Depends(get_db),
):
    return _s_etag(request, site_service.catalog_page(db, key, page, per_page))


@router.get("/catalog/{product_id}")
def site_card(
    product_id: int,
    request: Request,
    key: ApiKey = Depends(s_klyuchom(SCOPE_CATALOG_READ)),
    db: Session = Depends(get_db),
):
    return _s_etag(request, site_service.card(db, key, product_id))


@router.get("/changes")
def site_changes(
    since: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=site_service.MAX_CHANGES, ge=1, le=site_service.MAX_CHANGES),
    key: ApiKey = Depends(s_klyuchom(SCOPE_CATALOG_READ)),
    db: Session = Depends(get_db),
):
    return site_service.changes(db, key, since, limit)


def _spisok_chisel(znachenie: str | None) -> list[int]:
    if not znachenie:
        return []
    try:
        return [int(ch) for ch in znachenie.split(",") if ch.strip()]
    except ValueError:
        raise errors.ValidationError("id must be a comma-separated list of integers", code="bad_id_list") from None


@router.get("/stock")
def site_stock(
    id: str | None = Query(default=None, max_length=2000),
    sku: str | None = Query(default=None, max_length=8000),
    key: ApiKey = Depends(s_klyuchom(SCOPE_STOCK_READ)),
    db: Session = Depends(get_db),
):
    skus = [s.strip() for s in (sku or "").split(",") if s.strip()]
    return site_service.stock(db, key, _spisok_chisel(id), skus)


# --- запись --------------------------------------------------------------------


class OrderLineIn(BaseModel):
    id: int | None = None
    sku: str | None = None
    quantity: str | int


class CustomerIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""


class OrderIn(BaseModel):
    site_ref: str = ""
    reserve_minutes: int | None = None
    comment: str = ""
    customer: CustomerIn | None = None
    items: list[OrderLineIn] = []


@router.post("/orders")
def site_create_order(
    payload: OrderIn,
    key: ApiKey = Depends(s_klyuchom(SCOPE_ORDERS_WRITE)),
    db: Session = Depends(get_db),
):
    """Заказ с сайта. Повтор с тем же `site_ref` — тот же заказ и 200, а не 201."""
    otvet, novyy = site_service.create_order(db, key, payload.model_dump())
    return JSONResponse(otvet, status_code=201 if novyy else 200)


@router.get("/orders/{site_ref}")
def site_order(
    site_ref: str,
    key: ApiKey = Depends(s_klyuchom(SCOPE_ORDERS_READ)),
    db: Session = Depends(get_db),
):
    return site_service.order_for(db, key, site_ref)


@router.post("/orders/{site_ref}/cancel")
def site_cancel_order(
    site_ref: str,
    key: ApiKey = Depends(s_klyuchom(SCOPE_ORDERS_WRITE)),
    db: Session = Depends(get_db),
):
    return site_service.cancel_order(db, key, site_ref)


class RegisterIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    consent: bool = False
    consent_ref: str = ""


@router.post("/customers", status_code=202)
def site_register_customer(
    payload: RegisterIn,
    key: ApiKey = Depends(s_klyuchom(SCOPE_CUSTOMERS_WRITE)),
    db: Session = Depends(get_db),
):
    return site_service.register_customer(db, key, payload.model_dump())


class LeadIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    message: str = ""


@router.post("/leads", status_code=202)
def site_lead(
    payload: LeadIn,
    _key: ApiKey = Depends(s_klyuchom(SCOPE_LEADS_WRITE)),
    db: Session = Depends(get_db),
):
    """Та же заявка, что с формы (`lead_service.receive`), только по ключу сайта.

    Ответ всегда один: ни «завели», ни «узнали своего» снаружи не отличимы —
    иначе форма стала бы способом узнать, ваш ли клиент такой-то человек.
    """
    lead_service.receive(db, payload.model_dump())
    return {"status": "accepted"}
