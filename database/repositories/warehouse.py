"""Запросы склада.

Здесь живёт главное правило блока: **остаток считается запросом**.

Соблазн загрузить движения товара и сложить их в Python велик, но он ломается
ровно там, где это дороже всего: список товаров показывает первые 50 движений, а
их 3000 — и остаток тихо оказывается неверным, причём выглядит правдоподобно.
Поэтому суммирует всегда база: `SUM(quantity_milli) GROUP BY product_id` не
зависит ни от пагинации, ни от того, что успело попасть в сессию.
"""

import re

from sqlalchemy import Select, case, func, or_, select, update
from sqlalchemy.orm import Session

from database.models import Product, ProductBarcode, ProductPhoto, StockMove
from database.models.warehouse import (
    MOVE_OUT,
    MOVE_RETURN,
    MOVE_WRITEOFF,
    QUANTITY_SCALE,
)
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

    Отбор образцом, а не «верхние полсотни строк диапазона». Полсотни хватало,
    пока подделок мало, но `A-0000420` — семь цифр вместо шести — при побайтном
    сравнении стоит ВЫШЕ любого нашего и остаётся в диапазоне. Полсотни таких, и
    окно не доходит до настоящего максимума: счёт начинается с единицы, упирается
    в занятое, и заведение товара отказывает при полностью свободном диапазоне.

    `REGEXP` — единственная точная приметa нашего вида, и она законна: база у
    продукта одна, MySQL (CLAUDE.md §1). Взамен один `MAX` без выборки и без
    разбора в питоне.
    """
    obrazets = f"^{re.escape(prefix)}[0-9]{{{digits}}}$"
    nash = db.scalar(
        select(func.max(Product.sku)).where(Product.sku.op("REGEXP")(obrazets))
    )
    return int(nash[len(prefix):]) if nash else 0


#: Порядки списка товаров. По остатку — по сумме движений на всех складах:
#: остаток не хранится, и сортировать по нему можно только той же суммой.
PRODUCT_SORTS = ("name", "stock", "stock_desc")


def search_products(
    db: Session,
    q: str | None = None,
    include_services: bool = True,
    page: int = 1,
    per_page: int = 50,
    sort: str | None = None,
) -> tuple[list[Product], int]:
    stmt = select(Product).where(Product.deleted_at.is_(None))
    if q:
        needle = q.strip()
        stmt = stmt.where(or_(contains(Product.name, needle), contains(Product.sku, needle)))
    if not include_services:
        stmt = stmt.where(Product.is_service.is_(False))
    if sort in ("stock", "stock_desc"):
        ostatok = (
            select(StockMove.product_id.label("pid"), func.sum(StockMove.quantity_milli).label("summa"))
            .group_by(StockMove.product_id)
            .subquery()
        )
        velichina = func.coalesce(ostatok.c.summa, 0)
        stmt = stmt.outerjoin(ostatok, ostatok.c.pid == Product.id)
        if sort == "stock":
            stmt = stmt.order_by(velichina.asc(), Product.id.asc())
        else:
            stmt = stmt.order_by(velichina.desc(), Product.id.desc())
    else:
        stmt = stmt.order_by(Product.name.asc(), Product.id.asc())
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
    product_id: int | None, deal_id: int | None, warehouse_id: int | None = None, kind: str | None = None
) -> Select:
    stmt = select(StockMove)
    if kind:
        stmt = stmt.where(StockMove.kind == kind)
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
    kind: str | None = None,
) -> tuple[list[StockMove], int]:
    stmt = _moves_base(product_id, deal_id, warehouse_id, kind).order_by(
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


def products_by_ids(db: Session, product_ids, include_deleted: bool = False) -> list[Product]:
    """Товары пачкой. Удалённых не отдаём: печатать наклейку на то, чего нет в
    справочнике, значит наклеить на коробку код, который потом никого не найдёт.

    `include_deleted` — для уже выписанной бумаги: она печатается такой, какой
    её выписали, и удаление товара не имеет права опустошить столбец «Ед.».
    """
    product_ids = [i for i in set(product_ids) if i]
    if not product_ids:
        return []
    usloviya = [Product.id.in_(product_ids)]
    if not include_deleted:
        usloviya.append(Product.deleted_at.is_(None))
    return list(db.scalars(select(Product).where(*usloviya)))


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


#: Виды движений, которые означают «ушло со склада под заявку», и возврат к ним.
#:
#: Приход сюда не входит НАМЕРЕННО. Закупка под клиента цепляется к заявке, и
#: приходная накладная наследует её `deal_id` — сложи их со знаком, и величина
#: уйдёт в минус, а закрытие спишет вдвое больше, чем в строках. Корректировка
#: по инвентаризации к заявке отношения не имеет тем более.
VIDY_UHODA = (MOVE_OUT, MOVE_WRITEOFF, MOVE_RETURN)


def spisano_po_zayavkam(db: Session, product_ids=None) -> dict[tuple[int, int], int]:
    """Сколько товара уже ушло со склада под каждую заявку: {(заявка, товар): тысячные}.

    Считается по движениям, а не по бумагам: бумагу можно сторнировать, и сторно
    — это тоже движение, обратное по знаку. Считая по движениям, возврат
    учитывается сам собой; считая по накладным, пришлось бы отдельно вычитать
    сторно и помнить об этом вечно. Тот же довод, что у `documents.promised`.

    Обрезаем нулём на КАЖДОЙ паре: вернули больше, чем отгрузили, — величина
    ушла бы в минус, а вызывающий вычитает её, то есть минус РАЗДУЛ бы и бронь,
    и списание. Тот же приём, что у `documents._otgruzheno_po_zakazam`.
    """
    zapros = (
        select(
            StockMove.deal_id,
            StockMove.product_id,
            -func.coalesce(func.sum(StockMove.quantity_milli), 0),
        )
        .where(StockMove.deal_id.is_not(None), StockMove.kind.in_(VIDY_UHODA))
        .group_by(StockMove.deal_id, StockMove.product_id)
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        zapros = zapros.where(StockMove.product_id.in_(product_ids))
    return {
        (zayavka, tovar): max(0, as_int(skolko))
        for zayavka, tovar, skolko in db.execute(zapros).all()
    }


# --- витрина сайта ---------------------------------------------------------------
#
# Карточка опубликована, когда товар не удалён и по складу магазина было хоть одно
# движение (услуга — только не удалена). «Было движение», а не «остаток больше
# нуля»: распроданный до нуля товар карточку сохраняет, иначе адрес страницы
# умирал бы в момент продажи последней штуки (docs/ustroystvo/16-api-sayta.md §3).


def _opublikovan(warehouse_id: int | None):
    # Ключ без склада видит одни услуги: у товара без места нет и полки.
    if warehouse_id is None:
        return Product.is_service.is_(True)
    dvigalsya = select(StockMove.product_id).where(StockMove.warehouse_id == warehouse_id)
    return or_(Product.is_service.is_(True), Product.id.in_(dvigalsya))


def site_catalog(db: Session, warehouse_id: int | None, page: int = 1, per_page: int = 100):
    stmt = (
        select(Product)
        .where(Product.deleted_at.is_(None), _opublikovan(warehouse_id))
        .order_by(Product.id)
    )
    return page_of(db, stmt, page=page, per_page=per_page)


def site_product(db: Session, warehouse_id: int | None, product_id: int) -> Product | None:
    return db.scalar(
        select(Product).where(
            Product.id == product_id, Product.deleted_at.is_(None), _opublikovan(warehouse_id)
        )
    )


def site_products(db: Session, warehouse_id: int | None, product_ids) -> list[Product]:
    product_ids = list(set(product_ids))
    if not product_ids:
        return []
    return list(
        db.scalars(
            select(Product).where(
                Product.id.in_(product_ids), Product.deleted_at.is_(None), _opublikovan(warehouse_id)
            )
        )
    )


def site_summary(db: Session, warehouse_id: int) -> dict[str, int]:
    """Сколько карточек на сайте и сколько из них без цены — одним запросом на оба."""
    stmt = select(
        func.count(Product.id),
        func.coalesce(func.sum(case((Product.price_minor.is_(None), 1), else_=0)), 0),
    ).where(Product.deleted_at.is_(None), _opublikovan(warehouse_id))
    vsego, bez_tseny = db.execute(stmt).one()
    return {"published": as_int(vsego), "without_price": as_int(bez_tseny)}


def site_changed_since(db: Session, warehouse_id: int | None, since) -> set[int]:
    """Товары, чья карточка правилась или по складу магазина было движение после `since`."""
    pravleny = select(Product.id).where(Product.updated_at > since)
    itog = {as_int(i) for i in db.scalars(pravleny)}
    if warehouse_id is not None:
        dvigalis = (
            select(StockMove.product_id)
            .where(StockMove.warehouse_id == warehouse_id, StockMove.created_at > since)
            .distinct()
        )
        itog |= {as_int(i) for i in db.scalars(dvigalis)}
    return itog


def all_photos_of_products(db: Session, product_ids) -> dict[int, list[ProductPhoto]]:
    """Все снимки каждого товара, в порядке показа. Одним запросом на страницу."""
    product_ids = [i for i in set(product_ids) if i]
    if not product_ids:
        return {}
    rows = db.scalars(
        select(ProductPhoto)
        .where(ProductPhoto.product_id.in_(product_ids))
        .order_by(ProductPhoto.sort_order.asc(), ProductPhoto.id.asc())
    )
    itog: dict[int, list[ProductPhoto]] = {}
    for row in rows:
        itog.setdefault(row.product_id, []).append(row)
    return itog


def photo_by_uid(db: Session, photo_uid: str) -> ProductPhoto | None:
    return db.scalar(select(ProductPhoto).where(ProductPhoto.photo_uid == photo_uid))


def products_by_skus(db: Session, skus) -> list[Product]:
    skus = list(set(skus))
    if not skus:
        return []
    return list(db.scalars(select(Product).where(Product.sku.in_(skus), Product.deleted_at.is_(None))))


def malo_ili_konchilos(db: Session, limit: int = 5) -> tuple[list[tuple[Product, int]], int]:
    """Товары, чей остаток не выше порога «заканчивается» или ноль, — и сколько их всего.

    Остаток считается здесь же суммой движений (правило: он не хранится), а
    товар без единого движения — это ноль, и он «закончился» так же честно,
    как и списанный до нуля: внешнее соединение, а не внутреннее.
    """
    ostatok = (
        select(StockMove.product_id.label("tovar"), func.sum(StockMove.quantity_milli).label("milli"))
        .group_by(StockMove.product_id)
        .subquery()
    )
    milli = func.coalesce(ostatok.c.milli, 0)
    usloviya = (
        Product.deleted_at.is_(None),
        Product.is_service.is_(False),
        milli <= func.coalesce(Product.min_stock_milli, 0),
    )
    ryady = db.execute(
        select(Product, milli)
        .outerjoin(ostatok, ostatok.c.tovar == Product.id)
        .where(*usloviya)
        .order_by(milli.asc(), Product.id.asc())
        .limit(limit)
    ).all()
    vsego = db.scalar(
        select(func.count()).select_from(Product).outerjoin(ostatok, ostatok.c.tovar == Product.id).where(*usloviya)
    )
    return [(product, as_int(summa)) for product, summa in ryady], int(vsego or 0)
