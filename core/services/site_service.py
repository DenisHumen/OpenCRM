"""API сайта магазина: каталог, наличие, лента изменений, заказы, регистрация.

Устройство и решения владельца — `docs/16-api-sayta.md`. Опоры, которые здесь
не пересчитываются заново, а зовутся: наличие — `reserve_service.availability`,
дедупликация клиента — `leads_repo.find_client`, заявка — `lead_service.receive`.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.ratelimit import SlidingWindowLimiter
from core.services import (
    api_key_service,
    audit_service,
    client_service,
    order_service,
    product_photo_service,
    reserve_service,
    settings_service,
    warehouse_service,
)
from core.utils import normalize_phone, now_utc
from database.models import ApiKey, Product, User
from database.models.audit import SOURCE_SITE_API
from database.models.client import SOURCE_SITE
from database.models.document import (
    KIND_SALES_ORDER,
    OPEN_ORDER_STATUSES,
    STATUS_CANCELLED,
    STATUS_DRAFT,
    WAYBILL_KINDS,
)
from database.repositories import documents as documents_repo
from database.repositories import leads as leads_repo
from database.repositories import users as users_repo
from database.repositories import warehouse as warehouse_repo
from database.session import tochka_otkata

#: Лента отдаёт карточки, изменившиеся после курсора, с перекрытием: транзакция,
#: начатая до `as_of` и зафиксированная после, иначе пропадала бы навсегда.
OVERLAP_SEC = 10
MAX_CHANGES = 200
#: Потолок списка `/stock`: больше значит, что сайт выкачивает каталог наличием.
MAX_STOCK_IDS = 200
#: Потолок новых карточек клиентов на ключ в час: ботнет проходит любой
#: ограничитель по адресу, а справочник, где не находят клиента, — не справочник.
MAX_NEW_CUSTOMERS_PER_HOUR = 200
_customers_limiter = SlidingWindowLimiter(MAX_NEW_CUSTOMERS_PER_HOUR, 3600, name="site_customers")

STATE_MANY = "many"
STATE_FEW = "few"
STATE_NONE = "none"
STATE_ALWAYS = "always"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _avtor(db: Session, key: ApiKey) -> User:
    """От чьего имени пишется заказ: тот, кто выдал ключ, а его нет — владелец.

    Бланку нужен автор на бумаге и в истории; у ключа человека нет, а «никто»
    заказ выписать не может. В журнале источник при этом `site_api`.
    """
    avtor = users_repo.get_by_id(db, key.created_by) if key.created_by else None
    if avtor is None:
        avtor = users_repo.get_root(db)
    if avtor is None:
        raise errors.ConflictError("No staff account to act on behalf of", code="no_actor")
    return avtor


def _sklad_id(db: Session, key: ApiKey) -> int | None:
    """Склад ключа или `None` у ключа без склада (услуги и прайс)."""
    sklad = api_key_service.serving_warehouse(db, key)
    return sklad.id if sklad else None


def _obyazatelnyy_sklad(db: Session, key: ApiKey):
    """Наличие и заказ без зала не считаются: у товара без места нет и полки."""
    sklad = api_key_service.serving_warehouse(db, key)
    if sklad is None:
        raise errors.ConflictError("The key names no shop warehouse", code="warehouse_not_serving")
    return sklad


# --- карточка ------------------------------------------------------------------


def _photo_urls(photo) -> dict:
    return {
        "url": f"/media/product/{photo.photo_uid}.webp",
        "thumb_url": f"/media/product/{photo.photo_uid}-thumb.webp",
    }


def cards(db: Session, products: list[Product]) -> list[dict]:
    """Карточки пачкой: снимки и упаковка — по запросу на страницу, не на строку."""
    ids = [p.id for p in products]
    photos = warehouse_repo.all_photos_of_products(db, ids)
    barcodes = warehouse_repo.barcodes_by_products(db, ids)
    itog = []
    for p in products:
        kody = barcodes.get(p.id) or []
        osnovnoy = next((k for k in kody if k.is_primary), kody[0] if kody else None)
        itog.append(
            {
                "id": p.id,
                "sku": p.sku,
                "name": p.name,
                "unit": p.unit,
                "description": p.site_description or "",
                "is_service": bool(p.is_service),
                # Массив с одного дня: скаляр, ставший массивом, ломает всех
                # читателей v1; цены нет — пустой массив, и это «купить нельзя».
                "prices": [] if p.price_minor is None else [{"list": "default", "price_minor": p.price_minor}],
                "pack_size_milli": osnovnoy.pack_size_milli if osnovnoy else 1000,
                "photos": [_photo_urls(f) for f in photos.get(p.id, [])],
                "updated_at": _iso(p.updated_at),
            }
        )
    return itog


def catalog_page(db: Session, key: ApiKey, page: int, per_page: int) -> dict:
    items, total = warehouse_repo.site_catalog(db, _sklad_id(db, key), page=page, per_page=per_page)
    return {
        "items": cards(db, items),
        "total": total,
        "page": page,
        "per_page": per_page,
        "currency": settings_service.get_all(db).get("currency", ""),
    }


def card(db: Session, key: ApiKey, product_id: int) -> dict:
    product = warehouse_repo.site_product(db, _sklad_id(db, key), product_id)
    if product is None:
        raise errors.NotFoundError("Product not found", code="product_not_found")
    otvet = cards(db, [product])[0]
    otvet["currency"] = settings_service.get_all(db).get("currency", "")
    return otvet


# --- наличие -------------------------------------------------------------------


def _state(product: Product, available: int, key: ApiKey) -> str:
    if product.is_service:
        return STATE_ALWAYS
    if available <= 0:
        return STATE_NONE
    if key.stock_mode == "boolean":
        return STATE_MANY
    # Порог «мало»: свой у товара (`min_stock_milli` значит ровно это), иначе
    # порог ключа. Наружу порог не уходит ни в одном виде.
    porog = product.min_stock_milli if product.min_stock_milli else key.few_threshold_milli
    return STATE_FEW if available <= porog else STATE_MANY


def _stock_items(db: Session, key: ApiKey, sklad_id: int | None, products: list[Product]) -> list[dict]:
    tovary = [p.id for p in products if not p.is_service]
    nalichie = reserve_service.availability(db, tovary, warehouse_id=sklad_id) if tovary else {}
    itog = []
    for p in products:
        available = nalichie.get(p.id, {}).get("available_milli", 0)
        stroka = {"id": p.id, "sku": p.sku, "unit": p.unit, "state": _state(p, available, key)}
        # Число — только при `exact` и только у товара: `null` слишком легко
        # читается как ноль, поэтому поля нет вовсе.
        if key.stock_mode == "exact" and not p.is_service:
            stroka["available_milli"] = available
        itog.append(stroka)
    return itog


def _recheck_after(db: Session) -> str | None:
    return _iso(documents_repo.min_reserved_until(db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES))


def stock(db: Session, key: ApiKey, ids: list[int], skus: list[str]) -> dict:
    sklad = _obyazatelnyy_sklad(db, key)
    if len(ids) + len(skus) > MAX_STOCK_IDS:
        raise errors.ValidationError(
            f"At most {MAX_STOCK_IDS} products per request; use /changes for the whole catalogue",
            code="too_many_ids",
        )
    products = warehouse_repo.site_products(db, sklad.id, ids)
    if skus:
        po_sku = warehouse_repo.products_by_skus(db, skus)
        vidimye = warehouse_repo.site_products(db, sklad.id, [p.id for p in po_sku])
        est = {p.id for p in products}
        products += [p for p in vidimye if p.id not in est]
    products.sort(key=lambda p: p.id)
    return {
        "as_of": _iso(now_utc()),
        "ttl_sec": key.ttl_sec,
        "recheck_after": _recheck_after(db),
        "items": _stock_items(db, key, sklad.id, products),
    }


# --- лента изменений -----------------------------------------------------------


def _kursor_v_stroku(*chasti) -> str:
    syroy = "|".join(str(c) for c in chasti)
    return base64.urlsafe_b64encode(syroy.encode("ascii")).decode("ascii").rstrip("=")


def _kursor_iz_stroki(since: str) -> tuple[str, datetime | None, int]:
    """(вид, метка времени, последний id). Вид: `full` — полная выгрузка, `t` — по времени."""
    try:
        dopolnenie = "=" * (-len(since) % 4)
        syroy = base64.urlsafe_b64decode((since + dopolnenie).encode("ascii")).decode("ascii")
        chasti = syroy.split("|")
        vid = chasti[0]
        if vid == "full":
            return vid, None, int(chasti[1])
        if vid == "t":
            metka = datetime.fromisoformat(chasti[1].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            return vid, metka, int(chasti[2]) if len(chasti) > 2 else 0
    except (ValueError, IndexError, binascii.Error, UnicodeDecodeError):
        pass
    raise errors.ValidationError("Bad cursor", code="bad_cursor")


def changes(db: Session, key: ApiKey, since: str | None, limit: int) -> dict:
    sklad_id = _sklad_id(db, key)
    as_of = now_utc().replace(tzinfo=None)
    limit = max(1, min(limit, MAX_CHANGES))
    if since:
        vid, metka, posle_id = _kursor_iz_stroki(since)
    else:
        vid, metka, posle_id = "full", None, 0

    if vid == "full":
        # Холодный старт: весь каталог по возрастанию id, страницами по курсору.
        items, _total = warehouse_repo.site_catalog(db, sklad_id, page=1, per_page=10**9)
        items = [p for p in items if p.id > posle_id]
    else:
        izmenilis = warehouse_repo.site_changed_since(db, sklad_id, metka)
        izmenilis |= documents_repo.tovary_zakazov_s(db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, metka)
        items = sorted(
            (p for p in warehouse_repo.site_products(db, sklad_id, izmenilis) if p.id > posle_id),
            key=lambda p: p.id,
        )
    has_more = len(items) > limit
    stranitsa = items[:limit]
    if has_more:
        next_since = _kursor_v_stroku(vid, metka.isoformat() + "Z" if metka else stranitsa[-1].id, stranitsa[-1].id) if vid == "t" else _kursor_v_stroku("full", stranitsa[-1].id)
    else:
        next_since = _kursor_v_stroku("t", (as_of - timedelta(seconds=OVERLAP_SEC)).isoformat() + "Z")
    karty = cards(db, stranitsa)
    nalichie = {s["id"]: s for s in _stock_items(db, key, sklad_id, stranitsa)}
    for karta in karty:
        karta["stock"] = {k: v for k, v in nalichie[karta["id"]].items() if k not in ("id", "sku", "unit")}
    return {
        "as_of": _iso(as_of),
        "next_since": next_since,
        "has_more": has_more,
        "recheck_after": _recheck_after(db),
        "ttl_sec": key.ttl_sec,
        "items": karty,
    }


# --- заказы --------------------------------------------------------------------


def _razobrat_stroki(db: Session, sklad_id: int, items: list[dict]) -> list[tuple[Product, int]]:
    if not items:
        raise errors.ValidationError("An order needs at least one line", code="lines_required")
    ids = [int(i["id"]) for i in items if i.get("id") is not None]
    skus = [str(i["sku"]) for i in items if i.get("id") is None and i.get("sku")]
    po_id = {p.id: p for p in warehouse_repo.site_products(db, sklad_id, ids)}
    po_sku_vse = {p.sku: p for p in warehouse_repo.products_by_skus(db, skus)}
    vidimye = {p.id for p in warehouse_repo.site_products(db, sklad_id, [p.id for p in po_sku_vse.values()])}
    po_sku = {sku: p for sku, p in po_sku_vse.items() if p.id in vidimye}

    stroki: list[tuple[Product, int]] = []
    ne_naydeny = []
    for item in items:
        product = po_id.get(int(item["id"])) if item.get("id") is not None else po_sku.get(str(item.get("sku") or ""))
        if product is None:
            ne_naydeny.append({"id": item.get("id"), "sku": item.get("sku")})
            continue
        quantity = warehouse_service.parse_quantity(item.get("quantity"))
        if quantity is None or quantity <= 0:
            raise errors.ValidationError("Quantity must be greater than zero", code="bad_line_quantity")
        stroki.append((product, quantity))
    if ne_naydeny:
        raise errors.ValidationError(
            f"Unknown product in {len(ne_naydeny)} line(s)", code="product_unknown",
            details={"items": ne_naydeny},
        )
    for product, _ in stroki:
        if product.price_minor is None:
            # Заказ на товар без цены прошёл бы БЕСПЛАТНЫМ: строка берёт цену
            # из карточки как есть, а сумма считает `price_minor or 0`.
            raise errors.ValidationError(
                f"Price is not set for '{product.name}'", code="price_not_set",
                details={"items": [{"id": product.id, "sku": product.sku}]},
            )
    return stroki


def _proverit_nalichie(db: Session, sklad_id: int, stroki: list[tuple[Product, int]]) -> None:
    """Счёт ПО ТОВАРУ, а не по строке: две строки по 5 при остатке 6 иначе проходят обе."""
    nuzhno: dict[int, int] = {}
    tovary: dict[int, Product] = {}
    for product, quantity in stroki:
        if product.is_service:
            continue
        nuzhno[product.id] = nuzhno.get(product.id, 0) + quantity
        tovary[product.id] = product
    if not nuzhno:
        return
    # Замки по возрастанию id — тот же порядок, что у проведения заказа: иначе
    # два заказа на одну пару товаров ждут друг друга насмерть.
    for product_id in sorted(nuzhno):
        warehouse_repo.zapert_tovar(db, product_id)
    nalichie = reserve_service.availability(db, sorted(nuzhno), warehouse_id=sklad_id)
    ne_hvataet = []
    for product_id, skolko in sorted(nuzhno.items()):
        available = nalichie.get(product_id, {}).get("available_milli", 0)
        if skolko > available:
            ne_hvataet.append(
                {
                    "id": product_id,
                    "sku": tovary[product_id].sku,
                    "requested_milli": skolko,
                    "available_milli": max(available, 0),
                }
            )
    if ne_hvataet:
        # 409, а не 422 (решение §15 п. 2): дело не в форме запроса, а в
        # состоянии склада, и сайт обязан предложить меньшее количество.
        raise errors.ConflictError(
            f"Not enough stock for {len(ne_hvataet)} item(s)", code="not_enough_stock",
            details={"items": ne_hvataet},
        )


def _srok_broni(key: ApiKey, payload: dict) -> datetime:
    minuty = payload.get("reserve_minutes")
    if minuty is None:
        minuty = key.max_reserve_minutes
    try:
        minuty = int(minuty)
    except (TypeError, ValueError):
        raise errors.ValidationError("reserve_minutes must be an integer", code="bad_reserve") from None
    if minuty < 1:
        raise errors.ValidationError("reserve_minutes must be at least 1", code="bad_reserve")
    if minuty > key.max_reserve_minutes:
        raise errors.ValidationError(
            f"reserve_minutes exceeds the key's limit of {key.max_reserve_minutes}",
            code="reserve_too_long",
        )
    return now_utc().replace(tzinfo=None) + timedelta(minutes=minuty)


def order_out(db: Session, order) -> dict:
    lines = documents_repo.lines_of(db, order.id)
    products = {p.id: p for p in warehouse_repo.products_by_ids(db, [l.product_id for l in lines if l.product_id], include_deleted=True)}
    # Черновик по заказу заводится сам (docs/21) и товара не двигает: сайту
    # называем только бумаги, по которым товар и вправду уехал.
    nakladnye = [
        d.number for d in documents_repo.po_osnovaniyu(db, order.id)
        if d.kind in WAYBILL_KINDS and d.status not in (STATUS_CANCELLED, STATUS_DRAFT)
    ]
    now = now_utc().replace(tzinfo=None)
    return {
        "site_ref": order.site_ref,
        "number": order.number,
        "status": order.status,
        "reserved_until": _iso(order.reserved_until),
        "reserve_expired": bool(
            order.reserved_until and order.reserved_until <= now and order.status in OPEN_ORDER_STATUSES
        ),
        "total_minor": order_service.total_minor(lines),
        "currency": settings_service.get_all(db).get("currency", ""),
        "waybills": nakladnye,
        "lines": [
            {
                "id": line.product_id,
                "sku": products[line.product_id].sku if line.product_id in products else None,
                "name": line.name_snapshot,
                "quantity_milli": line.quantity_milli,
                "price_minor": line.price_minor,
            }
            for line in lines
        ],
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
    }


def _svoy_zakaz(db: Session, key: ApiKey, site_ref: str):
    order = documents_repo.get_by_site_ref(db, site_ref)
    if order is None or order.api_key_id != key.id:
        # Чужой заказ выглядит как несуществующий: «есть, но не ваш» подтвердил
        # бы, что чужой `site_ref` занят.
        raise errors.NotFoundError("Order not found", code="order_not_found")
    return order


def create_order(db: Session, key: ApiKey, payload: dict) -> tuple[dict, bool]:
    """Заказ с сайта. Повтор с тем же `site_ref` возвращает тот же заказ (False)."""
    site_ref = str(payload.get("site_ref") or "").strip()
    if not site_ref:
        raise errors.ValidationError("site_ref is required", code="site_ref_required")
    if len(site_ref) > 64:
        raise errors.ValidationError("site_ref is too long (max 64)", code="site_ref_too_long")
    sklad = _obyazatelnyy_sklad(db, key)
    byl = documents_repo.get_by_site_ref(db, site_ref)
    if byl is not None:
        return order_out(db, _svoy_zakaz(db, key, site_ref)), False

    stroki = _razobrat_stroki(db, sklad.id, list(payload.get("items") or []))
    srok = _srok_broni(key, payload)
    _proverit_nalichie(db, sklad.id, stroki)
    # Под замками — ещё раз: сосед с тем же `site_ref` мог зафиксироваться, пока
    # мы ждали, и его бронь уже вычтена из «доступно».
    byl = documents_repo.get_by_site_ref(db, site_ref)
    if byl is not None:
        return order_out(db, _svoy_zakaz(db, key, site_ref)), False

    avtor = _avtor(db, key)
    dannye = {
        "kind": KIND_SALES_ORDER,
        "client_name": str((payload.get("customer") or {}).get("name") or "").strip() or None,
        "comment": str(payload.get("comment") or "").strip()[:500],
    }
    klient = _naiti_klienta(db, payload.get("customer") or {})
    if klient is not None:
        dannye["client_id"] = klient.id
    try:
        with tochka_otkata(db):
            order, _novyy_klient = order_service.create(db, dannye, avtor)
            order.site_ref = site_ref
            order.api_key_id = key.id
            order.reserved_until = srok
            db.flush()
            for product, quantity in stroki:
                order_service.add_line(
                    db,
                    order.id,
                    {"product_id": product.id, "quantity": warehouse_service.format_quantity(quantity)},
                    avtor,
                )
    except IntegrityError:
        # Уникальность нарушена по `site_ref`: повтор доставки проиграл гонку
        # победителю — отдаём его заказ. Иначе — не наша беда, наверх.
        byl = documents_repo.get_by_site_ref(db, site_ref)
        if byl is None:
            raise
        return order_out(db, _svoy_zakaz(db, key, site_ref)), False
    return order_out(db, order), True


def _naiti_klienta(db: Session, customer: dict):
    """Покупатель по почте и номеру — тем же поиском, что у формы. Не находится — заказ без карточки."""
    email = str(customer.get("email") or "").strip().lower()
    phone = str(customer.get("phone") or "").strip()
    if not email and not phone:
        return None
    strana = settings_service.get_all(db).get("default_country_code", "")
    phone_norm = normalize_phone(phone, strana)[:32] if phone else ""
    return leads_repo.find_client(db, email, phone_norm)


def order_for(db: Session, key: ApiKey, site_ref: str) -> dict:
    return order_out(db, _svoy_zakaz(db, key, site_ref))


def cancel_order(db: Session, key: ApiKey, site_ref: str) -> dict:
    order = _svoy_zakaz(db, key, site_ref)
    if order.status == STATUS_CANCELLED:
        return order_out(db, order)
    nakladnye = [
        d for d in documents_repo.po_osnovaniyu(db, order.id)
        if d.kind in WAYBILL_KINDS and d.status not in (STATUS_CANCELLED, STATUS_DRAFT)
    ]
    if order.status not in OPEN_ORDER_STATUSES or nakladnye:
        # Товар физически ушёл: дальше это возврат, а не отмена, и через сайт
        # он не делается.
        raise errors.ConflictError(
            "The order is already fulfilled; a return is handled in the CRM",
            code="order_already_fulfilled",
        )
    order_service.cancel(db, order.id, _avtor(db, key), note="Cancelled by the site")
    return order_out(db, documents_repo.get(db, order.id))


# --- регистрация клиента -------------------------------------------------------


def register_customer(db: Session, key: ApiKey, payload: dict) -> dict:
    """Завести карточку или узнать свою. Ответ одной формы в обоих случаях."""
    if not payload.get("consent"):
        raise errors.ValidationError("consent is required", code="consent_required")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    email = str(payload.get("email") or "").strip().lower()[:255]
    phone = str(payload.get("phone") or "").strip()[:64]
    if not email and not phone:
        raise errors.ValidationError(
            "An email address or a phone number is required", code="contact_required"
        )
    strana = settings_service.get_all(db).get("default_country_code", "")
    phone_norm = normalize_phone(phone, strana)[:32] if phone else ""
    consent_ref = str(payload.get("consent_ref") or "").strip()[:120]
    avtor = _avtor(db, key)

    klient = leads_repo.find_client(db, email, phone_norm)
    if klient is not None:
        # Известную карточку регистрация НЕ правит: знание чужого номера
        # превратилось бы в способ переписать в чужой карточке почту.
        client_service.add_note(
            db, klient.id, avtor, "note",
            f"Registered on the site as '{name}'"
            + (f", email {email}" if email else "")
            + (f", phone {phone}" if phone else ""),
        )
    else:
        if _customers_limiter.proverit_i_zanyat(str(key.id)):
            raise errors.RateLimitedError(
                "Too many new customers from this key", code="customers_flooded"
            )
        klient = client_service.create_client(
            db, {"name": name[:200], "email": email, "phone": phone, "source": SOURCE_SITE}, avtor
        )
    audit_service.record(
        db,
        actor=None,
        source=SOURCE_SITE_API,
        action=audit_service.ACTION_CUSTOMER_REGISTERED,
        entity_type=audit_service.ENTITY_CLIENT,
        entity_id=klient.id,
        entity_label=klient.name,
        source_ref=consent_ref,
        after=f"key {key.name}",
    )
    return {"customer_ref": api_key_service.customer_ref(key, klient.id), "status": "accepted"}


# --- снимок товара без ключа ---------------------------------------------------


def photo_path(db: Session, filename: str):
    """Путь к снимку по имени `<uid>.webp` / `<uid>-thumb.webp`; нет — `None`.

    Товар обязан быть опубликован хоть на одном магазинном складе: неопубликованный
    выглядит как несуществующий — «есть, но не покажем» подтвердило бы, что он есть.
    """
    if not filename.endswith(".webp") or "/" in filename or "\\" in filename:
        return None
    imya = filename[: -len(".webp")]
    thumb = imya.endswith("-thumb")
    uid = imya[: -len("-thumb")] if thumb else imya
    if not uid.isalnum():
        return None
    photo = warehouse_repo.photo_by_uid(db, uid)
    if photo is None:
        return None
    product = warehouse_repo.get_product(db, photo.product_id)
    if product is None:
        return None
    if not product.is_service and not _opublikovan_gde_to(db, product.id):
        return None
    put = product_photo_service.put_na_diske(photo, "thumb" if thumb else "view")
    return put if put.is_file() else None


def _opublikovan_gde_to(db: Session, product_id: int) -> bool:
    from database.models.warehouse import WH_SHOP
    from database.repositories import warehouses as places_repo

    for sklad in places_repo.list_alive(db):
        if sklad.kind == WH_SHOP and warehouse_repo.site_product(db, sklad.id, product_id) is not None:
            return True
    return False


def site_summary(db: Session, warehouse_id: int) -> dict:
    return warehouse_repo.site_summary(db, warehouse_id)
