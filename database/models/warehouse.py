"""Склад: товары и движения.

Главное решение блока — **остаток не хранится**. Его нет ни в `products`, ни
где-либо ещё: остаток равен сумме движений и считается запросом
(`database/repositories/warehouse.py`). Хранимое число рано или поздно разойдётся
с историей — из-за отката транзакции, из-за правки задним числом, из-за второго
процесса, — и сойтись обратно уже не сможет: неоткуда узнать, какая из двух цифр
правильная. Сумма движений неверной быть не может по построению.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Количество храним целым в тысячных долях единицы: 1.250 кг → 1250.
# Дробные килограммы и метры существуют, а float в учёте недопустим — 0.1 + 0.2
# в нём не равно 0.3, и склад начинает «терять» сотые доли на каждой операции.
# Три знака после запятой закрывают граммы и миллилитры; больше в рознице не
# встречается, а Integer в SQLite/MySQL — 8 байт, переполнения не будет.
QUANTITY_SCALE = 1000

# Единицы измерения — стабильные ключи, подписи живут в i18n фронтенда.
UNITS = ("pcs", "kg", "g", "l", "ml", "m", "m2", "pack", "hour")

# Виды движения. Ключи стабильные: они уходят в базу и в фильтры, подписи
# переводятся на фронтенде. Знак количества задаёт не вид, а само движение
# (приход +, расход −): «корректировка» бывает в обе стороны, и вычислять знак
# из вида означало бы иметь два источника правды об одном и том же.
MOVE_IN = "in"              # приход
MOVE_OUT = "out"            # расход (продажа, выдача)
MOVE_WRITEOFF = "writeoff"  # списание (брак, порча, недостача)
MOVE_ADJUST = "adjust"      # корректировка по инвентаризации
MOVE_RETURN = "return"      # возврат
MOVE_KINDS = (MOVE_IN, MOVE_OUT, MOVE_WRITEOFF, MOVE_ADJUST, MOVE_RETURN)


class Product(Base):
    """Позиция номенклатуры: товар или услуга."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Артикул уникален, но не обязателен. Именно поэтому NULL, а не пустая
    # строка: два товара без артикула — норма, а две пустые строки нарушили бы
    # уникальность. NULL в уникальном индексе не конфликтует ни в SQLite, ни в
    # MySQL — это ровно то поведение, которое здесь нужно.
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    unit: Mapped[str] = mapped_column(String(16), default="pcs", server_default="pcs")
    # Деньги — целые в минорных единицах (копейки/центы). NULL — «цену не
    # назвали», и это не то же самое, что 0: ноль означал бы «отдаём бесплатно».
    price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Услуга — та же строка прайса, но остатка у неё нет и быть не может:
    # «выезд мастера» нельзя оприходовать. Движения по услуге запрещены.
    is_service: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Порог предупреждения, в тех же тысячных. NULL — «не следим»; 0 — осмысленное
    # «предупреждай, как только уйдёт в минус».
    min_stock_milli: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # Мягкое удаление: на товар ссылаются движения, а история списаний не должна
    # исчезать вместе с карточкой.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class StockMove(Base):
    """Одно движение товара. Складывая их, получаем остаток — других источников нет."""

    __tablename__ = "stock_moves"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT, а не CASCADE: удалить товар вместе с движениями значит бесшумно
    # переписать историю склада — вчерашние списания под заявки просто исчезнут,
    # и себестоимость заявки станет другой. Штатный путь удаления —
    # `products.deleted_at`; ключ здесь стоит на случай прямого DELETE в базе.
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    # Знаковое: приход +, расход −. Остаток = SUM(quantity_milli) по товару,
    # поэтому вычитать при чтении ничего не нужно и перепутать знак негде.
    quantity_milli: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # Списание под заявку. SET NULL, а не CASCADE: товар физически ушёл со
    # склада, и удаление заявки не возвращает его на полку. Пропади движение
    # вместе с заявкой — остаток вырос бы сам собой, и никто бы не понял почему.
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Себестоимость за единицу на момент движения, в минорных единицах. Снимок, а
    # не ссылка на products.cost_minor: закупочная цена меняется, а себестоимость
    # прошлой заявки — нет. NULL — «не знали», это не ноль.
    cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Когда операция случилась на самом деле (склад часто заполняют вечером за
    # весь день) — и отдельно когда о ней узнала база. Расхождение этих двух дат
    # и есть повод не доверять «остатку на сейчас» задним числом.
    happened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    __table_args__ = (
        # Остаток — это `SUM(quantity_milli) GROUP BY product_id`, и количество
        # обязано лежать в самом индексе. Иначе база проходит движения по индексу
        # товара, но за каждым числом лезет в таблицу: двести тысяч случайных
        # чтений ради одной цифры на строку списка.
        Index("ix_stock_moves_product_qty", "product_id", "quantity_milli"),
        # История движений в карточке товара — по времени операции, а не записи.
        Index("ix_stock_moves_product_happened", "product_id", "happened_at"),
    )
