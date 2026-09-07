"""Запросы отчёта продаж, которых нет у отчёта о выручке.

Суммы и число событий по окнам считает не этот файл, а те же вызовы, что и
отчёт о выручке (`reports.money_by_month`, `finance.postupleniya_po_mesyatsam`):
третий счёт тех же денег разошёлся бы с ними молча. Здесь только то, чего у
отчёта нет вовсе — первые события дня для подсказки и разрез по городам.

Половина по кассе живёт в `finance.py`, рядом с условием «что считается
пришедшими деньгами»; здесь половина по выигранным заявкам. Выбор между ними —
дело службы, которая знает базис выручки.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Client, Deal
from database.models.pipeline import KIND_WON
from database.repositories import pipeline as pipeline_repo


def _vyigrannye(db: Session) -> list[str]:
    """Ключи этапов вида «выиграно». Справочником, а не соединением: у отчётов
    оно уводит план запроса от окна по `closed_at` на этапы."""
    return [key for key, kind in pipeline_repo.kinds_by_key(db).items() if kind == KIND_WON]


def _v_okne(vyigrannye: list[str], ot: datetime, do: datetime):
    return (
        Deal.deleted_at.is_(None),
        Deal.stage.in_(vyigrannye),
        Deal.closed_at >= ot,
        Deal.closed_at < do,
    )


def pervye_vyigrannye_dney(
    db: Session, ot: datetime, do: datetime, predel_v_dne: int
) -> dict[str, list[tuple[str, str, int]]]:
    """{день: [(номер, название, сумма), …]} — первые выигранные заявки дня.

    Оконной функцией: за три месяца заявок бывают тысячи, а показываем мы по
    три на день. Достать всё и обрезать в Python значило бы прочитать базу
    целиком ради подсказки, которую видят по наведению.
    """
    vyigrannye = _vyigrannye(db)
    if not vyigrannye:
        return {}
    nomer = (
        func.row_number()
        .over(
            partition_by=func.date(Deal.closed_at),
            order_by=(Deal.closed_at.desc(), Deal.id.desc()),
        )
        .label("nomer")
    )
    vnutri = (
        select(
            func.date(Deal.closed_at).label("den"),
            Deal.id,
            Deal.title,
            func.coalesce(Deal.amount, 0).label("summa"),
            nomer,
        )
        .where(*_v_okne(vyigrannye, ot, do))
        .subquery()
    )
    rows = db.execute(
        select(vnutri.c.den, vnutri.c.id, vnutri.c.title, vnutri.c.summa)
        .where(vnutri.c.nomer <= predel_v_dne)
        .order_by(vnutri.c.den, vnutri.c.nomer, vnutri.c.id)
    ).all()
    itog: dict[str, list[tuple[str, str, int]]] = {}
    for den, nomer_zayavki, nazvanie, summa in rows:
        itog.setdefault(str(den), []).append((f"#{nomer_zayavki}", nazvanie or "", int(summa or 0)))
    return itog


def goroda_vyigrannyh(db: Session, ot: datetime, do: datetime, predel: int) -> list[tuple[str, int]]:
    """[(город, сумма), …] по убыванию суммы.

    Карточки без города пропускаются: пустая строка в списке городов — это не
    город, а пропуск в данных, и первой строкой она встала бы чаще всех.
    """
    vyigrannye = _vyigrannye(db)
    if not vyigrannye:
        return []
    rows = db.execute(
        select(Client.city, func.coalesce(func.sum(Deal.amount), 0).label("summa"))
        .join(Client, Client.id == Deal.client_id)
        .where(*_v_okne(vyigrannye, ot, do), Client.city != "")
        .group_by(Client.city)
        .order_by(func.coalesce(func.sum(Deal.amount), 0).desc(), Client.city)
        .limit(predel)
    ).all()
    return [(gorod, int(summa or 0)) for gorod, summa in rows]
