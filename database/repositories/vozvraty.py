"""Статистика возвратов: сколько, на какую сумму, доля от отгрузок, что возвращают.

Возврат — вид бумаги (`kind = return`), проведённый — `closed`. Моментом
проведения считается `updated_at`: после проведения возврат не правится ни
одной ручкой, и строка больше не трогается.

Месяцы приходят готовыми границами в UTC — тот же довод, что у отчётов:
границу месяца задаёт календарь смотрящего, а не сервер.
"""

from datetime import datetime

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from database.models import Document, DocumentLine
from database.models.document import KIND_RETURN, KIND_SALES_ORDER, STATUS_CLOSED
from database.query import as_int


def _provedyonnye(kind: str, start: datetime, end: datetime):
    return (
        Document.kind == kind,
        Document.status == STATUS_CLOSED,
        Document.updated_at >= start,
        Document.updated_at < end,
    )


def svodka(db: Session, start: datetime, end: datetime) -> dict:
    """Итог за период: возвратов, сумма возвращённого, проведённых заказов покупателя."""
    vozvraty = db.execute(
        select(func.count(), func.coalesce(func.sum(Document.refund_minor), 0)).where(
            *_provedyonnye(KIND_RETURN, start, end)
        )
    ).one()
    otgruzki = db.scalar(
        select(func.count()).where(*_provedyonnye(KIND_SALES_ORDER, start, end))
    )
    return {
        "count": int(vozvraty[0] or 0),
        "refund": as_int(vozvraty[1]),
        "shipped_count": int(otgruzki or 0),
    }


def po_mesyatsam(db: Session, buckets: list[tuple[datetime, datetime]]) -> dict[int, dict]:
    """Возвраты и отгрузки по месяцам одним запросом: {номер месяца: {...}}."""
    if not buckets:
        return {}
    bucket = case(
        *[
            (and_(Document.updated_at >= start, Document.updated_at < end), index)
            for index, (start, end) in enumerate(buckets)
        ],
        else_=None,
    )
    rows = db.execute(
        select(
            bucket.label("bucket"),
            Document.kind,
            func.count(),
            func.coalesce(func.sum(Document.refund_minor), 0),
        )
        .where(
            Document.kind.in_((KIND_RETURN, KIND_SALES_ORDER)),
            Document.status == STATUS_CLOSED,
            Document.updated_at >= buckets[0][0],
            Document.updated_at < buckets[-1][1],
        )
        .group_by(bucket, Document.kind)
    ).all()
    itog: dict[int, dict] = {}
    for index, kind, count, refund in rows:
        if index is None:
            continue
        yacheyka = itog.setdefault(int(index), {"count": 0, "refund": 0, "shipped_count": 0})
        if kind == KIND_RETURN:
            yacheyka["count"] += int(count or 0)
            yacheyka["refund"] += as_int(refund)
        else:
            yacheyka["shipped_count"] += int(count or 0)
    return itog


def tovary(db: Session, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
    """Что возвращают чаще всего: по товару — количество и число возвратов."""
    rows = db.execute(
        select(
            DocumentLine.product_id,
            func.max(DocumentLine.name_snapshot),
            func.coalesce(func.sum(DocumentLine.quantity_milli), 0),
            func.count(func.distinct(DocumentLine.document_id)),
        )
        .join(Document, Document.id == DocumentLine.document_id)
        .where(*_provedyonnye(KIND_RETURN, start, end), DocumentLine.product_id.is_not(None))
        .group_by(DocumentLine.product_id)
        .order_by(func.coalesce(func.sum(DocumentLine.quantity_milli), 0).desc(), DocumentLine.product_id.asc())
        .limit(limit)
    ).all()
    return [
        {
            "product_id": int(product_id),
            "name": name or "",
            "quantity_milli": as_int(quantity),
            "returns": int(skolko or 0),
        }
        for product_id, name, quantity, skolko in rows
    ]


def prodazhi_tovara(db: Session, product_id: int, start: datetime) -> dict:
    """Сколько товара ушло проведёнными заказами покупателя с даты: количество и заказов."""
    stroka = db.execute(
        select(
            func.coalesce(func.sum(DocumentLine.quantity_milli), 0),
            func.count(func.distinct(DocumentLine.document_id)),
        )
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == KIND_SALES_ORDER,
            Document.status == STATUS_CLOSED,
            Document.updated_at >= start,
            DocumentLine.product_id == product_id,
        )
    ).one()
    return {"quantity_milli": as_int(stroka[0]), "count": int(stroka[1] or 0)}


def vozvraty_tovara(db: Session, product_id: int, start: datetime) -> dict:
    """Сколько товара вернулось проведёнными возвратами с даты: количество и возвратов."""
    stroka = db.execute(
        select(
            func.coalesce(func.sum(DocumentLine.quantity_milli), 0),
            func.count(func.distinct(DocumentLine.document_id)),
        )
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.kind == KIND_RETURN,
            Document.status == STATUS_CLOSED,
            Document.updated_at >= start,
            DocumentLine.product_id == product_id,
        )
    ).one()
    return {"quantity_milli": as_int(stroka[0]), "count": int(stroka[1] or 0)}
