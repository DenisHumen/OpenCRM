"""Запросы склада.

Здесь живёт главное правило блока: **остаток считается запросом**.

Соблазн загрузить движения товара и сложить их в Python велик, но он ломается
ровно там, где это дороже всего: список товаров показывает первые 50 движений, а
их 3000 — и остаток тихо оказывается неверным, причём выглядит правдоподобно.
Поэтому суммирует всегда база: `SUM(quantity_milli) GROUP BY product_id` не
зависит ни от пагинации, ни от того, что успело попасть в сессию.
"""

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import Session

from database.models import Product, ProductBarcode, ProductPhoto, StockMove
from database.models.warehouse import QUANTITY_SCALE
from database.query import as_int, contains, page_of


def list_product_photos(db: Session, product_id: int) -> list[ProductPhoto]:
    """Снимки товара в заданном человеком порядке.

    `id` вторым ключом сортировки, а не для красоты: у снимков, загруженных
    подряд, `sort_order` совпадает до первой перестановки, и без второго ключа
    порядок выдачи зависел бы от плана запроса — то есть менялся бы сам.
    """
    return list(
        db.scalars(
            select(ProductPhoto)
            .where(ProductPhoto.product_id == product_id)
            .order_by(ProductPhoto.sort_order.asc(), ProductPhoto.id.asc())
        )
    )


def get_product_photo(db: Session, product_id: int, photo_id: int) -> ProductPhoto | None:
    """Снимок ЭТОГО товара. Товар в условии обязателен, а не для удобства:
    иначе чужой номер снимка отдавал бы чужую картинку по прямому адресу."""
    return db.scalar(
        select(ProductPhoto).where(
            ProductPhoto.id == photo_id, ProductPhoto.product_id == product_id
        )
    )


def next_photo_order(db: Session, product_id: int) -> int:
    """Место в конце списка. Считается запросом — хранимого счётчика нет."""
    posledniy = db.scalar(
        select(func.max(ProductPhoto.sort_order)).where(ProductPhoto.product_id == product_id)
    )
    return 0 if posledniy is None else int(posledniy) + 1


def photos_of_products(db: Session, product_ids) -> dict[int, ProductPhoto]:
    """Первый снимок каждого товара — для списка, где место есть под один.

    Одним запросом на страницу, а не по запросу на строку: список товаров
    показывает полсотни позиций, и полсотни запросов за картинками — это ровно
    та беда, от которой в проекте отдельный сторож
    (`test_raskladka_stoit_odin_zapros_na_stranitsu`).
    """
    product_ids = [i for i in set(product_ids) if i]
    if not product_ids:
        return {}
    rows = db.scalars(
        select(ProductPhoto)
        .where(ProductPhoto.product_id.in_(product_ids))
        .order_by(ProductPhoto.sort_order.asc(), ProductPhoto.id.asc())
    )
    pervye: dict[int, ProductPhoto] = {}
    for row in rows:
        pervye.setdefault(row.product_id, row)
    return pervye


def get_product(db: Session, product_id: int, include_deleted: bool = False) -> Product | None:
    product = db.get(Product, product_id)
    if product is None:
        return None
    if product.deleted_at is not None and not include_deleted:
        return None
    return product


def get_by_sku(db: Session, sku: str) -> Product | None:
    """Ищет и среди удалённых: артикул уникален по всей таблице, включая корзину."""
    return db.scalar(select(Product).where(Product.sku == sku))


def max_vydannogo_sku(db: Session, prefix: str, digits: int) -> int:
    """Наибольший номер среди артикулов, выданных нами. 0 — таких ещё нет.

    Чужой артикул счётчик не двигает: `ZX-9` выданным не считается, и следующий
    наш номер не обязан быть больше него. Удалённые товары считаются: артикул
    остаётся занятым уникальным индексом, а этикетка — наклеенной на коробке.

    Отбор диапазоном, а не по образцу: сравнение здесь побайтное
    (`ExactString`), поэтому всё, что стоит между `A-000000` и `A-999999`, —
    это либо наш номер, либо редкая ручная подделка под него. Пятидесяти строк
    сверху хватает, чтобы пройти такие подделки насквозь и не тянуть в память
    весь склад.
    """
    nizhniy, verhniy = prefix + "0" * digits, prefix + "9" * digits
    verhnie = db.scalars(
        select(Product.sku)
        .where(Product.sku >= nizhniy, Product.sku <= verhniy)
        .order_by(Product.sku.desc())
        .limit(50)
    )
    for sku in verhnie:
        hvost = sku[len(prefix):]
        if len(hvost) == digits and hvost.isdigit():
            return int(hvost)
    return 0


def search_products(
    db: Session,
    q: str | None = None,
    include_services: bool = True,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Product], int]:
    stmt = select(Product).where(Product.deleted_at.is_(None))
    if q:
        needle = q.strip()
        stmt = stmt.where(or_(contains(Product.name, needle), contains(Product.sku, needle)))
    if not include_services:
        stmt = stmt.where(Product.is_service.is_(False))
    stmt = stmt.order_by(Product.name)
    return page_of(db, stmt, page=page, per_page=per_page)


def names_of(db: Session, product_ids: set[int]) -> dict[int, tuple[str, str]]:
    """{id: (название, единица)} — для строк истории и врезки в карточке заявки."""
    if not product_ids:
        return {}
    rows = db.execute(
        select(Product.id, Product.name, Product.unit).where(Product.id.in_(product_ids))
    ).all()
    return {product_id: (name, unit) for product_id, name, unit in rows}


def zapert_tovar(db: Session, product_id: int) -> None:
    """Занять товар до конца транзакции: остальные ждут своей очереди.

    **Зачем это нужно.** Остаток не хранится — он равен `SUM(quantity_milli)` и
    считается запросом. Значит у любой проверки «хватает ли» два шага: спросить
    остаток и записать движение. Между ними есть окно, и в него попадают двое.

    Замерено дуэлью: на складе ровно две единицы, двое увозят по две разом.
    Оба спрашивают до чужой записи, оба видят «хватает», оба пишут — со склада
    уходит четыре из двух. Хуже самого минуса то, что ни одна перевозка не
    помечена «перевезено сверх остатка»: обычному движению минус разрешён
    нарочно (деталь поставили сегодня, накладную занесут в пятницу), и отличает
    законный минус от незаконного как раз эта запись. Через месяц на вопрос
    «почему на складе минус» ответить нечем.

    **Замок на строку ТОВАРА, а не на движения.** Запереть `stock_moves` по
    (товар, склад) нельзя: `SELECT ... FOR UPDATE` берёт существующие строки, а
    спор идёт о НОВОЙ — и при `READ COMMITTED` промежуточных замков нет, так что
    соседняя вставка проходит мимо. Строка товара, наоборот, одна и та же для
    обоих, и через неё оба обязаны пройти. Тот же приём, что у последнего
    владельца (`repositories/users.py`).

    Цена — один запрос на движение по складу и очередь между операциями с ОДНИМ
    товаром. Разные товары друг друга не ждут.
    """
    db.execute(select(Product.id).where(Product.id == product_id).with_for_update()).all()


def stock_of(
    db: Session,
    product_id: int,
    warehouse_id: int | None = None,
    on_date=None,
) -> int:
    """Остаток одного товара в тысячных долях единицы.

    `warehouse_id` — остаток на конкретном складе; без него сумма по всем.

    `on_date` — остаток НА ДАТУ: «сколько было на первое число». Это побочная
    выгода того, что остаток не хранится, и назвать её стоит вслух — при
    хранимом числе такой вопрос не имел бы ответа вовсе, а он нужен и для сверки
    с бумажной инвентаризацией, и для разговора с бухгалтером.

    coalesce — потому что SUM по пустому набору даёт NULL, а «движений не было»
    означает ровно ноль, а не «неизвестно».

    `as_int` — потому что `SUM()` в MySQL возвращает `Decimal`, а количество в
    проекте целое (разбор — в докстроке самой `as_int`).
    """
    stmt = select(func.coalesce(func.sum(StockMove.quantity_milli), 0)).where(
        StockMove.product_id == product_id
    )
    if warehouse_id is not None:
        stmt = stmt.where(StockMove.warehouse_id == warehouse_id)
    if on_date is not None:
        stmt = stmt.where(StockMove.happened_at <= on_date)
    return as_int(db.scalar(stmt))


def stock_by_product(
    db: Session,
    product_ids: list[int] | None = None,
    warehouse_id: int | None = None,
    on_date=None,
) -> dict[int, int]:
    """Остатки пачкой: {product_id: остаток в тысячных}.

    Один запрос на весь список товаров вместо запроса на строку — иначе экран
    склада на 500 позиций делает 500 обращений к базе.

    Если когда-нибудь этого станет мало (десятки тысяч движений на товар), кэш
    остатка следует завести отдельной таблицей `product_stock`, пересчитываемой
    в той же транзакции, что и вставка движения, — и обязательно с фоновой
    сверкой против этого запроса. Источником правды кэш при этом не становится
    НИКОГДА: разойдясь с историей однажды, он не даст способа узнать, какое из
    двух чисел верное, и склад придётся пересчитывать вручную по бумагам.
    """
    stmt = select(StockMove.product_id, func.coalesce(func.sum(StockMove.quantity_milli), 0))
    if product_ids is not None:
        if not product_ids:
            return {}
        stmt = stmt.where(StockMove.product_id.in_(product_ids))
    if warehouse_id is not None:
        stmt = stmt.where(StockMove.warehouse_id == warehouse_id)
    if on_date is not None:
        stmt = stmt.where(StockMove.happened_at <= on_date)
    stmt = stmt.group_by(StockMove.product_id)
    return {product_id: as_int(total) for product_id, total in db.execute(stmt).all()}


def stock_by_warehouse(
    db: Session,
    product_ids: list[int] | None = None,
    on_date=None,
) -> dict[int, dict[int, int]]:
    """Раскладка остатков по местам: {product_id: {warehouse_id: остаток}}.

    То, ради чего половина задачи. Ищем по данным товара, а в ответе показываем
    **где и сколько** — иначе на вопрос «а на точке-то оно есть?» отвечать
    нечем, и продавец звонит и спрашивает голосом.

    Один запрос на всю страницу, как и `stock_by_product`. Запрос на строку
    превратил бы поиск из 500 позиций в 500 обращений к базе — эта ошибка в
    блоке уже разбиралась и закрывалась, повторять её нельзя.

    Нули в ответ не попадают: склад, где товара никогда не было, — это не «0 шт»,
    а отсутствие строки. Дорисовать нули там, где они осмысленны (все склады в
    карточке товара), — дело вызывающего, у которого есть список складов.
    """
    stmt = select(
        StockMove.product_id,
        StockMove.warehouse_id,
        func.coalesce(func.sum(StockMove.quantity_milli), 0),
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        stmt = stmt.where(StockMove.product_id.in_(product_ids))
    if on_date is not None:
        stmt = stmt.where(StockMove.happened_at <= on_date)
    stmt = stmt.group_by(StockMove.product_id, StockMove.warehouse_id)

    spread: dict[int, dict[int, int]] = {}
    for product_id, warehouse_id, total in db.execute(stmt).all():
        spread.setdefault(product_id, {})[warehouse_id] = as_int(total)
    return spread


def _moves_base(
    product_id: int | None, deal_id: int | None, warehouse_id: int | None = None
) -> Select:
    stmt = select(StockMove)
    if product_id is not None:
        stmt = stmt.where(StockMove.product_id == product_id)
    if deal_id is not None:
        stmt = stmt.where(StockMove.deal_id == deal_id)
    if warehouse_id is not None:
        stmt = stmt.where(StockMove.warehouse_id == warehouse_id)
    return stmt


def list_moves(
    db: Session,
    product_id: int | None = None,
    deal_id: int | None = None,
    warehouse_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[StockMove], int]:
    stmt = _moves_base(product_id, deal_id, warehouse_id).order_by(
        StockMove.happened_at.desc(), StockMove.id.desc()
    )
    return page_of(db, stmt, page=page, per_page=per_page)


#: количество × цена даёт «минорные единицы, умноженные на QUANTITY_SCALE» —
#: сумма копится в этом масштабе и приводится к деньгам один раз в конце
_COST_EXPR = func.coalesce(
    func.sum(-StockMove.quantity_milli * func.coalesce(StockMove.cost_minor, 0)), 0
)


def _scaled_to_minor(total) -> int:
    """Масштабированная сумма → минорные единицы, целочисленно.

    Делить на 1000 обычным `/` нельзя: в Python это float, а деньги через float
    мы не считаем принципиально. Округление — к ближайшему от нуля, чтобы возврат
    на склад (отрицательный вклад) округлялся так же, как списание.

    `as_int` на входе, а не на выходе. Без него сюда приезжает `Decimal` (так
    `SUM()` отвечает в MySQL), и `//` возвращает `Decimal` же — то есть
    себестоимость заявки уезжала наружу не целой, вопреки подписи, и в ответ API
    попадала дробным числом (`jsonable_encoder` переводит `Decimal` во float).
    Значение при этом сходилось: знак здесь развёрнут явно, а на неотрицательных
    операндах «вниз» и «к нулю» — одно и то же. Опираться на это совпадение
    нельзя: сама по себе `//` у `Decimal` округляет к нулю, и стоит кому-то
    убрать явную ветку для отрицательных, как копейка возврата разойдётся.
    """
    total = as_int(total)
    half = QUANTITY_SCALE // 2
    if total >= 0:
        return (total + half) // QUANTITY_SCALE
    return -((-total + half) // QUANTITY_SCALE)


# Себестоимости ОДНОГО движения здесь больше нет: единственным её читателем был
# подписчик, вписывавший сумму в строку ленты, а лента вычёркивать деньги не
# умеет и проносила их мимо `warehouse.view_amounts` (см. `core/subscriptions.py`).
# Понадобится снова — считать её обязательно здесь же, рядом с `deal_cost_minor`
# и его округлением: посчитанная отдельно, она разойдётся с итогом во врезке на
# копейку, и объяснить расхождение будет нечем.


def deal_cost_minor(db: Session, deal_id: int) -> int:
    """Себестоимость товаров, списанных под заявку, в минорных единицах.

    Считается запросом по той же причине, что и остаток. Расход хранится
    отрицательным количеством, поэтому знак переворачиваем: себестоимость заявки —
    число положительное, а возврат на склад её уменьшает.
    """
    return _scaled_to_minor(db.scalar(select(_COST_EXPR).where(StockMove.deal_id == deal_id)))


def deal_cost_by_deal(db: Session, deal_ids: list[int]) -> dict[int, int]:
    """То же пачкой — для списка заявок."""
    if not deal_ids:
        return {}
    rows = db.execute(
        select(StockMove.deal_id, _COST_EXPR)
        .where(StockMove.deal_id.in_(deal_ids))
        .group_by(StockMove.deal_id)
    ).all()
    return {deal_id: _scaled_to_minor(total) for deal_id, total in rows}


# --- штрихкоды ---
#
# Их у товара несколько, и почему — разобрано в модели `ProductBarcode`.


def barcodes_of(db: Session, product_id: int) -> list[ProductBarcode]:
    """Коды товара. Основной первым — его печатают на наклейке."""
    return list(
        db.scalars(
            select(ProductBarcode)
            .where(ProductBarcode.product_id == product_id)
            .order_by(ProductBarcode.is_primary.desc(), ProductBarcode.id.asc())
        )
    )


def barcodes_by_products(db: Session, product_ids) -> dict[int, list[ProductBarcode]]:
    """Коды сразу нескольких товаров — одним запросом на весь список.

    Список товаров показывает код рядом с позицией, и запрос на каждую строку
    превратил бы страницу склада в двести обращений к базе.
    """
    product_ids = [i for i in set(product_ids) if i]
    if not product_ids:
        return {}
    grouped: dict[int, list[ProductBarcode]] = {}
    rows = db.scalars(
        select(ProductBarcode)
        .where(ProductBarcode.product_id.in_(product_ids))
        .order_by(ProductBarcode.is_primary.desc(), ProductBarcode.id.asc())
    )
    for row in rows:
        grouped.setdefault(row.product_id, []).append(row)
    return grouped


def get_barcode(db: Session, code: str) -> ProductBarcode | None:
    return db.scalar(select(ProductBarcode).where(ProductBarcode.code == code))


def barcode_of_product(db: Session, product_id: int, barcode_id: int) -> ProductBarcode | None:
    """Код, принадлежащий именно этому товару — иначе чужой удаляли бы по номеру."""
    row = db.get(ProductBarcode, barcode_id)
    if row is None or row.product_id != product_id:
        return None
    return row


def product_by_code(db: Session, code: str) -> Product | None:
    """Товар по отсканированному коду. Удалённый не считается.

    Мягко удалённый товар возвращать нельзя: сканер тогда молча подставил бы в
    заказ позицию, которой в справочнике нет, и найти её потом было бы негде.
    """
    return db.scalar(
        select(Product)
        .join(ProductBarcode, ProductBarcode.product_id == Product.id)
        .where(ProductBarcode.code == code, Product.deleted_at.is_(None))
    )


def internal_codes_like(db: Session, prefix: str) -> list[str]:
    """Выданные внутренние коды с этой приставкой — чтобы выдать следующий."""
    return list(
        db.scalars(
            select(ProductBarcode.code).where(
                ProductBarcode.code.startswith(prefix, autoescape=True)
            )
        )
    )


def add_barcode(db: Session, row: ProductBarcode) -> ProductBarcode:
    db.add(row)
    db.flush()
    return row


def drop_barcode(db: Session, row: ProductBarcode) -> None:
    db.delete(row)
    db.flush()


def make_primary(db: Session, row: ProductBarcode) -> None:
    """Сделать код основным, сняв признак с остальных кодов ЭТОГО товара.

    Двумя явными UPDATE и «свой» первым — по той же причине, что у основной
    фирмы: присваивание полю ORM ничего не пишет, если значение уже такое, и в
    гонке основных не остаётся вовсе. Здесь цена ошибки меньше (на наклейке
    окажется не тот из двух кодов), но приём один, и разводить два разных
    способа делать одно и то же незачем.
    """
    db.execute(update(ProductBarcode).where(ProductBarcode.id == row.id).values(is_primary=True))
    db.execute(
        update(ProductBarcode)
        .where(
            ProductBarcode.product_id == row.product_id,
            ProductBarcode.id != row.id,
            ProductBarcode.is_primary.is_(True),
        )
        .values(is_primary=False)
    )
    db.refresh(row)


def products_by_ids(db: Session, product_ids) -> list[Product]:
    """Товары пачкой. Удалённых не отдаём: печатать наклейку на то, чего нет в
    справочнике, значит наклеить на коробку код, который потом никого не найдёт."""
    product_ids = [i for i in set(product_ids) if i]
    if not product_ids:
        return []
    return list(
        db.scalars(
            select(Product).where(Product.id.in_(product_ids), Product.deleted_at.is_(None))
        )
    )


def moves_of_document(db: Session, document_id: int) -> list[StockMove]:
    """Движения, сделанные одним бланком. Нужны отмене: без них нельзя сказать,
    что именно списала эта отгрузка, и откатить её точно."""
    return list(
        db.scalars(
            select(StockMove)
            .where(StockMove.document_id == document_id)
            .order_by(StockMove.id.asc())
        )
    )


def spisano_po_zayavkam(db: Session, product_ids=None) -> dict[tuple[int, int], int]:
    """Сколько товара уже ушло со склада под каждую заявку: {(заявка, товар): тысячные}.

    Считается по движениям, а не по бумагам: бумагу можно сторнировать, и сторно
    — это тоже движение, обратное по знаку. Считая по движениям, возврат
    учитывается сам собой; считая по накладным, пришлось бы отдельно вычитать
    сторно и помнить об этом вечно. Тот же довод, что у `documents.promised`.

    Знак приводим к «сколько ушло»: расход отрицателен, поэтому берём минус
    суммы. Вернули больше, чем взяли, — величина уходит в минус, и обрезать её
    здесь нельзя: вызывающий вычитает её из нужды заявки, и обрезка молча
    завысила бы бронь.
    """
    zapros = (
        select(
            StockMove.deal_id,
            StockMove.product_id,
            -func.coalesce(func.sum(StockMove.quantity_milli), 0),
        )
        .where(StockMove.deal_id.is_not(None))
        .group_by(StockMove.deal_id, StockMove.product_id)
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        zapros = zapros.where(StockMove.product_id.in_(product_ids))
    return {
        (zayavka, tovar): as_int(skolko)
        for zayavka, tovar, skolko in db.execute(zapros).all()
    }
