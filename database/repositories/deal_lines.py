"""Запросы по строкам заявки.

Здесь же живёт итог заявки. Он **считается запросом**, а не складывается в
Python по загруженным строкам: список строк на экране может быть подрезан, а
итог обязан быть полным. Тот же довод, что у остатка склада.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Deal, DealLine, Product


def list_for_deal(db: Session, deal_id: int) -> list[DealLine]:
    return list(
        db.scalars(
            select(DealLine)
            .where(DealLine.deal_id == deal_id)
            .order_by(DealLine.sort_order, DealLine.id)
        )
    )


def get(db: Session, deal_id: int, line_id: int) -> DealLine | None:
    """Строка вместе с заявкой: без сверки чужую строку можно было бы править
    по её номеру, зная только свою заявку."""
    return db.scalar(
        select(DealLine).where(DealLine.id == line_id, DealLine.deal_id == deal_id)
    )


def add(db: Session, line: DealLine) -> DealLine:
    db.add(line)
    db.flush()
    return line


def drop(db: Session, line: DealLine) -> None:
    db.delete(line)
    db.flush()


def next_sort_order(db: Session, deal_id: int) -> int:
    last = db.scalar(
        select(func.max(DealLine.sort_order)).where(DealLine.deal_id == deal_id)
    )
    return (last or 0) + 10


def sum_for_deal(db: Session, deal_id: int) -> int | None:
    """Итог заявки в минорных единицах. None — строк нет вовсе.

    None и 0 разные: «строк нет» — это «сумму никто не называл», а ноль означал
    бы «отдаём бесплатно». Строка без цены (`price_minor IS NULL`) в итог не
    входит, но и не обнуляет его: цену ещё не назвали, а остальное уже посчитано.
    """
    if not db.scalar(select(func.count(DealLine.id)).where(DealLine.deal_id == deal_id)):
        return None
    # Делим КАЖДУЮ строку, а не сумму произведений: итог обязан сойтись с тем,
    # что человек складывает глазами. Цена 3,33 за 1,5 метра — это 4,995, строка
    # показывает 4,99, и две таких строки дают 9,98; одно деление по сумме дало
    # бы 9,99 — столбец в накладной не сходился бы с числом под ним.
    #
    # `DIV`, а не `/`: целочисленное деление без промежуточного DECIMAL. Деньги
    # и количества здесь неотрицательны, поэтому «вниз» и «к нулю» совпадают.
    itog = db.scalar(
        select(
            func.coalesce(
                func.sum((DealLine.price_minor * DealLine.quantity_milli).op("DIV")(1000)),
                0,
            )
        ).where(DealLine.deal_id == deal_id, DealLine.price_minor.is_not(None))
    )
    return int(itog)


def sebestoimost_zayavki(db: Session, deal_id: int) -> tuple[int, bool]:
    """Себестоимость по строкам: (сумма в минорных, известна ли она целиком).

    Второе число — не украшение. Себестоимости нет у своих трат и у товаров, у
    которых её не назвали; сложив то, что есть, и назвав это себестоимостью, мы
    показали бы прибыль ВЫШЕ настоящей — ровно там, где по ней решают о скидке.
    Поэтому «известна» отвечает отдельно, а показывает прибыль только тот, кому
    ответили «да».

    Это ОЖИДАЕМАЯ себестоимость — снимок на момент набора. Свершившуюся считает
    `warehouse.deal_cost_minor` по движениям, и до закрытия её ещё нет.
    """
    ryady = db.execute(
        select(DealLine.cost_minor, DealLine.quantity_milli).where(
            DealLine.deal_id == deal_id
        )
    ).all()
    if not ryady:
        return 0, False
    izvestna = all(cost is not None for cost, _ in ryady)
    # По строке, как и итог: иначе прибыль (итог минус себестоимость) считалась
    # бы из чисел, округлённых по разным правилам, и разъезжалась на копейки.
    summa = sum((cost * kol) // 1000 for cost, kol in ryady if cost is not None)
    return int(summa), izvestna


def count_for_deals(db: Session, deal_ids: list[int]) -> dict[int, int]:
    """Сколько строк у каждой заявки — одним запросом на список.

    Нужно списку и доске: строка «3 позиции» рядом с суммой отвечает на вопрос
    «сумма откуда», не открывая карточку.
    """
    if not deal_ids:
        return {}
    ryady = db.execute(
        select(DealLine.deal_id, func.count(DealLine.id))
        .where(DealLine.deal_id.in_(deal_ids))
        .group_by(DealLine.deal_id)
    ).all()
    return {deal_id: skolko for deal_id, skolko in ryady}


def po_otkrytym_zayavkam(db: Session, product_ids=None) -> dict[tuple[int, int], int]:
    """Сколько товара обещано строками ОТКРЫТЫХ заявок: {(заявка, товар): тысячные}.

    Закрытая заявка не держит ничего: товар по ней либо ушёл, либо не уйдёт
    никогда. Удалённая — тем более. Оба условия здесь, а не у вызывающего:
    забыть одно из них значит держать в брони товар, который никто не ждёт.

    Один запрос на весь список товаров, а не на каждый: экран склада из 500
    позиций иначе превратился бы в 500 обращений к базе.
    """
    zapros = (
        select(
            DealLine.deal_id,
            DealLine.product_id,
            func.coalesce(func.sum(DealLine.quantity_milli), 0),
        )
        .join(Deal, Deal.id == DealLine.deal_id)
        .join(Product, Product.id == DealLine.product_id)
        .where(
            Deal.closed_at.is_(None),
            Deal.deleted_at.is_(None),
            # Услуга остатка не имеет и держать не может: строка «выезд мастера»
            # иначе показывала бы вечную нехватку и бронь на карточке услуги.
            Product.is_service.is_(False),
        )
        .group_by(DealLine.deal_id, DealLine.product_id)
    )
    if product_ids is not None:
        if not product_ids:
            return {}
        zapros = zapros.where(DealLine.product_id.in_(product_ids))
    return {
        (zayavka, tovar): int(skolko)
        for zayavka, tovar, skolko in db.execute(zapros).all()
    }
