"""Запросы по своим юрлицам.

Главное здесь — **основная фирма ровно одна**, и держится это запросами, а не
частичным уникальным индексом. Причин две. Такие индексы в проекте запрещены:
система рассчитана на переезд SQLite → MySQL, а в MySQL частичных индексов нет
вовсе, и схема, которую нельзя перенести, сломается ровно в момент переезда.
Обычный же UNIQUE на колонку не заменяет его ничем — он запретил бы двум фирмам
одновременно НЕ быть основными, то есть сделал бы невозможным нормальный случай.

`make_default` пишет двумя явными UPDATE, и «своя» ставится ПЕРВОЙ. Через
присваивание полю ORM второй шаг иногда не давал запроса вовсе: если в момент
чтения фирма уже была основной, присваивание `True` ничего не меняло, и
SQLAlchemy правильно не писал ничего, — а соседний запрос тем временем признак
снимал. Поймано живым прогоном: пять раундов по три одновременные попытки, на
пятом основных стало **ноль**. Ноль хуже двух: две основные читатель разрешает
сам (берёт первую), а без основной бланк печатается без шапки — и виновата будет
бумага, а не гонка недельной давности.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from database.models import Company


def get(db: Session, company_id: int) -> Company | None:
    return db.get(Company, company_id)


def list_alive(db: Session) -> list[Company]:
    """Основная — первой: её выбирают чаще всех остальных вместе взятых."""
    return list(
        db.scalars(
            select(Company)
            .where(Company.deleted_at.is_(None))
            .order_by(Company.is_default.desc(), Company.name.asc(), Company.id.asc())
        )
    )


def default_company(db: Session) -> Company | None:
    """Фирма, от имени которой работаем, когда её не выбрали руками.

    Сортировка по id не украшение: окажись две записи помеченными основными,
    ответ обязан остаться одним и тем же при каждом вызове. Молча плавающая
    фирма в реквизитах хуже, чем неверная.
    """
    return db.scalars(
        select(Company)
        .where(Company.deleted_at.is_(None), Company.is_default.is_(True))
        .order_by(Company.id.asc())
    ).first()


def oldest_alive(db: Session) -> Company | None:
    """Самая давняя из живых — кандидат в основные, когда основную удалили."""
    return db.scalars(
        select(Company).where(Company.deleted_at.is_(None)).order_by(Company.id.asc())
    ).first()


def make_default(db: Session, company: Company) -> None:
    """Сделать фирму основной, сняв признак с остальных. Про порядок — в шапке."""
    db.execute(update(Company).where(Company.id == company.id).values(is_default=True))
    db.execute(
        update(Company)
        .where(Company.id != company.id, Company.is_default.is_(True))
        .values(is_default=False)
    )
    # Объект в памяти помнит прежнее значение: писали запросом, мимо него.
    db.refresh(company)
