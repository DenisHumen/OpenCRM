"""Заказы: перечень позиций, сборка сканером, отгрузка и приёмка.

Роутер закрыт `require_module("orders")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отвечать тому, кто
помнит адрес.

Заказ — вид бланка, поэтому номер, статусы, печать и поиск сканом живут в
`documents`; здесь только то, чего у квитанции нет.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import (
    codes,
    document_service,
    modules_service,
    order_service,
    permissions_service,
)
from core.services import settings_service
from database.models import User
from database.models.document import DOCUMENT_LOCALES, ORDER_KINDS, WAYBILL_KINDS
from database.repositories import documents as documents_repo
from web.api import schemas
from web.api.deps import MAX_SEARCH, get_db, require_module, require_perm
from web.public import routes as public_routes

router = APIRouter(
    prefix="/orders",
    tags=["orders"],
    dependencies=[Depends(require_module("orders"))],
)


class OrderIn(BaseModel):
    """Заказ на входе. Клиента можно назвать номером, почтой или именем.

    Три поля вместо одного `client_id`, потому что у стойки его негде взять:
    покупатель называет телефон, а не номер записи в базе. Дальше `order_service`
    сам решает, привязать найденную карточку или завести новую, и на два
    совпадения отвечает отказом со списком кандидатов, а не догадкой.
    """

    kind: str
    client_id: int | None = None
    deal_id: int | None = None
    company_id: int | None = None
    client_name: str | None = None
    client_phone: str | None = None
    client_email: str | None = None
    #: «Всё равно завести нового». Приходит вторым запросом после отказа
    #: `client_ambiguous` — когда человек посмотрел кандидатов и решил, что ни
    #: один из них не тот. Молчаливым умолчанием быть не может: тогда отказ
    #: перестал бы что-либо останавливать.
    client_create_new: bool = False
    locale: str | None = None
    note: str | None = None


class LineIn(BaseModel):
    # Пусто — разовая позиция без карточки товара («доставка», «упаковка»).
    product_id: int | None = None
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class LinePatchIn(BaseModel):
    name: str | None = None
    quantity: str | int | float | None = None
    price: int | None = None


class PickIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    # Сколько добавить за один писк. Обычно одна штука; коробка со своим кодом
    # приносит столько, сколько в ней лежит (`pack_size_milli` штрихкода).
    quantity_milli: int = Field(default=1000, gt=0)


class CloseIn(BaseModel):
    # Склад выбирается явно: молчаливое списание с основного однажды снимет
    # деталь не оттуда, где её взяли.
    warehouse_id: int | None = None
    # Явное согласие отгрузить больше, чем лежит. По умолчанию отгрузка при
    # нехватке останавливается — отгрузить нечего физически.
    confirm_negative: bool = False


@router.get("")
def list_orders(
    search: str | None = Query(default=None, max_length=MAX_SEARCH),
    kind: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    """Список заказов. Категория здесь — СОСТОЯНИЕ, а не вид.

    Вид у заказа выбирается чипами и их всего два (покупателю, поставщику), а
    вопрос «что делать сейчас» задаёт состояние: новые собирают, готовые
    отгружают, закрытые не трогают. Поэтому счёт категорий здесь по статусам, а
    у бланков — по видам: там шесть видов вперемешку и различить их нечем.
    """
    if sort and sort not in documents_repo.PORYADKI:
        raise errors.ValidationError(
            f"Unknown sort: {sort}. Known: {', '.join(sorted(documents_repo.PORYADKI))}",
            code="unknown_sort",
        )
    kinds = (kind,) if kind in ORDER_KINDS else ORDER_KINDS
    items, total = documents_repo.search(
        db, q=search, status=status, client_id=client_id, deal_id=deal_id,
        kinds=kinds, sort=sort, page=page, per_page=per_page,
    )
    # Строки — одним запросом на страницу, а не запросом на строку списка:
    # сумма заказа складывается из них, и без них список молчит о деньгах.
    rows = documents_repo.lines_by_documents(db, [item.id for item in items])
    amounts = permissions_service.sees_amounts(db, user, "orders")
    otvet = schemas.paginated(
        [schemas.order_out(item, rows.get(item.id, []), amounts=amounts) for item in items],
        total, page, per_page,
    )
    # Счёт по состояниям — БЕЗ отбора по состоянию: иначе, свернув «закрытые»,
    # человек потерял бы и число рядом с ними, то есть способ их вернуть.
    otvet["counts"] = documents_repo.schyot_po_statusam(
        db, q=search, client_id=client_id, deal_id=deal_id, kinds=kinds
    )
    return otvet


@router.post("", status_code=201)
def create_order(
    payload: OrderIn,
    user: User = Depends(require_perm("orders", "create")),
    db: Session = Depends(get_db),
):
    order, client_created = order_service.create(db, payload.model_dump(), user)
    # «Заведён новый клиент» — часть ответа, а не догадка экрана по тому, был ли
    # `client_id` в запросе: карточку могли и найти по номеру.
    return {
        **schemas.order_out(order, [], amounts=permissions_service.sees_amounts(db, user, "orders")),
        "client_created": client_created,
    }


@router.get("/{order_id}")
def get_order(
    order_id: int,
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    order = order_service.get(db, order_id)
    data = schemas.order_out(
        order,
        order_service.lines(db, order.id),
        amounts=permissions_service.sees_amounts(db, user, "orders"),
    )
    # Бумаги, выписанные по этому заказу. Закрытие теперь выписывает
    # накладную, и человек обязан видеть КАКУЮ — иначе бумага есть, а найти
    # её можно только глазами по всему списку накладных.
    #
    # Пусто у заказов, закрытых до переезда: задним числом накладные им не
    # выписываются, и показывать выдуманный номер вместо честной пустоты
    # нельзя.
    #
    # При выключенном блоке накладных списка нет вовсе — не «пустой», а
    # именно нет: выключенный блок исчезает целиком, включая упоминания о
    # себе в чужих ответах.
    if modules_service.is_enabled(db, "waybills"):
        bumagi, _vsego = documents_repo.search(
            db, basis_id=order.id, kinds=WAYBILL_KINDS, page=1, per_page=50
        )
        data["waybills"] = [
            {
                "id": w.id,
                "number": w.number,
                "kind": w.kind,
                "status": w.status,
            }
            for w in bumagi
        ]
    return data


@router.post("/{order_id}/lines", status_code=201)
def add_line(
    order_id: int,
    payload: LineIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    line = order_service.add_line(db, order_id, payload.model_dump(), user)
    return schemas.order_line_out(line, amounts=permissions_service.sees_amounts(db, user, "orders"))


@router.patch("/{order_id}/lines/{line_id}")
def update_line(
    order_id: int,
    line_id: int,
    payload: LinePatchIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    line = order_service.update_line(db, order_id, line_id, payload.model_dump(exclude_unset=True))
    return schemas.order_line_out(line, amounts=permissions_service.sees_amounts(db, user, "orders"))


@router.delete("/{order_id}/lines/{line_id}")
def remove_line(
    order_id: int,
    line_id: int,
    _: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    order_service.remove_line(db, order_id, line_id)
    return {"message": "Line removed"}


class DealLinkIn(BaseModel):
    """К какой заявке цепляем заказ. `null` — отцепить."""

    deal_id: int | None = None


@router.post("/{order_id}/deal")
def link_deal(
    order_id: int,
    payload: DealLinkIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    """Прицепить заказ к заявке или отцепить.

    Отдельной ручкой, а не общим PATCH: у заказа правится только эта связь —
    позиции меняются своими ручками, а номер и вид не меняются вовсе.
    """
    order = order_service.prikrepit_k_zayavke(
        db, order_id, payload.deal_id, permissions_service.deals_scope(db, user)
    )
    return schemas.order_out(
        order,
        order_service.lines(db, order.id),
        amounts=permissions_service.sees_amounts(db, user, "orders"),
    )


@router.post("/{order_id}/pick")
def pick(
    order_id: int,
    payload: PickIn,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    """Отметить позицию собранной по отсканированному коду.

    Собранное живёт отдельно от заказанного: расхождение «заказано пять, собрано
    четыре» видно построчно до отгрузки, а не на выдаче.
    """
    line = order_service.pick(db, order_id, payload.code, payload.quantity_milli)
    return schemas.order_line_out(line, amounts=permissions_service.sees_amounts(db, user, "orders"))


@router.post("/{order_id}/ready")
def mark_ready(
    order_id: int,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    order = order_service.mark_ready(db, order_id, user)
    return schemas.order_out(
        order, order_service.lines(db, order.id), amounts=permissions_service.sees_amounts(db, user, "deals")
    )


@router.post("/{order_id}/close")
def close_order(
    order_id: int,
    payload: CloseIn,
    user: User = Depends(require_perm("orders", "issue")),
    db: Session = Depends(get_db),
):
    """Отгрузить заказ покупателя или принять заказ поставщику.

    Право отдельное (`issue`, как выпуск бланка): набирать позиции и двигать
    склад — разные полномочия. Сборщик набирает, отгружает старший.
    """
    order = order_service.close(
        db, order_id, user,
        warehouse_id=payload.warehouse_id,
        confirm_negative=payload.confirm_negative,
    )
    return schemas.order_out(
        order, order_service.lines(db, order.id), amounts=permissions_service.sees_amounts(db, user, "orders")
    )


@router.post("/{order_id}/revert")
def revert_order(
    order_id: int,
    user: User = Depends(require_perm("orders", "issue")),
    db: Session = Depends(get_db),
):
    """Отменить проведение обратными движениями. Прежние остаются на месте."""
    order = order_service.revert(db, order_id, user)
    return schemas.order_out(
        order, order_service.lines(db, order.id), amounts=permissions_service.sees_amounts(db, user, "orders")
    )


@router.post("/{order_id}/cancel")
def cancel_order(
    order_id: int,
    user: User = Depends(require_perm("orders", "edit")),
    db: Session = Depends(get_db),
):
    """Отменить непроведённый заказ. Резерв снимется сам — он не хранится."""
    order = order_service.cancel(db, order_id, user)
    return schemas.order_out(
        order, order_service.lines(db, order.id), amounts=permissions_service.sees_amounts(db, user, "orders")
    )


#: Подписи печатной формы. Отдельно от квитанции: у заказа таблица позиций и
#: итог, у квитанции — описание одной вещи, и общий словарь пришлось бы делить
#: на две половины прямо в шаблоне.
PRINT_STRINGS = {
    "ru": {
        "title": "Заказ", "party": "Кому", "item": "Позиция", "qty": "Кол-во",
        "price": "Цена", "sum": "Сумма", "total": "Итого",
        "gave": "Отпустил", "took": "Получил", "print": "Печать",
    },
    "en": {
        "title": "Order", "party": "To", "item": "Item", "qty": "Qty",
        "price": "Price", "sum": "Amount", "total": "Total",
        "gave": "Issued by", "took": "Received by", "print": "Print",
    },
    "uk": {
        "title": "Замовлення", "party": "Кому", "item": "Позиція", "qty": "К-сть",
        "price": "Ціна", "sum": "Сума", "total": "Разом",
        "gave": "Відпустив", "took": "Отримав", "print": "Друк",
    },
}


@router.get("/{order_id}/print", response_class=HTMLResponse)
def print_order(
    order_id: int,
    locale: str | None = None,
    user: User = Depends(require_perm("orders", "view")),
    db: Session = Depends(get_db),
):
    """Печатная форма заказа: таблица позиций и итог.

    Форма отличается от квитанции по существу: квитанция отвечает на вопрос
    «что вы у меня взяли», заказ — «что и почём мне отдадут». Печатает браузер,
    как и всё остальное: сервер на VPS, принтер на столе.

    Суммы показываем всегда: тот, кто печатает заказ клиенту, обязан видеть
    цены — иначе бумага уйдёт с пустым столбцом. Право `orders.view_amounts`
    закрывает закупочную цену в интерфейсе, а не отпускную в бумаге.
    """
    order = order_service.get(db, order_id)
    rows = order_service.lines(db, order.id)
    lang = locale if locale in DOCUMENT_LOCALES else order.locale

    currency = settings_service.get_all(db).get("currency", "USD")
    payload = document_service.payload_of(order)
    lines = [
        {
            "name": line.name_snapshot,
            "quantity": _quantity(line.quantity_milli),
            "price": _money(line.price_minor, currency),
            "sum": _money(summa, currency),
        }
        for line, summa in zip(rows, document_service.line_totals(rows))
    ]

    html = public_routes.templates.get_template("order_print.html").render(
        doc=order,
        locale=lang,
        t=PRINT_STRINGS[lang],
        company=payload.get("company"),
        # Кому адресован заказ. У покупателя это клиент, у поставщика — имя,
        # подставленное при заведении: поставщик отдельной сущностью в системе
        # не заведён, и оба случая читаются из одного и того же снимка.
        party=(payload.get("client") or {}).get("name"),
        created=order.created_at.strftime("%d.%m.%Y %H:%M") if order.created_at else "",
        lines=lines,
        total=_money(order_service.total_minor(rows), currency),
        barcode=codes.barcode_svg(order.number),
    )
    return HTMLResponse(html)


# `_line_sum` отсюда убран. Он был ВТОРЫМ местом, где считаются деньги, — ровно
# тем, от чего предостерегает докстрока модели `DocumentLine`, — и успел
# разойтись с первым дважды. Во-первых, он округлял на каждой строке, тогда как
# итог округляется один раз, и колонка «Сумма» не складывалась в «Итого».
# Во-вторых, у него не было ветки для отрицательных: строка со скидочной ценой
# округлялась в колонке в одну сторону, а в итоге в другую.
#
# Теперь суммы строк берутся из `document_service.line_totals` — общим счётом,
# нарастающим итогом, так что колонка складывается в итог тождественно.


def _money(minor: int | None, currency: str) -> str:
    if minor is None:
        return ""
    whole, cents = divmod(abs(int(minor)), 100)
    return f"{'-' if minor < 0 else ''}{whole}.{cents:02d} {currency}"


def _quantity(milli: int) -> str:
    from core.services import warehouse_service

    return warehouse_service.format_quantity(milli)
