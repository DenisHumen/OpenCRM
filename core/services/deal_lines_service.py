"""Строки заявки: товары со склада и свои траты.

Отдельным модулем, а не внутри `deal_service`: строке нужен и товар, и заявка, а
`warehouse_service` уже зовёт `deal_service.parse_money` — сложить это в один
модуль значит замкнуть ввоз в кольцо.

Что за строка — не хранится, а выводится из `product_id` (разбор:
`docs/19-sborka-zakaza.md` §Р2).
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    barcode_service,
    modules_service,
    reserve_service,
    warehouse_service,
)
from database.models.document import KIND_SALES_ORDER, OPEN_ORDER_STATUSES
from database.repositories import documents as documents_repo
from core.services.deal_service import MAX_MONEY, get_deal, parse_money
from core.utils import now_utc
from database.models import Deal, DealLine, User
from database.repositories import deal_lines as lines_repo
from database.models.warehouse import MOVE_OUT
from database.repositories import warehouse as warehouse_repo

#: Ширина `deal_lines.name_snapshot`, взятая у самой колонки. Длиннее — отказ, а
#: не обрезка: обрезанное название уходит в счёт клиенту, и заметят это не здесь.
MAX_NAME = DealLine.__table__.c.name_snapshot.type.length


def spisok(db: Session, deal_id: int) -> list[DealLine]:
    get_deal(db, deal_id)
    return lines_repo.list_for_deal(db, deal_id)


def itog(db: Session, deal_id: int) -> int | None:
    """Итог заявки по строкам. None — строк нет: сумму никто не называл."""
    return lines_repo.sum_for_deal(db, deal_id)


def pribyl(db: Session, deal_id: int) -> tuple[int | None, int | None]:
    """Себестоимость по строкам и ожидаемая прибыль. None — считать не из чего.

    Прибыль отдаётся ТОЛЬКО когда себестоимость известна у всех строк: сложив
    то, что есть, мы показали бы её выше настоящей — там, где по ней решают о
    скидке.
    """
    itog_strok = lines_repo.sum_for_deal(db, deal_id)
    sebes, izvestna = lines_repo.sebestoimost_zayavki(db, deal_id)
    if not izvestna:
        return None, None
    return sebes, None if itog_strok is None else itog_strok - sebes


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
            warehouse_id=_sklad(db, data, tovar),
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
    if "warehouse_id" in data:
        stroka.warehouse_id = _sklad(db, data, _tovar_stroki(db, stroka))
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
    itog_strok = lines_repo.sum_for_deal(db, deal.id)
    # Потолок тот же, что у введённой руками суммы, и по той же причине:
    # `deals.amount` — INT, а MySQL на переполнении отвечает 1264, обработчика
    # на этот класс нет, и человек получает пятисотку без подсказки. Каждый
    # сомножитель в своём пределе, а произведение — уже нет.
    if itog_strok is not None and abs(itog_strok) > MAX_MONEY:
        raise errors.ValidationError(
            "The lines total is too large", code="deal_amount_too_big"
        )
    deal.amount = itog_strok
    deal.updated_at = now_utc()
    db.flush()


def stroka_out(stroka: DealLine, amounts: bool = True) -> dict:
    """Строка для ответа API.

    `kind` и `total_minor` считаются здесь и в базе не лежат: иначе каждый
    клиент API выводил бы их сам, и половина ошиблась бы с тысячными.

    `amounts=False` — у смотрящего нет права `deals.view_amounts`. Ключи
    остаются на месте пустыми, а не исчезают: форма ответа не должна зависеть
    от того, кто спрашивает. Себестоимость закрывается тем же правом, что цена,
    — просьба «менеджер ведёт заявку, но не видит её маржу» обходится этим
    разделом, если о праве здесь забыть.
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
        "price_minor": stroka.price_minor if amounts else None,
        "cost_minor": stroka.cost_minor if amounts else None,
        "total_minor": summa if amounts else None,
        "kind": "extra" if stroka.product_id is None else "product",
        "sort_order": stroka.sort_order,
    }


def s_nehvatkoy(db: Session, stroki: list[DealLine], amounts: bool = True) -> list[dict]:
    """Строки для ответа вместе с нехваткой по каждой.

    Наличие спрашивается ОДНИМ запросом на все товары списка, а не по запросу на
    строку: заявка на два десятка позиций иначе била бы в базу два десятка раз
    ради одной таблички на экране.

    Нехватка — предупреждение, а не отказ: продавать то, что ещё едет, —
    обычное дело, и запрет сломал бы работу вместо помощи.
    """
    tovary = [s.product_id for s in stroki if s.product_id is not None]
    est = reserve_service.availability(db, tovary) if tovary else {}
    otvet = []
    for stroka in stroki:
        kusok = stroka_out(stroka, amounts)
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

        к списанию = в строках − уже ушедшее под заявку − непогашенное её заказами

    Первое вычитаемое делает действие повторяемым: закрыли, откатили этап,
    закрыли снова — второй раз списывать нечего. Откат этапа делают руками
    каждый день, а движение склада не отменяется удалением, только обратным.

    Второе не даёт списать то, что ещё отгрузит открытый заказ: иначе закрытие
    заявки и последующая накладная по её заказу вынесли бы со склада вдвое
    больше, чем в строках, и заметили бы это на инвентаризации.

    Услуги пропускаются: остатка у них нет и быть не может.
    """
    stroki = [s for s in lines_repo.list_for_deal(db, deal.id) if s.product_id is not None]
    if not stroki:
        return 0

    # Группируем по ПАРЕ (товар, склад): один товар может стоять в двух строках
    # с разных складов, и общая куча списала бы всё с первого попавшегося.
    nuzhno: dict[tuple[int, int | None], int] = {}
    for stroka in stroki:
        klyuch = (stroka.product_id, stroka.warehouse_id)
        nuzhno[klyuch] = nuzhno.get(klyuch, 0) + stroka.quantity_milli

    tovary = {product_id for product_id, _ in nuzhno}
    spisano = warehouse_repo.spisano_po_zayavkam(db, list(tovary))
    peredano = (
        documents_repo.zakazano_po_zayavkam(
            db, KIND_SALES_ORDER, OPEN_ORDER_STATUSES, list(tovary)
        )
        if modules_service.is_enabled(db, "orders")
        else {}
    )

    po_umolchaniyu = None
    spisano_pozitsiy = 0
    # Вычитаемые считаны на товар целиком, а списываем по складам — держим
    # остаток вычитаемого и тратим его по мере обхода, иначе каждая пара
    # вычитала бы одно и то же ещё раз.
    ostatok_vychetov = {
        product_id: spisano.get((deal.id, product_id), 0)
        + peredano.get((deal.id, product_id), 0)
        for product_id in tovary
    }

    for (product_id, sklad), kolichestvo in nuzhno.items():
        vychet = min(ostatok_vychetov.get(product_id, 0), kolichestvo)
        ostatok_vychetov[product_id] = ostatok_vychetov.get(product_id, 0) - vychet
        ostalos = kolichestvo - vychet
        if ostalos <= 0:
            continue
        tovar = warehouse_repo.get_product(db, product_id, include_deleted=True)
        if tovar is None or tovar.is_service:
            continue
        if sklad is None:
            # Склад у строки не назван — берём основной (решение владельца
            # 30.08.2026): у большинства он один. В самом движении склад записан.
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
            # Товар мог быть удалён из справочника уже после набора строки.
            # Без этого закрытие заявки отвечало бы 404, и закрыть её было бы
            # нельзя ничем: подписчик — `participant`, он валит смену этапа.
            allow_deleted=True,
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


def _sklad(db: Session, data: dict, tovar) -> int | None:
    """С какого склада берём товар строки. None — брать неоткуда и не надо.

    У своей траты и у услуги склада нет по существу: упаковку не берут с полки,
    а выезд мастера не лежит нигде. Присланный для них склад — это ошибка
    звонящего, и молчаливо принять его значит однажды объяснять, почему списание
    ушло не с того места.

    Не назвали — оставляем пусто, а не подставляем основной ЗДЕСЬ: склад по
    умолчанию берётся в момент списания (§Р4). Записать его в строку заранее
    значит соврать о выборе, которого человек не делал, — и оставить старый
    склад в строке, если основной потом сменят.
    """
    if "warehouse_id" not in data or data["warehouse_id"] is None:
        return None
    if tovar is None or tovar.is_service:
        # Скан — исключение, и одно. Что за кодом, звонящий знать не может, а
        # склад он шлёт не для ЭТОЙ строки, а как выбранный на экране. Услуге
        # склад просто не нужен — отказывать сканеру не за что. Названный руками
        # товар другое дело: там склад выбрали строке, которой он не положен.
        if tovar is not None and data.get("code"):
            return None
        raise errors.ValidationError(
            "This line takes nothing from a warehouse", code="line_has_no_warehouse"
        )
    return warehouse_service.get_warehouse(db, data["warehouse_id"]).id


def _tovar_stroki(db: Session, stroka: DealLine):
    if stroka.product_id is None:
        return None
    return warehouse_repo.get_product(db, stroka.product_id, include_deleted=True)


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
    """Товар строки: по номеру записи, артикулу или штрихкоду. None — своя трата.

    Три способа, и каждый из-за своего звонящего. `product_id` — экран, где
    товар уже выбран из подсказки. `sku` — магазин: он знает артикул, которым
    торгует, а наших внутренних номеров не знает вовсе. `code` — сканер у
    стойки: набирать название, когда коробка в руках, никто не станет.

    Скан отвечает отказом `barcode_unknown` с самим кодом внутри: пустой ответ
    после писка сканера читается как «сканер сломался» (`barcode_service.scan`).
    """
    code = (data.get("code") or "").strip()
    if code:
        return barcode_service.scan(db, code)
    sku = (data.get("sku") or "").strip()
    product_id = data.get("product_id")
    if not sku and not product_id:
        return None
    tovar = (
        # Вместе с удалёнными: «товар удалён» и «нет такого товара» — разные
        # беды, и человеку нужно знать, какая из них.
        warehouse_repo.get_product(db, product_id, include_deleted=True)
        if product_id
        else warehouse_repo.get_by_sku(db, sku)
    )
    if tovar is None:
        raise errors.NotFoundError("Product not found", code="product_not_found")
    if tovar.deleted_at is not None:
        raise errors.ValidationError("Product is deleted", code="product_deleted")
    return tovar
