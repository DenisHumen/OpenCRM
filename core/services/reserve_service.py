"""Бронь: сколько товара обещано и кто его держит.

**Считается запросом и не хранится нигде.** Хранимое число разойдётся с
обещаниями в первый же откат транзакции, и узнать потом, какое из двух верно,
будет неоткуда. Отсюда же берётся правило «бронь не переживает того, кто её
держит»: отменили заказ, закрыли заявку — обещание исчезло само, без единой
строки кода на уборку.

**Живёт здесь, а не в `order_service`.** Блок заказов выключается, а склад
требует только заявки (`core/modules.py`): заявки обязаны бронировать и без
заказов. Оставить расчёт внутри блока заказов значило бы выключить бронь вместе
с ним.

Два источника обещаний, и складывать их напрямую нельзя — заказ, заведённый из
заявки, повторяет те же товары. Правило (`docs/bloki/19-sborka-zakaza.md` §Р3):

    бронь(заявка, товар) = max(0, в строках заявки
                                  − уже переданное её открытым заказам
                                  − уже списанное со склада под неё)

Ничего не помечается: «передано заказу» и «списано» — результаты запросов.
"""

from sqlalchemy.orm import Session

from core.services import modules_service
from database.models.document import KIND_SALES_ORDER, OPEN_ORDER_STATUSES
from database.repositories import deal_lines as lines_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo
from database.repositories import warehouse as warehouse_repo


def po_zayavkam(db: Session, product_ids=None) -> dict[int, int]:
    """Сколько держат открытые заявки: {товар: тысячные}."""
    nuzhno = lines_repo.po_otkrytym_zayavkam(db, product_ids)
    if not nuzhno:
        return {}
    # Заказов может не быть вовсе — блок выключен. Тогда заявка держит всё сама.
    peredano = (
        documents_repo.zakazano_po_zayavkam(
            db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, product_ids
        )
        if modules_service.is_enabled(db, "orders")
        else {}
    )
    spisano = warehouse_repo.spisano_po_zayavkam(db, product_ids)

    itog: dict[int, int] = {}
    for (zayavka, tovar), skolko in nuzhno.items():
        # Обрезаем по нулю на КАЖДОЙ заявке отдельно, а не на итоге: отгрузив
        # по одной заявке сверх набранного, иначе съели бы бронь соседней — а
        # тот покупатель своего товара ждёт по-прежнему. Тот же довод, что у
        # `documents.promised`.
        ostalos = skolko - peredano.get((zayavka, tovar), 0) - spisano.get((zayavka, tovar), 0)
        if ostalos > 0:
            itog[tovar] = itog.get(tovar, 0) + ostalos
    return itog


def reserved(db: Session, product_ids=None) -> dict[int, int]:
    """Вся бронь: заказы покупателей плюс открытые заявки."""
    itog = dict(po_zayavkam(db, product_ids))
    if modules_service.is_enabled(db, "orders"):
        for tovar, skolko in documents_repo.promised(
            db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, product_ids
        ).items():
            itog[tovar] = itog.get(tovar, 0) + skolko
    return itog


def expected(db: Session, product_ids=None) -> dict[int, int]:
    """Сколько приедет по заказам поставщику. Нужно, чтобы не заказать дважды."""
    if not modules_service.is_enabled(db, "orders"):
        return {}
    from database.models.document import KIND_PURCHASE_ORDER

    return documents_repo.promised(
        db, KIND_PURCHASE_ORDER, OPEN_ORDER_STATUSES, product_ids
    )


def availability(
    db: Session, product_ids: list[int], warehouse_id: int | None = None
) -> dict[int, dict[str, int]]:
    """Остаток, бронь, ожидается и доступно — по каждому товару.

    Шесть запросов НА ВЕСЬ СПИСОК, а не шесть на строку: остаток, три на бронь
    по заявкам (нужно, передано заказам, списано) и по одному на бронь заказов
    и на ожидаемую поставку. Со списка в 500 позиций построчный счёт дал бы
    три тысячи обращений.

    `warehouse_id` режет только ОСТАТОК: `available = stock(склад) − reserved(вся
    система)`. Резерв сквозной намеренно — заказ склада не называет, и наивный
    фильтр вычел бы ноль (docs/ustroystvo/16-api-sayta.md §4). Так витрина магазина видит
    полку зала, а обещанное вычитается всё; ошибка если и есть, то в безопасную
    сторону.
    """
    if not product_ids:
        return {}
    stock = warehouse_repo.stock_by_product(db, product_ids, warehouse_id=warehouse_id)
    hold = reserved(db, product_ids)
    coming = expected(db, product_ids)
    return {
        product_id: {
            "stock_milli": stock.get(product_id, 0),
            "reserved_milli": hold.get(product_id, 0),
            "expected_milli": coming.get(product_id, 0),
            "available_milli": stock.get(product_id, 0) - hold.get(product_id, 0),
        }
        for product_id in product_ids
    }


def derzhat(
    db: Session, product_id: int, tolko_manager: int | None = None, s_summami: bool = True
) -> list[dict]:
    """Кто держит товар в брони: заявки и заказы, каждый со своим количеством.

    Ради этого списка бронь и заводилась видимой: «доступно 2 из 5» без ответа
    «а где остальные три» отправляет человека искать их по всем заявкам руками.

    Заявка показывается той же величиной, что и в общем расчёте, — остатком
    после переданного заказам и списанного. Иначе на карточке товара стояло бы
    одно число, а в «доступно» участвовало другое.

    `tolko_manager` — область видимости заявок (`permissions_service.deals_scope`).
    Чужие заявки не пропадают, а **схлопываются в одну безымянную строку**:
    убрать их совсем значило бы показать «в брони 5» и ни одного держателя, то
    есть заставить искать недостачу, которой нет. Имя и ссылка при этом не
    отдаются — заголовок заявки несёт клиента и суть работы.

    Заказы не сужаются: у них своей области видимости нет вовсе, `orders.view`
    показывает все. Сузить их здесь значило бы завести второе правило доступа,
    которого нет в самом разделе заказов.
    """
    nuzhno = lines_repo.po_otkrytym_zayavkam(db, [product_id])
    zakazy_est = modules_service.is_enabled(db, "orders")
    peredano = (
        documents_repo.zakazano_po_zayavkam(
            db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, [product_id]
        )
        if zakazy_est
        else {}
    )
    spisano = warehouse_repo.spisano_po_zayavkam(db, [product_id])

    ostatki = {}
    for (zayavka, _), skolko in nuzhno.items():
        ostalos = (
            skolko
            - peredano.get((zayavka, product_id), 0)
            - spisano.get((zayavka, product_id), 0)
        )
        if ostalos > 0:
            ostatki[zayavka] = ostalos

    derzhateli = []
    chuzhogo = 0
    for zayavka in deals_repo.by_ids(db, list(ostatki)):
        if tolko_manager is not None and zayavka.manager_id != tolko_manager:
            chuzhogo += ostatki[zayavka.id]
            continue
        # Сумма и срок — рядом с количеством: «кто держит» спрашивают, чтобы
        # решить, кому отдать первому, а решают это по сумме и по сроку.
        derzhateli.append(
            {
                "kind": "deal",
                "id": zayavka.id,
                "title": zayavka.title,
                "quantity_milli": ostatki[zayavka.id],
                "amount": zayavka.amount if s_summami else None,
                "at": zayavka.created_at.isoformat() if zayavka.created_at else None,
                "due_at": zayavka.due_at.isoformat() if zayavka.due_at else None,
            }
        )
    if chuzhogo:
        derzhateli.append(
            {"kind": "deal", "id": None, "title": None, "quantity_milli": chuzhogo, "amount": None, "at": None, "due_at": None}
        )

    if zakazy_est:
        # То же число, что участвует в «доступно»: сырое количество строк
        # заказа показывало бы «заказ держит 10» рядом с «в брони 6».
        # Ввоз внутри: бланки тянут склад, склад — бронь, и на верхнем уровне
        # круг замкнулся бы на импорте.
        from core.services import document_service

        zakazy = documents_repo.otkrytye_s_tovarom(db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, product_id)
        stroki = documents_repo.lines_by_documents(db, [zakaz.id for zakaz, _ in zakazy]) if s_summami else {}
        for zakaz, skolko in zakazy:
            if skolko > 0:
                derzhateli.append(
                    {
                        "kind": "order",
                        "id": zakaz.id,
                        "title": zakaz.number,
                        "quantity_milli": skolko,
                        "amount": document_service.total_minor(stroki.get(zakaz.id, [])) if s_summami else None,
                        "at": zakaz.created_at.isoformat() if zakaz.created_at else None,
                        "due_at": zakaz.due_at.isoformat() if zakaz.due_at else None,
                    }
                )
    return derzhateli
