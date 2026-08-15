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
    MOVE_IN,
    MOVE_KINDS,
    MOVE_OUT,
    MOVE_RETURN,
    MOVE_WRITEOFF,
    QUANTITY_SCALE,
    UNITS,
)
from database.models.warehouse import StockTransfer, Warehouse
from database.repositories import deals as deals_repo
from database.repositories import warehouse as warehouse_repo
from database.repositories import warehouses as places_repo

#: сколько знаков после запятой помещается в выбранный масштаб количества
QUANTITY_DIGITS = 3

#: Потолок количества в тысячных долях: миллиард единиц товара. Смысл тот же,
#: что у `MAX_MONEY` у денег, — это защита не от бизнеса, а от опечатки.
#: Без потолка число вроде «10^30» доходило до вставки и роняло запрос
#: переполнением в драйвере (поймано ещё на файловой базе: «Python int too large
#: to convert to SQLite INTEGER»), то есть пятисоткой на обычный
#: пользовательский ввод. Отказ с внятным кодом лучше пятисотки, а тихо
#: обрезать до максимума нельзя: остаток разойдётся с
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
    announce: bool = True,
) -> tuple[StockMove, bool]:
    """Записывает движение. Возвращает (движение, ушёл ли остаток в минус).

    `announce=False` — движение не объявляет о себе событием `STOCK_WRITTEN_OFF`.
    Нужно ровно там, где движение — не самостоятельное действие человека, а
    часть большего, которое объявит себя само: акт на пять позиций это пять
    движений, и пять строк в ленте вместо одной строки про акт означали бы
    решать за кладовщика, что он имел в виду. Обещание это записано в докстроке
    самого события; здесь — его исполнение. Журнал действий, себестоимость и
    остаток при этом считаются как обычно: молчит только лента.

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
    # Знак задаёт вид движения, а не присланное число. Расход и списание всегда
    # уменьшают остаток, приход и возврат — всегда увеличивают, сколько бы
    # минусов ни прислали. Свободный знак остаётся только у корректировки: она и
    # существует ради «по факту на полке меньше, чем в базе».
    #
    # Раньше приход с минусом проходил как есть и уменьшал остаток. В сумме
    # движений это то же самое, что расход, а в журнале склада — строка
    # «приход −5», то есть запись, врущая о том, что произошло. Разбираться в
    # такой истории через полгода нечем: цифры сходятся, слова не те.
    if kind in (MOVE_OUT, MOVE_WRITEOFF):
        quantity = -abs(quantity)
    elif kind in (MOVE_IN, MOVE_RETURN):
        quantity = abs(quantity)

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

    warehouse = resolve_warehouse(db, data.get("warehouse_id"))

    cost = parse_money(data.get("cost"), "cost")
    if cost is None:
        # Снимок себестоимости на момент движения: цена закупки поменяется, а
        # себестоимость этой заявки должна остаться прежней.
        cost = product.cost_minor

    # Остаток до движения — та самая «старая величина рядом с новой». Спросить
    # его после вставки уже нельзя: он считается суммой движений, и только что
    # записанное в неё войдёт.
    stock_before = warehouse_repo.stock_of(db, product.id, warehouse.id)

    move = StockMove(
        product_id=product.id,
        warehouse_id=warehouse.id,
        # Каким бланком вызвано движение. Без этой ссылки отгрузку заказа нельзя
        # ни назвать, ни отменить точно.
        document_id=data.get("document_id"),
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

    if announce and deal is not None and quantity < 0:
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
    # Остаток считаем ПО СКЛАДУ, а не по товару целиком: «ушло в минус» — это
    # про место, где товара физически не осталось. По сумме всех складов минус
    # мог бы и не наступить, хотя на точке пусто, и предупреждение не пришло бы
    # ровно тогда, когда оно нужно.
    stock_after = warehouse_repo.stock_of(db, product.id, warehouse.id)
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


def shortages(db: Session, rows, warehouse_id: int) -> list[str]:
    """Чего и сколько не хватает на складе. Пусто — списывать можно.

    Строка приходит откуда угодно, лишь бы у неё были `product_id`,
    `quantity_milli` и `name_snapshot`: спрашивают об этом и заказ перед
    отгрузкой, и акт перед проведением, и спрашивают ровно одно и то же. Два
    ответа на один вопрос разошлись бы в формулировке раньше, чем в цифрах, — и
    человек, привыкший к одной, не узнал бы вторую.

    Называем позиции поимённо: отказ «не хватает товара» без списка отправляет
    человека сверять бумагу со складом построчно руками.
    """
    stock = warehouse_repo.stock_by_product(
        db, [row.product_id for row in rows], warehouse_id=warehouse_id
    )
    short = []
    for row in rows:
        have = stock.get(row.product_id, 0)
        if have < row.quantity_milli:
            short.append(
                f"{row.name_snapshot} ({format_quantity(have)}"
                f" из {format_quantity(row.quantity_milli)})"
            )
    return short


def write_off_materials(
    db: Session,
    document,
    rows,
    author: User,
    warehouse_id: int | None = None,
    confirm_negative: bool = False,
    comment: str = "",
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
) -> list[StockMove]:
    """Снять со склада то, что бланк израсходовал. Возвращает движения.

    **Материал от работы отличает карточка товара, а не колонка строки.** Строка
    с услугой и строка без товара вовсе — выполненная работа: остатка у неё нет,
    и проверка нехватки объявила бы «консультации на складе ноль», после чего
    ни один акт с работами не провёлся бы вовсе. Ровно та же оговорка, что у
    отгрузки заказа, и по той же причине.

    **Нехватка останавливает.** У ручного движения принято «разрешаем с
    предупреждением» (деталь поставили сегодня, накладную занесут в пятницу),
    здесь этого мало: акт закрывает работу, и списать под него нечего
    физически. Остановка и явное подтверждение, которое видно в отказе.

    **Себестоимость снимается на строку бланка.** Это снимок момента, а не
    хранимая производная: то же самое число уезжает в само движение
    (`stock_moves.cost_minor`), и по той же причине — закупочная цена завтра
    сменится, а во сколько обошлась ЭТА работа, обязано остаться прежним.
    Хранить его у строки, а не считать по движениям, нужно ради выключаемости:
    склад однажды выключат, его таблицы станут недоступны разделу бланков, и
    акт, лезущий за своей себестоимостью в `stock_moves`, читал бы хранилище
    выключенного блока. Сумма по строкам при этом нигде не хранится — она
    считается (`document_service`), как и положено производному.

    Движения молчаливые (`announce=False`): в ленту заявки идёт одна строка про
    бланк, а не по строке на каждую снятую деталь. Разбор — у самого события.
    """
    materials = []
    for row in rows:
        if row.product_id is None:
            continue
        # Удалённый товар берём тоже: карточку могли убрать из справочника
        # после того, как деталь поставили в работу, а списать её всё равно
        # надо — иначе остаток останется завышенным навсегда.
        product = get_product(db, row.product_id, include_deleted=True)
        if product.is_service:
            continue
        materials.append((row, product))

    if not materials:
        return []

    # Склад выбирается явно, а не подставляется молча: списание с основного
    # однажды снимет деталь не оттуда, где её взяли. Не назвали — основной.
    warehouse = resolve_warehouse(db, warehouse_id)
    if not confirm_negative:
        short = shortages(db, [row for row, _ in materials], warehouse.id)
        if short:
            raise errors.ValidationError(
                "Not enough stock: " + ", ".join(short), code="not_enough_stock"
            )

    moves = []
    for row, product in materials:
        row.cost_minor = product.cost_minor
        move, _ = add_move(
            db,
            {
                "product_id": row.product_id,
                # `writeoff`, а не `out`: расход — это продажа и выдача клиенту,
                # а деталь, поставленная в работу, со склада именно списана.
                # Журнал склада обязан читаться словами, а не только цифрами.
                "kind": MOVE_WRITEOFF,
                "quantity": format_quantity(row.quantity_milli),
                "warehouse_id": warehouse.id,
                "deal_id": document.deal_id,
                "comment": comment,
                "document_id": document.id,
            },
            author,
            source=source,
            source_ref=source_ref,
            announce=False,
        )
        moves.append(move)
    return moves


# --- склады как места ---------------------------------------------------------
#
# Разбор, почему склад живёт на движении, а не на товаре, — в докстроке модели
# `Warehouse`. Здесь только правила, которые нельзя выразить схемой.


MAX_WAREHOUSE_NAME = 200
MAX_WAREHOUSE_CODE = 32


def list_warehouses(db: Session) -> list[Warehouse]:
    return places_repo.list_alive(db)


def warehouse_count(db: Session) -> int:
    """Сколько складов открыто.

    Отвечает и на вопрос интерфейса «показывать ли выбор склада»: **выбор
    появляется, когда складов больше одного**. Не настройкой и не выключателем —
    выводится из данных. Мастерской с одной подсобкой выбор склада в каждой
    форме помеха, а не возможность; завёл второй — выбор появился везде сам.
    """
    return places_repo.count_alive(db)


def seed_defaults(db: Session) -> None:
    """Один склад обязан существовать: без места система не примет ни прихода.

    На боевом сервере его кладёт миграция, здесь — страховка для базы, поднятой
    `create_all` (путь разработки) или почищенной руками. Идемпотентно: есть
    хоть один склад — не делаем ничего, даже если он закрыт... нет, именно
    открытый: закрытый склад приходом не воспользуешься.
    """
    if places_repo.count_alive(db) > 0:
        return
    warehouse = Warehouse(name="Основной")
    db.add(warehouse)
    db.flush()
    places_repo.make_default(db, warehouse)


def get_warehouse(db: Session, warehouse_id: int, include_deleted: bool = False) -> Warehouse:
    warehouse = places_repo.get(db, warehouse_id, include_deleted=include_deleted)
    if warehouse is None:
        raise errors.NotFoundError("Warehouse not found", code="warehouse_not_found")
    return warehouse


def default_warehouse(db: Session) -> Warehouse:
    """Куда попадает приход, если склад не выбрали.

    Основного не оказалось — назначаем самый давний и говорим об этом в базе, а
    не молча подставляем первый попавшийся: без пометки следующий вызов выбрал
    бы, возможно, другой, и приход поехал бы то туда, то сюда.
    """
    warehouse = places_repo.default_warehouse(db)
    if warehouse is not None:
        return warehouse
    fallback = places_repo.oldest_alive(db)
    if fallback is None:
        # Система без склада не может принять ни одного прихода. На боевой базе
        # такого не бывает — склад засевает миграция, — но на пустой тестовой
        # или после ручной чистки бывает, и отказ должен объяснять, что делать.
        raise errors.ValidationError(
            "No warehouse exists — create one first", code="no_warehouse"
        )
    places_repo.make_default(db, fallback)
    return fallback


def resolve_warehouse(db: Session, warehouse_id: int | None) -> Warehouse:
    """Склад из формы или основной, если не выбрали."""
    if warehouse_id is None:
        return default_warehouse(db)
    return get_warehouse(db, warehouse_id)


def create_warehouse(db: Session, data: dict) -> Warehouse:
    warehouse = Warehouse(
        name=_clean_warehouse_name(data.get("name")),
        code=_clean_warehouse_code(db, data.get("code"), warehouse_id=None),
        address=(data.get("address") or "").strip(),
        note=(data.get("note") or "").strip(),
    )
    # Код уникален, и проверка выше не спасает от соседа, который завёл такой же
    # между «проверили» и «вставили». Ловим отказ базы и отвечаем по-человечески.
    warehouse = uniqueness.insert_unique(
        db,
        warehouse,
        taken=lambda new: new.code is not None
        and places_repo.get_by_code(db, new.code) is not None,
        message="This warehouse code is already used",
        code="warehouse_code_taken",
    )
    # Первый склад становится основным сам: иначе приход упёрся бы в «основной
    # не выбран» ровно тогда, когда выбирать не из чего.
    if places_repo.default_warehouse(db) is None:
        places_repo.make_default(db, warehouse)
    if data.get("is_default"):
        places_repo.make_default(db, warehouse)

    return warehouse


def update_warehouse(db: Session, warehouse_id: int, data: dict) -> Warehouse:
    warehouse = get_warehouse(db, warehouse_id)
    if "name" in data:
        warehouse.name = _clean_warehouse_name(data.get("name"))
    if "code" in data:
        warehouse.code = _clean_warehouse_code(db, data.get("code"), warehouse_id=warehouse.id)
    if "address" in data:
        warehouse.address = (data.get("address") or "").strip()
    if "note" in data:
        warehouse.note = (data.get("note") or "").strip()
    db.flush()

    if data.get("is_default"):
        places_repo.make_default(db, warehouse)

    return warehouse


def set_default_warehouse(db: Session, warehouse_id: int) -> Warehouse:
    warehouse = get_warehouse(db, warehouse_id)
    places_repo.make_default(db, warehouse)
    return warehouse


def close_warehouse(db: Session, warehouse_id: int, actor: User) -> None:
    """Закрыть склад. Именно закрыть, а не удалить.

    Три отказа, и каждый закрывает свою дыру:

    - **последний склад** — система без склада не примет ни одного прихода;
    - **склад с остатком** — товар физически лежит, а в системе его нет нигде;
      сначала перемещение, потом закрытие;
    - удалять склад с историей нельзя вовсе, поэтому здесь `deleted_at`, а не
      DELETE: движения старше закрытия, и подписать их будет нечем.
    """
    warehouse = get_warehouse(db, warehouse_id)

    if places_repo.count_alive(db) <= 1:
        raise errors.ValidationError(
            "The last warehouse cannot be closed", code="last_warehouse"
        )

    left = places_repo.nonzero_stock(db, warehouse.id)
    if left:
        # Называем, сколько позиций осталось: отказ без этого отправляет человека
        # искать их глазами по всему справочнику.
        raise errors.ValidationError(
            f"{len(left)} item(s) still on this warehouse — move them first",
            code="warehouse_not_empty",
        )

    warehouse.deleted_at = now_utc()
    warehouse.is_default = False
    db.flush()

    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_WAREHOUSE,
        entity_id=warehouse.id,
        entity_label=warehouse.name,
    )

    # Закрыли основной — назначаем следующий. Оставить систему без основного
    # значит вернуть приход к вопросу «куда», причём незаметно.
    if places_repo.default_warehouse(db) is None:
        replacement = places_repo.oldest_alive(db)
        if replacement is not None:
            places_repo.make_default(db, replacement)


def _clean_warehouse_name(name: str | None) -> str:
    value = (name or "").strip()
    if not value:
        raise errors.ValidationError("Name is required", code="name_required")
    if len(value) > MAX_WAREHOUSE_NAME:
        raise errors.ValidationError("Name is too long", code="name_too_long")
    return value


def _clean_warehouse_code(db: Session, code: str | None, warehouse_id: int | None) -> str | None:
    """Пустой код — это NULL, а не пустая строка: см. комментарий у модели."""
    value = (code or "").strip()
    if not value:
        return None
    if len(value) > MAX_WAREHOUSE_CODE:
        raise errors.ValidationError("Code is too long", code="code_too_long")
    existing = places_repo.get_by_code(db, value)
    if existing is not None and existing.id != warehouse_id:
        raise errors.ConflictError(
            f"Code {value} is already used", code="warehouse_code_taken"
        )
    return value


# --- переезд между складами ---------------------------------------------------


def transfer(db: Session, data: dict, author: User) -> StockTransfer:
    """Перевезти товар с одного склада на другой.

    **Две строки движения в одной транзакции**, связанные шапкой. Почему не одна
    строка с двумя складами — разобрано в докстроке модели `StockTransfer`.

    Отказов здесь больше, чем у обычного движения, и каждый закрывает свою дыру:

    - **переезд на тот же склад** — это не операция, а опечатка, но в истории
      она выглядит как настоящий переезд и сбивает с толку при разборе;
    - **переезд услуги** — остатка у неё нет и быть не может;
    - **не хватает на источнике** — увезти с пустого склада нечего физически.
      У обычного движения минус разрешён (деталь поставили сегодня, накладную
      занесут в пятницу), здесь этого мало: остановка и явное подтверждение,
      которое записывается в комментарий переезда.

    Себестоимость копируется в обе строки. Иначе переезд менял бы себестоимость,
    и заявка, куда деталь спишут потом, обошлась бы дороже или дешевле оттого,
    что коробку переставили с полки на полку.
    """
    product = get_product(db, data["product_id"])
    if product.is_service:
        raise errors.ValidationError("A service has no stock", code="service_has_no_stock")

    source = get_warehouse(db, data["from_warehouse_id"])
    target = get_warehouse(db, data["to_warehouse_id"])
    if source.id == target.id:
        raise errors.ValidationError(
            "Source and destination are the same warehouse", code="same_warehouse"
        )

    quantity = parse_quantity(data.get("quantity"))
    if quantity is None or quantity <= 0:
        # Отрицательный переезд — это переезд в обратную сторону, и записывать
        # его так значит прятать направление в знаке. Пусть меняют склады местами.
        raise errors.ValidationError(
            "Quantity must be greater than zero", code="bad_transfer_quantity"
        )

    # Занимаем товар ДО подсчёта остатка: между «спросили» и «записали» есть
    # окно, и двое, увозящие последнее разом, проходят оба. Разбор — в докстроке
    # `warehouse_repo.zapert_tovar`.
    warehouse_repo.zapert_tovar(db, product.id)
    available = warehouse_repo.stock_of(db, product.id, source.id)
    if quantity > available and not data.get("confirm_negative"):
        raise errors.ValidationError(
            f"Only {format_quantity(available)} left on {source.name}",
            code="not_enough_on_source",
        )

    cost = product.cost_minor
    comment = (data.get("comment") or "").strip()
    if quantity > available:
        # Подтверждение записывается, а не просто пропускает проверку: через
        # месяц вопрос «почему на складе минус» задаст не тот, кто нажимал.
        note = f"перевезено сверх остатка ({format_quantity(available)})"
        comment = f"{comment} · {note}" if comment else note

    happened = to_utc_naive(data.get("happened_at")) or now_utc()
    header = StockTransfer(
        from_warehouse_id=source.id,
        to_warehouse_id=target.id,
        comment=comment,
        happened_at=happened,
        author_id=author.id,
    )
    db.add(header)
    db.flush()

    _transfer_pair(db, header, product, quantity, cost, author, happened)

    audit_service.record(
        db,
        action=audit_service.ACTION_STOCK_TRANSFERRED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_PRODUCT,
        entity_id=product.id,
        entity_label=product.name,
        before=source.name,
        after=f"{target.name} ({format_quantity(quantity)})",
    )
    return header


def revert_transfer(db: Session, transfer_id: int, author: User) -> StockTransfer:
    """Отменить переезд — обратным переездом, а не удалением строк.

    Склад обязан помнить, что уезжало и что вернулось: удали мы две строки, и
    вопрос «куда делись две матрицы» останется без ответа, а остаток при этом
    сойдётся — то есть ошибка станет невидимой.

    Второй раз тот же переезд не отменяется: иначе двойное нажатие увезло бы
    товар обратно дважды.
    """
    header = places_repo.get_transfer(db, transfer_id)
    if header is None:
        raise errors.NotFoundError("Transfer not found", code="transfer_not_found")
    if places_repo.reversal_of(db, header.id) is not None:
        raise errors.ConflictError(
            "This transfer is already reverted", code="transfer_already_reverted"
        )
    if header.reverses_id is not None:
        # Отмена отмены — это обычный переезд, и делать его надо руками: иначе
        # в журнале появится цепочка, в которой не разобраться.
        raise errors.ValidationError(
            "A reverting transfer cannot be reverted", code="transfer_is_reversal"
        )

    happened = now_utc()
    back = StockTransfer(
        from_warehouse_id=header.to_warehouse_id,
        to_warehouse_id=header.from_warehouse_id,
        comment=f"отмена переезда №{header.id}",
        reverses_id=header.id,
        happened_at=happened,
        author_id=author.id,
    )
    db.add(back)
    db.flush()

    for move in places_repo.moves_of_transfer(db, header.id):
        if move.quantity_milli >= 0:
            # Обратный переезд строим по расходной строке: она называет и товар,
            # и количество, и себестоимость на момент отъезда.
            continue
        product = get_product(db, move.product_id)
        _transfer_pair(
            db, back, product, abs(move.quantity_milli), move.cost_minor, author, happened
        )

    audit_service.record(
        db,
        action=audit_service.ACTION_STOCK_TRANSFERRED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_WAREHOUSE,
        entity_id=header.from_warehouse_id,
        entity_label=f"отмена переезда №{header.id}",
    )
    return back


def _transfer_pair(
    db: Session,
    header: StockTransfer,
    product: Product,
    quantity: int,
    cost: int | None,
    author: User,
    happened,
) -> None:
    """Расход с источника и приход на приёмник — обе строки сразу.

    Отдельной функцией, потому что зовут её двое (переезд и его отмена), а
    половина перемещения — это товар, пропавший с одного склада и не появившийся
    на другом. Хуже, чем не перевозить вовсе: остаток врёт, а следов нет.
    """
    db.add(
        StockMove(
            product_id=product.id,
            warehouse_id=header.from_warehouse_id,
            transfer_id=header.id,
            quantity_milli=-quantity,
            kind=MOVE_OUT,
            cost_minor=cost,
            comment=header.comment,
            happened_at=happened,
            author_id=author.id,
        )
    )
    db.add(
        StockMove(
            product_id=product.id,
            warehouse_id=header.to_warehouse_id,
            transfer_id=header.id,
            quantity_milli=quantity,
            kind=MOVE_IN,
            # Тот же снимок себестоимости, что у расхода: переезд не имеет права
            # менять, во сколько товар обошёлся.
            cost_minor=cost,
            comment=header.comment,
            happened_at=happened,
            author_id=author.id,
        )
    )
    db.flush()
