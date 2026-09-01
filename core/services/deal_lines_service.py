"""Строки заявки: товары со склада и свои траты.

Отдельным модулем, а не внутри `deal_service`: строке нужен и товар, и заявка, а
`warehouse_service` уже зовёт `deal_service.parse_money` — сложить это в один
модуль значит замкнуть ввоз в кольцо.

Что за строка — не хранится, а выводится из `product_id` (разбор:
`docs/19-sborka-zakaza.md` §Р2).
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import reserve_service, warehouse_service
from core.services.deal_service import get_deal, parse_money
from core.utils import now_utc
from database.models import Deal, DealLine, User
from database.repositories import deal_lines as lines_repo
from database.models.warehouse import MOVE_OUT
from database.repositories import warehouse as warehouse_repo

#: Ширина `deal_lines.name_snapshot`. Длиннее — отказ, а не обрезка: обрезанное
#: название уходит в счёт клиенту, и заметят это не здесь.
MAX_NAME = 200


def spisok(db: Session, deal_id: int) -> list[DealLine]:
    get_deal(db, deal_id)
    return lines_repo.list_for_deal(db, deal_id)


def itog(db: Session, deal_id: int) -> int | None:
    """Итог заявки по строкам. None — строк нет: сумму никто не называл."""
    return lines_repo.sum_for_deal(db, deal_id)


def dobavit(db: Session, deal_id: int, data: dict) -> DealLine:
    deal = _otkrytaya(db, deal_id)
    kolichestvo = _kolichestvo(data.get("quantity"))
    tovar = _tovar(db, data)

    if tovar is None:
        # Своя трата: упаковка, доставка, работа без номенклатуры.
        nazvanie = _nazvanie(data.get("name"))
        cena = parse_money(data.get("price"), "price")
        sebestoimost = None
    else:
        # Снимок названия и себестоимости: товар переименуют и переоценят, а
        # проданная заявка обязана остаться такой, какой её подписал клиент.
        nazvanie = tovar.name[:MAX_NAME]
        cena = parse_money(data.get("price"), "price")
        if cena is None:
            cena = tovar.price_minor
        sebestoimost = tovar.cost_minor

    stroka = lines_repo.add(
        db,
        DealLine(
            deal_id=deal.id,
            product_id=None if tovar is None else tovar.id,
            name_snapshot=nazvanie,
            quantity_milli=kolichestvo,
            price_minor=cena,
            cost_minor=sebestoimost,
            sort_order=lines_repo.next_sort_order(db, deal.id),
        ),
    )
    pereschitat_summu(db, deal)
    return stroka


def pravit(db: Session, deal_id: int, line_id: int, data: dict) -> DealLine:
    deal = _otkrytaya(db, deal_id)
    stroka = _stroka(db, deal_id, line_id)
    if "quantity" in data:
        stroka.quantity_milli = _kolichestvo(data["quantity"])
    if "price" in data:
        stroka.price_minor = parse_money(data["price"], "price")
    # Название правится только у своей траты: у товарной строки это снимок
    # названия товара, и переписать его значит соврать о том, что продали.
    if "name" in data:
        if stroka.product_id is not None:
            raise errors.ValidationError(
                "A product line keeps the product name", code="name_is_snapshot"
            )
        stroka.name_snapshot = _nazvanie(data["name"])
    db.flush()
    pereschitat_summu(db, deal)
    return stroka


def ubrat(db: Session, deal_id: int, line_id: int) -> None:
    deal = _otkrytaya(db, deal_id)
    lines_repo.drop(db, _stroka(db, deal_id, line_id))
    pereschitat_summu(db, deal)


def pereschitat_summu(db: Session, deal: Deal) -> None:
    """Единственный писатель `deals.amount`, пока у заявки есть строки.

    Итог ХРАНИТСЯ, а не считается на каждый запрос: его суммируют шесть запросов
    отчётов и сводки, и `JOIN` со строками замедлил бы их все. Это сознательное
    отступление от правила «производное не хранится», утверждённое владельцем
    30.08.2026; сторожит его тест-инвариант «amount == SUM(строки)». Разбор и
    отвергнутый путь — `docs/19-sborka-zakaza.md` §Р5.

    Убрали последнюю строку — сумма снова становится «не назвали» (NULL), а не
    нулём: ноль означал бы «отдаём бесплатно».
    """
    deal.amount = lines_repo.sum_for_deal(db, deal.id)
    deal.updated_at = now_utc()
    db.flush()


def stroka_out(stroka: DealLine) -> dict:
    """Строка для ответа API.

    `kind` и `total_minor` считаются здесь и в базе не лежат: иначе каждый
    клиент API выводил бы их сам, и половина ошиблась бы с тысячными.
    """
    summa = (
        None
        if stroka.price_minor is None
        else stroka.price_minor * stroka.quantity_milli // 1000
    )
    return {
        "id": stroka.id,
        "product_id": stroka.product_id,
        "warehouse_id": stroka.warehouse_id,
        "name": stroka.name_snapshot,
        "quantity_milli": stroka.quantity_milli,
        "price_minor": stroka.price_minor,
        "cost_minor": stroka.cost_minor,
        "total_minor": summa,
        "kind": "extra" if stroka.product_id is None else "product",
        "sort_order": stroka.sort_order,
    }


def s_nehvatkoy(db: Session, stroki: list[DealLine]) -> list[dict]:
    """Строки для ответа вместе с нехваткой по каждой.

    Наличие спрашивается ОДНИМ запросом на все товары списка, а не по запросу на
    строку: заявка на два десятка позиций иначе била бы в базу два десятка раз
    ради одной таблички на экране.

    Нехватка — предупреждение, а не отказ (см. `reserve_service.nehvatka`).
    """
    tovary = [s.product_id for s in stroki if s.product_id is not None]
    est = reserve_service.availability(db, tovary) if tovary else {}
    otvet = []
    for stroka in stroki:
        kusok = stroka_out(stroka)
        dostupno = est.get(stroka.product_id or 0, {}).get("available_milli")
        kusok["shortage_milli"] = (
            -dostupno if dostupno is not None and dostupno < 0 else 0
        )
        otvet.append(kusok)
    return otvet


def spisat_pri_zakrytii(db: Session, deal: Deal, author: User | None) -> int:
    """Списать со склада то, что заявка обещала и что ещё не ушло. Вернуть число
    списанных позиций.

    Сколько списывать (`docs/19-sborka-zakaza.md` §Р4):

        к списанию = в строках заявки − уже списанное движениями с этим deal_id

    Формула САМА делает действие повторяемым: закрыли, откатили этап, закрыли
    снова — второй раз списывать нечего. Это не защита «на всякий случай»:
    откат этапа делают руками каждый день, а движение склада не отменяется
    удалением — только обратным движением.

    Уже отгруженное заказом вычитается тем же слагаемым: движения накладной
    несут `deal_id` заявки, и повторно списывать по ним нечего.

    Услуги пропускаются: остатка у них нет и быть не может.
    """
    stroki = [s for s in lines_repo.list_for_deal(db, deal.id) if s.product_id is not None]
    if not stroki:
        return 0

    nuzhno: dict[int, int] = {}
    sklady: dict[int, int | None] = {}
    for stroka in stroki:
        nuzhno[stroka.product_id] = nuzhno.get(stroka.product_id, 0) + stroka.quantity_milli
        sklady.setdefault(stroka.product_id, stroka.warehouse_id)

    spisano = warehouse_repo.spisano_po_zayavkam(db, list(nuzhno))
    po_umolchaniyu = None
    spisano_pozitsiy = 0

    for product_id, kolichestvo in nuzhno.items():
        ostalos = kolichestvo - spisano.get((deal.id, product_id), 0)
        if ostalos <= 0:
            continue
        tovar = warehouse_repo.get_product(db, product_id, include_deleted=True)
        if tovar is None or tovar.is_service:
            continue
        sklad = sklady.get(product_id)
        if sklad is None:
            # Склад у строки не назван — берём основной (решение владельца
            # 30.08.2026). У большинства склад один, и вопрос «с какого» для них
            # не существует; в самом движении склад записан, так что молчаливым
            # решение остаётся только на входе.
            if po_umolchaniyu is None:
                po_umolchaniyu = warehouse_service.default_warehouse(db)
            sklad = po_umolchaniyu.id
        warehouse_service.add_move(
            db,
            {
                "product_id": product_id,
                "kind": MOVE_OUT,
                "quantity": warehouse_service.format_quantity(ostalos),
                "warehouse_id": sklad,
                "deal_id": deal.id,
                "comment": f"written off on closing deal {deal.id}",
            },
            author,
        )
        spisano_pozitsiy += 1
    return spisano_pozitsiy


def _otkrytaya(db: Session, deal_id: int) -> Deal:
    """Заявка, в которой ещё можно менять строки.

    У закрытой строки заморожены: по ним уже списан товар и посчитана прибыль,
    и правка задним числом развела бы отчёт со складом.
    """
    deal = get_deal(db, deal_id)
    if deal.closed_at is not None:
        raise errors.ValidationError("The deal is closed", code="deal_closed")
    return deal


def _stroka(db: Session, deal_id: int, line_id: int) -> DealLine:
    stroka = lines_repo.get(db, deal_id, line_id)
    if stroka is None:
        raise errors.NotFoundError("Line not found", code="line_not_found")
    return stroka


def _kolichestvo(value) -> int:
    kolichestvo = warehouse_service.parse_quantity(value)
    if kolichestvo is None or kolichestvo <= 0:
        raise errors.ValidationError(
            "Quantity must be positive", code="quantity_not_positive"
        )
    return kolichestvo


def _nazvanie(value) -> str:
    nazvanie = (value or "").strip()
    if not nazvanie:
        raise errors.ValidationError("Name is required", code="name_required")
    if len(nazvanie) > MAX_NAME:
        raise errors.ValidationError(
            f"Name is too long (max {MAX_NAME} characters)", code="name_too_long"
        )
    return nazvanie


def _tovar(db: Session, data: dict):
    """Товар строки: по номеру записи или по артикулу. None — своя трата.

    Артикул принимается наравне с `product_id` ради магазина: он знает артикул,
    которым торгует, а наших внутренних номеров не знает вовсе.
    """
    sku = (data.get("sku") or "").strip()
    product_id = data.get("product_id")
    if not sku and not product_id:
        return None
    tovar = (
        warehouse_repo.get_product(db, product_id)
        if product_id
        else warehouse_repo.get_by_sku(db, sku)
    )
    if tovar is None:
        raise errors.NotFoundError("Product not found", code="product_not_found")
    if tovar.deleted_at is not None:
        raise errors.ValidationError("Product is deleted", code="product_deleted")
    return tovar
