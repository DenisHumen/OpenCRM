"""Запросы по складам и переездам.

Главное здесь то же, что у своих юрлиц: **основной склад ровно один**, и
держится это запросами, а не частичным уникальным индексом. Причина та же —
частичных индексов в MySQL нет вовсе. Обычный UNIQUE на колонку не заменяет их ничем: он запретил бы двум
складам одновременно НЕ быть основными, то есть сделал бы невозможным
нормальный случай.

`make_default` пишет двумя явными UPDATE, и «свой» ставится ПЕРВЫМ. Разбор,
почему порядок принципиален и как обратный порядок оставлял ноль основных при
одновременных запросах, — в шапке `database/repositories/companies.py`. Здесь
повторена та же схема намеренно: два места с одним инвариантом должны выглядеть
одинаково, иначе однажды разойдутся.

Второе правило блока: **склад всегда хотя бы один**. Проверка живёт в сервисе,
но считает её отсюда `count_alive`.
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database.models import StockMove, StockTransfer, Warehouse
from database.query import as_int, page_of


def get(db: Session, warehouse_id: int, include_deleted: bool = False) -> Warehouse | None:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        return None
    if warehouse.deleted_at is not None and not include_deleted:
        return None
    return warehouse


def get_by_code(db: Session, code: str) -> Warehouse | None:
    """Ищет и среди закрытых: код уникален по всей таблице, включая закрытые."""
    return db.scalar(select(Warehouse).where(Warehouse.code == code))


def list_alive(db: Session) -> list[Warehouse]:
    """Основной — первым: на него попадает приход, и выбирают его чаще прочих."""
    return list(
        db.scalars(
            select(Warehouse)
            .where(Warehouse.deleted_at.is_(None))
            .order_by(Warehouse.is_default.desc(), Warehouse.name.asc(), Warehouse.id.asc())
        )
    )


def count_alive(db: Session) -> int:
    """Сколько складов открыто. Ответ на два вопроса сразу: можно ли закрыть ещё
    один и надо ли вообще показывать выбор склада в формах."""
    return (
        db.scalar(
            select(func.count()).select_from(Warehouse).where(Warehouse.deleted_at.is_(None))
        )
        or 0
    )


def default_warehouse(db: Session) -> Warehouse | None:
    """Куда попадает приход, если склад не выбрали.

    Сортировка по id не украшение: окажись две записи помеченными основными,
    ответ обязан остаться тем же при каждом вызове. Молча плавающий склад в
    приходе хуже, чем неверный: неверный заметят, плавающий — нет.
    """
    return db.scalars(
        select(Warehouse)
        .where(Warehouse.deleted_at.is_(None), Warehouse.is_default.is_(True))
        .order_by(Warehouse.id.asc())
    ).first()


def oldest_alive(db: Session) -> Warehouse | None:
    """Самый давний из открытых — кандидат в основные, когда основной закрыли."""
    return db.scalars(
        select(Warehouse).where(Warehouse.deleted_at.is_(None)).order_by(Warehouse.id.asc())
    ).first()


def make_default(db: Session, warehouse: Warehouse) -> None:
    """Сделать склад основным, сняв признак с остальных. Про порядок — в шапке."""
    db.execute(update(Warehouse).where(Warehouse.id == warehouse.id).values(is_default=True))
    db.execute(
        update(Warehouse)
        .where(Warehouse.id != warehouse.id, Warehouse.is_default.is_(True))
        .values(is_default=False)
    )
    # Объект в памяти помнит прежнее значение: писали запросом, мимо него.
    db.refresh(warehouse)


def has_moves(db: Session, warehouse_id: int) -> bool:
    """Были ли на складе движения. Склад с историей закрывают, а не удаляют."""
    return (
        db.scalar(
            select(StockMove.id).where(StockMove.warehouse_id == warehouse_id).limit(1)
        )
        is not None
    )


def nonzero_stock(db: Session, warehouse_id: int) -> list[tuple[int, int]]:
    """Что и сколько ещё лежит на складе: [(product_id, остаток)].

    Нужен ровно для одного отказа — «нельзя закрыть склад, на котором лежит
    товар». Отказать мало: человек должен видеть, что именно перевозить, иначе
    он пойдёт искать это глазами по всему справочнику.

    Ненулевые — с обеих сторон: минус на складе означает, что расход занесли, а
    приход забыли, и закрывать такой склад тем более рано.

    `as_int` — потому что `SUM()` в MySQL возвращает `Decimal`, а в подписи
    стоит целое. Сегодня отказ про непустой склад читает только длину списка, но
    остаток здесь — тот же остаток, что и везде, и приезжать он обязан таким же:
    иначе первый, кто решит назвать в отказе цифры, получит пятисотку на
    `format_quantity`. Разбор — в докстроке `query.as_int`.
    """
    total = func.coalesce(func.sum(StockMove.quantity_milli), 0)
    rows = db.execute(
        select(StockMove.product_id, total)
        .where(StockMove.warehouse_id == warehouse_id)
        .group_by(StockMove.product_id)
        .having(total != 0)
    ).all()
    return [(product_id, as_int(amount)) for product_id, amount in rows]


def names_of(db: Session, warehouse_ids) -> dict[int, str]:
    """{id: название} — для строк истории и раскладки. Закрытые тоже: движение
    старше закрытия склада, и подписать его нечем, кроме имени."""
    warehouse_ids = [i for i in set(warehouse_ids) if i]
    if not warehouse_ids:
        return {}
    rows = db.execute(
        select(Warehouse.id, Warehouse.name).where(Warehouse.id.in_(warehouse_ids))
    ).all()
    return dict(rows)


# --- переезды ---


def get_transfer(db: Session, transfer_id: int) -> StockTransfer | None:
    return db.get(StockTransfer, transfer_id)


def list_transfers(
    db: Session,
    warehouse_id: int | None = None,
    product_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[StockTransfer], int]:
    """Журнал переездов. Фильтр по складу берёт обе стороны: «что уехало отсюда
    и что приехало сюда» — один и тот же вопрос кладовщика.

    Фильтр по товару нужен карточке: журнал в ней обязан быть про эту позицию, а
    не про все сразу. Через подзапрос по строкам, а не соединением: соединение
    вернуло бы переезд дважды (у него две строки на позицию), и в журнале
    появились бы близнецы.
    """
    stmt = select(StockTransfer)
    if warehouse_id is not None:
        stmt = stmt.where(
            (StockTransfer.from_warehouse_id == warehouse_id)
            | (StockTransfer.to_warehouse_id == warehouse_id)
        )
    if product_id is not None:
        stmt = stmt.where(
            StockTransfer.id.in_(
                select(StockMove.transfer_id).where(StockMove.product_id == product_id)
            )
        )
    stmt = stmt.order_by(StockTransfer.happened_at.desc(), StockTransfer.id.desc())
    return page_of(db, stmt, page=page, per_page=per_page)


def moves_of_transfer(db: Session, transfer_id: int) -> list[StockMove]:
    """Строки одного переезда. Расход первым: он и объясняет, откуда товар."""
    return list(
        db.scalars(
            select(StockMove)
            .where(StockMove.transfer_id == transfer_id)
            .order_by(StockMove.quantity_milli.asc(), StockMove.id.asc())
        )
    )


def moves_by_transfers(db: Session, transfer_ids) -> dict[int, list[StockMove]]:
    """Строки сразу нескольких переездов — одним запросом на страницу журнала."""
    transfer_ids = [i for i in set(transfer_ids) if i]
    if not transfer_ids:
        return {}
    grouped: dict[int, list[StockMove]] = {}
    rows = db.scalars(
        select(StockMove)
        .where(StockMove.transfer_id.in_(transfer_ids))
        .order_by(StockMove.quantity_milli.asc(), StockMove.id.asc())
    )
    for row in rows:
        grouped.setdefault(row.transfer_id, []).append(row)
    return grouped


def reversal_of(db: Session, transfer_id: int) -> StockTransfer | None:
    """Переезд, отменяющий этот. Второй раз отменить один и тот же нельзя."""
    return db.scalars(
        select(StockTransfer).where(StockTransfer.reverses_id == transfer_id)
    ).first()


def reverted_ids(db: Session, transfer_ids) -> set[int]:
    """Какие из этих переездов уже отменены — одним запросом на страницу.

    Нужно интерфейсу: кнопка «отменить» на уже отменённом переезде — это
    обещание, которое сервер не выполнит (и правильно сделает). Показывать её
    значит звать человека на отказ.
    """
    transfer_ids = [i for i in set(transfer_ids) if i]
    if not transfer_ids:
        return set()
    rows = db.scalars(
        select(StockTransfer.reverses_id).where(StockTransfer.reverses_id.in_(transfer_ids))
    )
    return {row for row in rows if row is not None}
