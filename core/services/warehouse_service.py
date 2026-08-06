"""Склад: товары, движения, остатки.

Правила блока, которые легко нарушить и тяжело потом починить:

1. Остаток нигде не хранится — он равен сумме движений (см. репозиторий).
2. Деньги — целые в минорных единицах, количество — целое в тысячных долях
   единицы. Ни то, ни другое не проходит через float ни на секунду.
3. Движение не редактируется и не удаляется: ошибку исправляют обратным
   движением. История склада — это и есть склад; правка задним числом означала
   бы, что остаток на прошлую пятницу зависит от того, когда его спросили.
"""

from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from core import events
from core import exceptions as errors
from core import uniqueness
from core.services import audit_service
from core.services.deal_service import parse_money
from core.utils import now_utc, to_utc_naive
from database.models import Product, StockMove, User
from database.models.audit import SOURCE_MANUAL
from database.models.warehouse import (
    MOVE_KINDS,
    MOVE_OUT,
    MOVE_WRITEOFF,
    QUANTITY_SCALE,
    UNITS,
)
from database.repositories import deals as deals_repo
from database.repositories import warehouse as warehouse_repo

#: сколько знаков после запятой помещается в выбранный масштаб количества
QUANTITY_DIGITS = 3

#: Потолок количества в тысячных долях: миллиард единиц товара. Смысл тот же,
#: что у `MAX_MONEY` у денег, — это защита не от бизнеса, а от опечатки.
#: Без потолка число вроде «10^30» доходило до вставки и роняло запрос
#: OverflowError'ом («Python int too large to convert to SQLite INTEGER»), то
#: есть пятисоткой на обычный пользовательский ввод. Отказ с внятным кодом
#: лучше пятисотки, а тихо обрезать до максимума нельзя: остаток разойдётся с
#: накладной ровно так же, как от молчаливого округления дробей.
MAX_QUANTITY = 10**12

#: Со склада ушло под заявку. Подробности: `move`, `product`, `deal`.
#:
#: Объявляется на движение, а не на партию: партии в этом складе нет. Движение
#: заводится по одному (`POST /warehouse/moves`), и одно движение — это ровно
#: одно действие человека. Разбивать по строкам позиции тут попросту нечего, а
#: склеивать соседние движения в одну строку ленты значило бы решать за
#: кладовщика, что он имел в виду, — и хранить в ленте то, чего в базе нет.
#: Появится акт на несколько позиций — событие поднимется от акта, и в ленте
#: станет одна строка на акт; подписчик при этом не изменится.
#:
#: Приход и возврат под заявку событием не объявляются. Со склада под заявку
#: ВЗЯЛИ — это работа по заявке; положили обратно — это поправка учёта, и она
#: видна во врезке себестоимости прямо под лентой. Пускать в ленту оба
#: направления значит превратить её в журнал склада.
STOCK_WRITTEN_OFF = "stock.written_off"


def parse_quantity(value: str | int | float | None) -> int | None:
    """Человеческий ввод количества → целое в тысячных долях единицы.

    Почему количество разбирает сервер, хотя деньги приходят уже целыми
    (`deal_service.parse_money`): у денег два знака и умножение на 100 в браузере
    безобидно, а у количества три, и `Math.round(0.3335 * 1000)` на фронте даст
    334 — то самое молчаливое округление, ради защиты от которого всё и считается
    целыми. Пусть точность живёт там, где её можно гарантировать.

    Лишние знаки не округляем, а отвергаем. Округли сервер 0.3335 кг до 0.334 —
    остаток разойдётся с накладной на грамм, а после нескольких таких операций на
    складе заведётся «свободный» товар, которого нет, и его спишут второй раз.

    float на входе принимаем, но тут же приводим к строке: Decimal(0.1) — это
    0.1000000000000000055511151231257827, и в тысячных такое даёт мусор.
    """
    if value is None:
        return None
    if isinstance(value, str):
        # Запятая как разделитель и пробелы-разряды («1 250,5») — обычный способ
        # набора в русской раскладке, а неразрывный пробел приезжает копипастом
        # из прайса. Ловить оператора на том, КАК он набрал, вместо того, что он
        # имел в виду, — плохая сделка.
        text = value.strip().replace(",", ".").replace(" ", "").replace(" ", "")
        if not text:
            return None
    else:
        text = str(value)
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise errors.ValidationError(
            f"Not a number: {value!r}", code="bad_quantity"
        ) from exc
    if not number.is_finite():
        raise errors.ValidationError(f"Not a number: {value!r}", code="bad_quantity")
    scaled = number * QUANTITY_SCALE
    # Потолок проверяем ДО перевода в целое, и на Decimal, а не на int:
    # у Decimal экспонента хранится отдельно, поэтому сравнение дёшево при любом
    # порядке, а `int()` от 1e100000 строит число из ста тысяч цифр и падает
    # ValueError'ом мимо нашей обработки.
    if abs(scaled) > MAX_QUANTITY:
        raise errors.ValidationError("Quantity is too large", code="quantity_too_large")
    if scaled != scaled.to_integral_value():
        raise errors.ValidationError(
            f"Too many decimal places (max {QUANTITY_DIGITS})", code="quantity_too_precise"
        )
    return int(scaled)


def format_quantity(milli: int) -> str:
    """Тысячные → строка без лишних нулей: 1500 → «1.5», 2000 → «2»."""
    sign = "-" if milli < 0 else ""
    whole, frac = divmod(abs(milli), QUANTITY_SCALE)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:03d}".rstrip("0")


# --- товары ---

def get_product(db: Session, product_id: int, include_deleted: bool = False) -> Product:
    product = warehouse_repo.get_product(db, product_id, include_deleted=include_deleted)
    if product is None:
        raise errors.NotFoundError("Product not found", code="product_not_found")
    return product


def create_product(db: Session, data: dict) -> Product:
    name = (data.get("name") or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    product = Product(
        name=name,
        sku=_clean_sku(db, data.get("sku"), product_id=None),
        unit=_clean_unit(data.get("unit")),
        price_minor=parse_money(data.get("price"), "price"),
        cost_minor=parse_money(data.get("cost"), "cost"),
        is_service=bool(data.get("is_service")),
        min_stock_milli=parse_quantity(data.get("min_stock")),
        note=(data.get("note") or "").strip(),
    )
    if product.is_service and product.min_stock_milli is not None:
        # У услуги нет остатка, а значит и порога предупреждения быть не может:
        # иначе экран склада вечно ругался бы на «нехватку» консультации.
        raise errors.ValidationError(
            "A service has no stock threshold", code="service_has_no_stock"
        )
    # Артикул уникален, и проверка в `_clean_sku` не спасает от соседа, который
    # заводит ту же позицию в ту же секунду: приёмка товара — как раз то место,
    # где двое работают с одной накладной.
    return uniqueness.insert_unique(
        db,
        product,
        taken=lambda row: row.sku is not None
        and warehouse_repo.get_by_sku(db, row.sku) is not None,
        message=f"SKU {product.sku} is already used",
        code="sku_taken",
    )


def update_product(db: Session, product_id: int, data: dict) -> Product:
    product = get_product(db, product_id)
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name:
            raise errors.ValidationError("Name is required", code="name_required")
        product.name = name
    if "sku" in data:
        product.sku = _clean_sku(db, data["sku"], product_id=product.id)
    if "unit" in data and data["unit"] is not None:
        product.unit = _clean_unit(data["unit"])
    for field, column in (("price", "price_minor"), ("cost", "cost_minor")):
        if field in data:
            setattr(product, column, parse_money(data[field], field))
    if "min_stock" in data:
        product.min_stock_milli = parse_quantity(data["min_stock"])
    if "note" in data and data["note"] is not None:
        product.note = data["note"].strip()
    if "is_service" in data and data["is_service"] is not None:
        # Превратить товар с историей движений в услугу нельзя: остаток у него
        # уже есть, а у услуги остатка не бывает — получилась бы позиция, у
        # которой одновременно «нет остатка» и лежит 12 штук на полке.
        if data["is_service"] and warehouse_repo.stock_of(db, product.id) != 0:
            raise errors.ValidationError(
                "Product has stock and cannot become a service", code="product_has_stock"
            )
        product.is_service = bool(data["is_service"])
    if product.is_service:
        product.min_stock_milli = None
    product.updated_at = now_utc()
    return product


def delete_product(
    db: Session,
    product_id: int,
    actor: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> None:
    """Мягкое удаление: карточка уходит из списков, движения остаются на месте."""
    product = get_product(db, product_id)
    product.deleted_at = now_utc()
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_PRODUCT,
        entity_id=product.id,
        entity_label=product.name,
    )


def restore_product(
    db: Session,
    product_id: int,
    actor: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> Product:
    """Вернуть товар из корзины. В журнал — наравне с удалением (см. клиентов)."""
    product = get_product(db, product_id, include_deleted=True)
    if product.deleted_at is None:
        return product
    product.deleted_at = None
    db.flush()
    audit_service.record_restore(
        db,
        actor=actor,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_PRODUCT,
        entity_id=product.id,
        entity_label=product.name,
    )
    return product


def _clean_unit(unit: str | None) -> str:
    value = (unit or "pcs").strip()
    if value not in UNITS:
        raise errors.ValidationError(f"unit must be one of {UNITS}", code="bad_unit")
    return value


def _clean_sku(db: Session, sku: str | None, product_id: int | None) -> str | None:
    """Пустой артикул — это NULL, а не пустая строка: см. комментарий у модели."""
    value = (sku or "").strip()
    if not value:
        return None
    existing = warehouse_repo.get_by_sku(db, value)
    if existing is not None and existing.id != product_id:
        raise errors.ConflictError(f"SKU {value} is already used", code="sku_taken")
    return value


# --- движения ---

def stock_of(db: Session, product: Product) -> int | None:
    """Остаток товара. У услуги остатка нет — именно None, а не 0."""
    if product.is_service:
        return None
    return warehouse_repo.stock_of(db, product.id)


def is_low(product: Product, stock_milli: int | None) -> bool:
    if stock_milli is None or product.min_stock_milli is None:
        return False
    return stock_milli <= product.min_stock_milli


def add_move(
    db: Session,
    data: dict,
    author: User,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> tuple[StockMove, bool]:
    """Записывает движение. Возвращает (движение, ушёл ли остаток в минус).

    **Уход в минус разрешён, но помечается.** Запрет выглядит правильнее, но
    ломает жизнь: в мастерской деталь ставят в машину сегодня, а накладную на
    неё заносят в пятницу. Запрет заставил бы кладовщика либо не записывать
    расход вовсе (и склад разойдётся с реальностью навсегда), либо задним числом
    выдумывать приход (и себестоимость станет фальшивой). Обе лжи хуже честного
    «−3 шт», которое видно на экране красным и требует найти потерянный приход.

    Поэтому склад не сторож, а зеркало: он показывает минус, а не отказывается
    его показать.
    """
    product = get_product(db, data["product_id"])
    if product.is_service:
        # Услугу нельзя оприходовать и нельзя списать: остатка у неё нет.
        raise errors.ValidationError(
            "A service has no stock", code="service_has_no_stock"
        )

    kind = (data.get("kind") or "").strip()
    if kind not in MOVE_KINDS:
        raise errors.ValidationError(f"kind must be one of {MOVE_KINDS}", code="bad_move_kind")

    quantity = parse_quantity(data.get("quantity"))
    if quantity is None or quantity == 0:
        # Нулевое движение ничего не меняет, но засоряет историю и выглядит как
        # выполненная операция — отказываем, чтобы не пришлось потом гадать.
        raise errors.ValidationError("Quantity must not be zero", code="zero_quantity")
    # Знак задаёт вид движения там, где он однозначен: расход и списание всегда
    # уменьшают остаток, сколько бы плюсов ни прислал клиент. У корректировки,
    # прихода и возврата знак берётся как есть — корректировка бывает в минус.
    if kind in (MOVE_OUT, MOVE_WRITEOFF):
        quantity = -abs(quantity)

    deal_id = data.get("deal_id")
    # Заявку держим, а не выбрасываем после проверки: она же поедет в событие, и
    # второй раз ходить за ней в базу незачем.
    deal = deals_repo.get(db, deal_id) if deal_id is not None else None
    if deal_id is not None and deal is None:
        # Проверяем до вставки: внешний ключ поймал бы несуществующую заявку и
        # сам, но ответом был бы 500 про нарушение ограничения вместо «такой
        # заявки нет». Удалённая заявка тоже не годится — списывать под неё
        # нечего, а движение потом некуда было бы показать.
        raise errors.NotFoundError("Deal not found", code="deal_not_found")

    cost = parse_money(data.get("cost"), "cost")
    if cost is None:
        # Снимок себестоимости на момент движения: цена закупки поменяется, а
        # себестоимость этой заявки должна остаться прежней.
        cost = product.cost_minor

    # Остаток до движения — та самая «старая величина рядом с новой». Спросить
    # его после вставки уже нельзя: он считается суммой движений, и только что
    # записанное в неё войдёт.
    stock_before = warehouse_repo.stock_of(db, product.id)

    move = StockMove(
        product_id=product.id,
        quantity_milli=quantity,
        kind=kind,
        deal_id=deal_id,
        cost_minor=cost,
        comment=(data.get("comment") or "").strip(),
        happened_at=to_utc_naive(data.get("happened_at")) or now_utc(),
        author_id=author.id,
    )
    db.add(move)
    db.flush()

    if deal is not None and quantity < 0:
        events.emit(
            STOCK_WRITTEN_OFF,
            db=db,
            actor=author,
            # Приписка кладовщика и есть причина: «поставили в машину» объясняет
            # расход лучше, чем вид движения. Не написал — берём вид: брак и
            # выдача под работу в ленте выглядят по-разному, и путать их нельзя.
            reason=(
                move.comment
                or ("written off as spoiled" if kind == MOVE_WRITEOFF else "used on the job")
            ),
            move=move,
            product=product,
            deal=deal,
        )

    # Остаток спрашиваем у базы уже после flush — она и складывает движения,
    # включая только что записанное. Никакого «старый остаток плюс дельта»:
    # именно так расходятся хранимые остатки.
    stock_after = warehouse_repo.stock_of(db, product.id)
    went_negative = quantity < 0 and stock_after < 0

    # «Кто списал две матрицы» — тот самый вопрос, ради которого журнал заведён.
    # Исполнитель у движения есть и без журнала (`stock_moves.author_id`), но
    # ответа он не даёт: по нему не отличить «списал руками» от «списалось,
    # потому что провёл акт», а разбираться будут именно в этом.
    audit_service.record(
        db,
        action=audit_service.ACTION_STOCK_MOVE_ADDED,
        actor=author,
        source=source,
        source_ref=source_ref,
        entity_type=audit_service.ENTITY_PRODUCT,
        entity_id=product.id,
        entity_label=product.name,
        before=format_quantity(stock_before),
        after=format_quantity(stock_after),
    )
    return move, went_negative
