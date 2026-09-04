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

from database.types import ExactString, text_default
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Количество храним целым в тысячных долях единицы: 1.250 кг → 1250.
# Дробные килограммы и метры существуют, а float в учёте недопустим — 0.1 + 0.2
# в нём не равно 0.3, и склад начинает «терять» сотые доли на каждой операции.
# Три знака после запятой закрывают граммы и миллилитры; больше в рознице не
# встречается.
#
# Про запас прочности. `Integer` в MySQL — это INT, **4 байта**, то есть до
# 2 147 483 647, а в тысячных долях это всего 2 147 483 единицы. Для штук,
# килограммов и метров розницы запас громадный, но это предел, а не его
# отсутствие: сумма движений по одному
# товару за годы работы складывается в BIGINT (`SUM` в MySQL расширяется сам),
# а вот отдельное движение больше двух миллионов единиц туда не влезет.
# Понадобится — колонку расширяют до BigInteger; сейчас это было бы лишним
# весом в каждой строке самой большой таблицы.
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

# Тип склада — закрытый набор ключей, как виды движения. Не название: видимость
# товара сайту, зависящая от названия, закрывала бы витрину исправлением
# опечатки в слове «Магазин» (docs/16-api-sayta.md §2).
#: Обычное место хранения: подсобка, стеллаж, машина мастера. Сайту не виден.
WH_STOCK = "stock"
#: Торговый зал магазина. Ровно то место, остаток которого показывает сайт.
WH_SHOP = "shop"
#: Товар в пути: уехал от поставщика, до нас не доехал.
WH_TRANSIT = "transit"
#: Брак, возврат, карантин. Товар есть, продавать его нельзя.
WH_DEFECT = "defect"
WAREHOUSE_KINDS = (WH_STOCK, WH_SHOP, WH_TRANSIT, WH_DEFECT)


class Warehouse(Base):
    """Место, где лежит товар: торговый зал, подсобка, машина выездного мастера.

    **Склад живёт на движении, а не на товаре.** Товар — строка справочника, он
    существует один раз; лежит он в разных местах, и это свойство операции, а не
    карточки. Отсюда `stock_moves.warehouse_id` и остаток
    `SUM(quantity_milli) GROUP BY product_id, warehouse_id`.

    Инвариант блока при этом цел: остаток по-прежнему **не хранится** и
    по-прежнему считается запросом, меняется только группировка. Колонка «остаток
    на складе N» сломала бы ровно то, ради чего склад устроен так, как устроен, —
    хранимое число однажды разойдётся с историей, и узнать, какая из двух цифр
    верна, будет неоткуда.

    **Складов всегда хотя бы один**, и ровно один помечен основным — тот же вид
    инварианта, что «основная фирма ровно одна», и держится он так же, запросами
    (частичных индексов в проекте нет: в MySQL их не существует).
    """

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    # Короткое обозначение для наклеек и печати. NULL, а не пустая строка: два
    # склада без кода — норма, а две пустые строки нарушили бы уникальность.
    code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )
    address: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Куда попадает приход, если склад не выбрали.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Что это за место (`WAREHOUSE_KINDS`). Сайту виден остаток складов `shop`
    # и только их; тип не спорит с `is_default` — основным может быть подсобка.
    kind: Mapped[str] = mapped_column(String(16), default=WH_STOCK, server_default=WH_STOCK)
    note: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # Мягкое удаление: на склад ссылаются движения, а история не должна исчезать
    # вместе с местом. Склад с движениями не удаляют, а закрывают.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class StockTransfer(Base):
    """Шапка переезда товара между складами. Строки — это движения со ссылкой сюда.

    **Перемещение — не приход и не расход, а два движения, связанные ссылкой.**

    Соблазн сделать одну строку с `from_warehouse_id` и `to_warehouse_id` велик и
    ломает всё сразу: одна строка влияла бы на два склада с разными знаками, и
    `SUM GROUP BY warehouse_id` перестал бы быть верным; каждый будущий запрос по
    складу обязан был бы знать про особый случай «а у перемещений всё иначе»;
    забыть про этот случай можно ровно один раз — и остаток тихо разойдётся.

    Поэтому расход со склада А и приход на склад Б — две обычные строки. Агрегат
    остаётся ровно таким, каким был, а «это один переезд, а не два независимых
    события» несёт `transfer_id`.

    Нового вида движения не нужно: годятся `out` и `in`. Отличает перемещение
    именно ссылка — это честнее, чем ещё один `kind`, который пришлось бы
    исключать в половине отчётов.
    """

    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: переезд — это история, и удалить склад вместе с ней значит
    # переписать прошлое. Штатный путь — закрытие склада (`deleted_at`).
    from_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    to_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    comment: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Отмена переезда — это обратный переезд, а не удаление строк: склад обязан
    # помнить, что уезжало и что вернулось. Здесь лежит ссылка на тот переезд,
    # который этот отменяет; у обычного переезда NULL.
    reverses_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    happened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )


class Product(Base):
    """Позиция номенклатуры: товар или услуга."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Артикул есть у каждого товара: не назвали — выдаём сами (`A-000123`).
    # Раньше колонка была NULL-евой с доводом «два товара без артикула — норма»;
    # довод отменён владельцем, разбор — в docs/19-sborka-zakaza.md §Р1.
    # Побайтно: артикул — опознавательный знак товара, и «ABC» с «abc» обязаны
    # остаться разными. Сравнение MySQL по умолчанию регистронезависимо и слило
    # бы их в один — уникальность отвергала бы дубль там, где дубля нет.
    sku: Mapped[str] = mapped_column(
        ExactString(64), nullable=False, unique=True, index=True
    )
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
    note: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    # Своя колонка, а не `note`: заметка кладовщика («вернули, пахнет») на
    # витрине объяснению не поддаётся. Разметки нет — текст едет как текст.
    site_description: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Индекс — под ленту изменений сайта: она отбирает по этой колонке.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), index=True
    )
    # Мягкое удаление: на товар ссылаются движения, а история списаний не должна
    # исчезать вместе с карточкой.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


#: Виды штрихкода. Ключи стабильные — уходят в базу и в печать наклейки.
#:
#: `ean13` и `code128` — то, что напечатано на упаковке производителем;
#: `internal` — наш собственный, выданный при заведении товара. Различать их
#: нужно не для красоты: заводской у товара может смениться (поставщик
#: перевыпустил упаковку), а внутренний не меняется никогда — на него уже
#: наклеены этикетки на коробках в углу склада.
BARCODE_EAN13 = "ean13"
BARCODE_CODE128 = "code128"
BARCODE_INTERNAL = "internal"
BARCODE_KINDS = (BARCODE_EAN13, BARCODE_CODE128, BARCODE_INTERNAL)


class ProductBarcode(Base):
    """Штрихкод товара. Их у товара НЕСКОЛЬКО, и это главное решение блока.

    Соблазн обойтись колонкой `barcode` в `products` велик и ломается на второй
    месяц работы:

    - у товара бывает **несколько упаковок** — штука, блок, коробка, — и у
      каждой свой код на боку;
    - поставщик **сменил код** на том же товаре, а старые остатки лежат с
      прежним;
    - один товар приходит **от двух поставщиков** с разными кодами;
    - у товара собственного производства заводского кода **нет вовсе**.

    Все четыре случая — обычная жизнь мастерской и магазина, и в одну колонку
    они не помещаются ни при каком старании.

    **Код уникален по всей таблице.** Один код не может означать два товара:
    иначе сканирование перестаёт быть однозначным, а однозначность — это
    единственное, ради чего оно существует. Проверка стоит в базе, а не в
    сервисе: двое приёмщиков заводят карточки одновременно, и окно между
    «проверили» и «вставили» существует всегда.
    """

    __tablename__ = "product_barcodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE, а не RESTRICT: штрихкод — свойство карточки, а не история. Ушла
    # карточка (по-настоящему, из корзины) — уходят и её коды. Движения склада
    # при этом остаются, у них своя защита.
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default=BARCODE_CODE128)
    # Сколько единиц товара в этой упаковке, в тех же тысячных. Штука — 1000,
    # блок из десяти — 10000. Отсканировали коробку — в заказ уходит десять
    # штук, а не одна: без этого приёмка коробками считается руками, ради чего
    # штрихкоды и заводились.
    pack_size_milli: Mapped[int] = mapped_column(
        Integer, default=QUANTITY_SCALE, server_default=str(QUANTITY_SCALE)
    )
    # Какой код печатать на наклейке, когда их несколько. Ровно один на товар —
    # держится сервисом, а не частичным индексом: частичных индексов в проекте
    # нет, потому что в MySQL их не существует (см. `company_service.set_default`).
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProductPhoto(Base):
    """Снимок товара. Их несколько, и порядок задаёт человек.

    **Зачем вообще.** Название на складе опознаёт вещь плохо: «шлейф 40-pin» и
    «шлейф 40-pin (узкий)» — две строки, отличить которые на полке можно только
    глазами. Снимок отвечает на вопрос «это она?» за секунду, а описание —
    никогда.

    **Первая по порядку — та, что показывают везде**, где место есть только для
    одной: в списке, в поиске, в строке заказа. Отдельного признака «главная»
    нет намеренно. Он завёл бы инвариант «ровно одна», а такие в этом проекте
    держатся запросами, а не частичными индексами (их в MySQL не существует) —
    то есть цена признака выше, чем польза от него. Порядок и так задаётся
    человеком, а «первая» — понятие, которое не может разъехаться само.

    **Файлы на диске, в базе только след.** Картинки в базе — это раздутый дамп,
    медленные копии и невозможность отдать файл напрямую. То же решение, что у
    файлов клиента и у работ доски.

    CASCADE у товара, как и у штрихкода: снимок — свойство карточки, а не
    история. Ушла карточка по-настоящему — уходят и снимки; движения склада при
    этом остаются, у них своя защита.
    """

    __tablename__ = "product_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Без своего `index=True`: составной ниже начинается с этой же колонки, а
    # префикс составного обслуживает и внешний ключ, и отбор по товару. Второй
    # индекс был бы не просто лишним — MySQL не даёт снять тот, на который
    # опирается внешний ключ, и откат миграции упирался бы в него.
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Имя файлов на диске без расширения: `<uid>.webp` и `<uid>-thumb.webp`.
    photo_uid: Mapped[str] = mapped_column(String(64), unique=True)
    #: Как файл назывался у того, кто его загрузил. Нужно, чтобы в журнале
    #: удаления стояло имя, а не «фотография 17».
    original_name: Mapped[str] = mapped_column(String(255))
    #: Вес ОБОИХ файлов на диске: показанный размер обязан совпадать с тем, что
    #: освободится при удалении.
    size_bytes: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # Порядок внутри товара — единственный способ, которым этот список
        # читают. Индекс составной, чтобы выдача не требовала сортировки.
        Index("ix_product_photos_product_order", "product_id", "sort_order"),
    )


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
    # Где это произошло. RESTRICT по той же причине, что у товара: удалить склад
    # вместе с движениями значит бесшумно переписать историю.
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    # Часть переезда или обычное движение. SET NULL, а не CASCADE: пропади
    # движение вместе с шапкой переезда — остаток вырос бы сам собой.
    transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Каким бланком вызвано движение: отгрузкой заказа, приёмкой поставки,
    # проведением акта. SET NULL по той же причине, что у заявки: товар
    # физически ушёл со склада, и удаление бумаги не возвращает его на полку.
    #
    # Ссылка нужна не для отчётности, а для отмены: без неё нельзя ни сказать,
    # какие именно движения сделала эта отгрузка, ни откатить их точно.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
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
    comment: Mapped[str] = mapped_column(Text, default="", server_default=text_default())
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
        # Лента изменений сайта отбирает движения склада по дате ЗАПИСИ, а не
        # операции: занесённое сегодня за прошлый вторник меняет сегодняшний
        # остаток. Соседний `ix_stock_moves_wh_happened` стоит по другой дате.
        Index("ix_stock_moves_wh_created", "warehouse_id", "created_at"),
        # История движений в карточке товара — по времени операции, а не записи.
        Index("ix_stock_moves_product_happened", "product_id", "happened_at"),
        # То же самое, но для раскладки по складам: группировка стала парой, и
        # индекса по одному товару ей мало — база пошла бы по нему, а за складом
        # и количеством лезла бы в таблицу на каждой строке.
        Index("ix_stock_moves_wh_product_qty", "warehouse_id", "product_id", "quantity_milli"),
        # История одного склада: «что происходило в подсобке за неделю».
        Index("ix_stock_moves_wh_happened", "warehouse_id", "happened_at"),
    )
