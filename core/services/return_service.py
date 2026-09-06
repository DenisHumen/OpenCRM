"""Возвраты покупателя: бумага, склад, деньги.

Отмены проведения у заказа больше нет — решение владельца 05.09.2026:
проведённый заказ это свершившееся, и назад дорога одна — возврат. Человек
заполняет, что вернули и почему, прикладывает фото, называет сумму; проведение
возвращает товар на склад и деньги клиенту. Разбор — `docs/bloki/22-vozvraty.md`.

Возврат — вид бумаги (`KIND_RETURN`) в той же таблице: номер, история, строки
и удаление достаются от бланка готовыми. Основание — проведённый заказ
покупателя, строки — только его товары, и не больше, чем отгружено.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import events as event_bus
from core import exceptions as errors
from core.security import tokens
from core.services import (
    audit_service,
    client_service,
    document_service,
    modules_service,
    warehouse_service,
    waybill_service,
)
from database.models import Document, DocumentEvent, DocumentFile, DocumentLine, User
from database.models.audit import SOURCE_MANUAL
from database.models.finance import MAX_AMOUNT_MINOR
from database.models.document import (
    KIND_RETURN,
    KIND_SALES_ORDER,
    KIND_WAYBILL_IN,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_DRAFT,
)
from database.models.warehouse import MOVE_RETURN
from database.repositories import clients as clients_repo
from database.repositories import documents as documents_repo

#: Возврат проведён: товар вернулся на склад, деньги — клиенту.
#:
#: Подробности: `vozvrat`, `order`, `lines`, `category_id` (статья денег,
#: названная человеком), `waybill` (приходная, если её выписал склад).
#:
#: Поднимается после условной смены статуса и после движений склада, до записи
#: в историю бумаги — по тем же трём доводам, что у `order_service.ORDER_CLOSED`.
RETURN_POSTED = "return.posted"

#: Что можно приложить к возврату: фото и видео. Договоры и таблицы — к
#: клиенту, а не к бумаге: у возврата спрашивают «как выглядела вещь».
VLOZHENIYA = {"jpg", "jpeg", "png", "webp", "gif", "mp4", "webm", "mov"}



# --- чтение -------------------------------------------------------------------


def get(db: Session, document_id: int) -> Document:
    vozvrat = document_service.get(db, document_id)
    if vozvrat.kind != KIND_RETURN:
        raise errors.NotFoundError("Return not found", code="return_not_found")
    return vozvrat


def lines(db: Session, document_id: int) -> list[DocumentLine]:
    return documents_repo.lines_of(db, document_id)


def files(db: Session, document_id: int) -> list[DocumentFile]:
    return documents_repo.files_of(db, document_id)


def events(db: Session, document_id: int) -> list[DocumentEvent]:
    return documents_repo.events(db, document_id)


def po_zakazu(db: Session, order_id: int) -> list[Document]:
    return documents_repo.vozvraty_po_zakazu(db, order_id)


def zakaz(db: Session, vozvrat: Document) -> Document:
    order = documents_repo.get(db, vozvrat.basis_id) if vozvrat.basis_id else None
    if order is None:
        raise errors.NotFoundError("The order of this return is gone", code="order_not_found")
    return order


def payload_of(vozvrat: Document) -> dict:
    return json.loads(vozvrat.payload or "{}")


def dostupno(db: Session, order: Document, krome_id: int | None = None) -> dict[int, int]:
    """Сколько каждого товара ещё можно вернуть: отгружено минус уже вернулось.

    Считается запросом на каждый вопрос, а не хранится: остаток «к возврату»
    — производное, как и остаток склада. Услуги в счёт не идут — на склад им
    возвращаться нечем.
    """
    otgruzheno: dict[int, int] = {}
    for row in documents_repo.lines_of(db, order.id):
        if row.product_id is None:
            continue
        if warehouse_service.get_product(db, row.product_id, include_deleted=True).is_service:
            continue
        otgruzheno[row.product_id] = otgruzheno.get(row.product_id, 0) + row.quantity_milli
    vernulos = documents_repo.vozvrashcheno_po_zakazu(db, order.id, krome_id=krome_id)
    # Сторно накладной заказа — тоже возврат товара, только бумагой склада.
    storno = documents_repo.stornirovano_po_zakazu(db, order.id)
    return {
        product_id: max(0, milli - vernulos.get(product_id, 0) - storno.get(product_id, 0))
        for product_id, milli in otgruzheno.items()
    }


# --- заведение и правка -------------------------------------------------------


def sozdat(db: Session, order_id: int, author: User) -> Document:
    """Завести черновик возврата по проведённому заказу покупателя.

    Строки заполняются тем, что ещё можно вернуть, и человек убирает лишнее:
    возврат «всё, кроме одной позиции» — правка одной строки, а не набор всех
    остальных заново. Сумма по умолчанию — цена этих строк; её правят.
    """
    order = document_service.get(db, order_id)
    if order.kind != KIND_SALES_ORDER:
        raise errors.ValidationError(
            "Only a sales order can be returned", code="not_a_sales_order"
        )
    if order.status != STATUS_CLOSED:
        raise errors.ValidationError(
            "Only a processed order can be returned", code="order_not_closed"
        )
    mozhno = dostupno(db, order)
    if not any(milli > 0 for milli in mozhno.values()):
        raise errors.ValidationError(
            "Nothing is left to return by this order", code="nothing_to_return"
        )
    snimok = document_service.payload_of(order)
    payload = {
        "client": snimok.get("client") or {"name": "", "phone": "", "email": ""},
        "company": snimok.get("company") or {},
        "deal": snimok.get("deal") or {},
        "order": {"id": order.id, "number": order.number},
        "note": "",
        "category_id": None,
    }
    vozvrat = document_service._insert_with_free_number(
        db,
        status=STATUS_DRAFT,
        kind=KIND_RETURN,
        locale=order.locale,
        client_id=order.client_id,
        deal_id=order.deal_id,
        basis_id=order.id,
        payload=json.dumps(payload, ensure_ascii=False),
        created_by=author.id,
    )
    stroki: list[DocumentLine] = []
    for row in documents_repo.lines_of(db, order.id):
        ostalos = mozhno.get(row.product_id or 0, 0) if row.product_id else 0
        if ostalos <= 0:
            continue
        milli = min(row.quantity_milli, ostalos)
        mozhno[row.product_id] = ostalos - milli
        stroki.append(
            documents_repo.add_line(
                db,
                DocumentLine(
                    document_id=vozvrat.id,
                    product_id=row.product_id,
                    name_snapshot=row.name_snapshot,
                    quantity_milli=milli,
                    price_minor=row.price_minor,
                    cost_minor=None,
                    sort_order=row.sort_order,
                ),
            )
        )
    vozvrat.refund_minor = document_service.total_minor(stroki)
    _zapisat_perehod(db, vozvrat, "", STATUS_DRAFT, author, f"for order {order.number}")
    return vozvrat


def pravit(db: Session, document_id: int, data: dict) -> Document:
    """Описание, сумма, клиент и статья денег — пока черновик."""
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat)
    payload = payload_of(vozvrat)
    if "note" in data:
        payload["note"] = str(data.get("note") or "").strip()[:2000]
    if "category_id" in data:
        payload["category_id"] = int(data["category_id"]) if data.get("category_id") else None
    if "client_id" in data:
        client_id = data.get("client_id")
        if client_id is None:
            vozvrat.client_id = None
            payload["client"] = {"name": "", "phone": "", "email": ""}
        else:
            client = clients_repo.get(db, int(client_id))
            if client is None:
                raise errors.NotFoundError("Client not found", code="client_not_found")
            vozvrat.client_id = client.id
            payload["client"] = {
                "name": client.name, "phone": client.phone or "", "email": client.email or "",
            }
    if "refund" in data:
        vozvrat.refund_minor = _summa(data.get("refund"))
    vozvrat.payload = json.dumps(payload, ensure_ascii=False)
    db.flush()
    return vozvrat


def _summa(value) -> int:
    if isinstance(value, bool) or value is None or value == "":
        raise errors.ValidationError("refund must be a whole number of minor units", code="bad_money")
    try:
        summa = int(value)
    except (TypeError, ValueError):
        raise errors.ValidationError("refund must be a whole number of minor units", code="bad_money") from None
    if summa < 0:
        raise errors.ValidationError("refund cannot be negative", code="negative_money")
    if summa > MAX_AMOUNT_MINOR:
        raise errors.ValidationError("refund is too large", code="money_too_large")
    return summa


def add_line(db: Session, document_id: int, data: dict, author: User) -> DocumentLine:
    """Строка возврата — только товар из заказа. Цена — та, по которой отгрузили."""
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat)
    order = zakaz(db, vozvrat)
    product_id = data.get("product_id")
    ishodnaya = next(
        (row for row in documents_repo.lines_of(db, order.id) if product_id and row.product_id == product_id),
        None,
    )
    if ishodnaya is None:
        raise errors.ValidationError(
            "Only products from the order can be returned", code="product_not_in_order"
        )
    if documents_repo.line_by_product(db, vozvrat.id, product_id) is not None:
        raise errors.ValidationError(
            "This product is already in the return", code="line_exists"
        )
    milli = warehouse_service.parse_quantity(data.get("quantity"))
    if not milli or milli <= 0:
        raise errors.ValidationError("Quantity must be positive", code="bad_quantity")
    line = documents_repo.add_line(
        db,
        DocumentLine(
            document_id=vozvrat.id,
            product_id=product_id,
            name_snapshot=ishodnaya.name_snapshot,
            quantity_milli=milli,
            price_minor=ishodnaya.price_minor,
            cost_minor=None,
            sort_order=documents_repo.next_sort_order(db, vozvrat.id),
        ),
    )
    return line


def update_line(db: Session, document_id: int, line_id: int, data: dict) -> DocumentLine:
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat)
    line = _stroka(db, vozvrat.id, line_id)
    if "quantity" in data:
        milli = warehouse_service.parse_quantity(data.get("quantity"))
        if not milli or milli <= 0:
            raise errors.ValidationError("Quantity must be positive", code="bad_quantity")
        line.quantity_milli = milli
    db.flush()
    return line


def remove_line(db: Session, document_id: int, line_id: int) -> None:
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat)
    documents_repo.drop_line(db, _stroka(db, vozvrat.id, line_id))


# --- вложения -----------------------------------------------------------------


def _files_dir(document_id: int) -> Path:
    return get_settings().document_files_dir.joinpath(str(document_id))


def file_path_on_disk(file: DocumentFile) -> Path:
    return _files_dir(file.document_id).joinpath(f"{file.file_uid}{Path(file.original_name).suffix}")


def add_file(db: Session, document_id: int, uploader: User, original_name: str, content: bytes) -> DocumentFile:
    """Фото или видео к возврату. Приёмка общая с файлами клиента."""
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat, "attach files to")
    ext, content = client_service.proverit_vlozhenie(original_name, content, VLOZHENIYA)
    file = documents_repo.add_file(
        db,
        DocumentFile(
            document_id=vozvrat.id,
            uploaded_by=uploader.id,
            file_uid=tokens.new_file_uid(),
            original_name=Path(original_name).name[:255],
            mime=client_service.MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream"),
            size_bytes=len(content),
        ),
    )
    directory = _files_dir(vozvrat.id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path_on_disk(file).write_bytes(content)
    return file


def get_file(db: Session, document_id: int, file_id: int) -> DocumentFile:
    file = documents_repo.get_file(db, document_id, file_id)
    if file is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    return file


def delete_file(db: Session, document_id: int, file_id: int, actor: User) -> None:
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat, "remove files from")
    file = get_file(db, vozvrat.id, file_id)
    _snyat_s_diska_posle_fiksatsii(db, file_path_on_disk(file))
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=file.id,
        entity_label=file.original_name,
    )
    documents_repo.drop_file(db, file)


def _snyat_s_diska_posle_fiksatsii(db: Session, path: Path) -> None:
    # После коммита, а не сразу: откат вернул бы строку, а файла уже нет.
    @sa_event.listens_for(db, "after_commit", once=True)
    def _ubrat(_session) -> None:
        path.unlink(missing_ok=True)


# --- проведение и отмена ------------------------------------------------------


def provesti(db: Session, document_id: int, author: User, warehouse_id: int | None = None) -> Document:
    """Провести возврат: товар на склад, деньги клиенту — одной транзакцией.

    Порядок тот же, что у закрытия заказа: замок, проверка, условная смена
    статуса, склад, событие, история. Замок — на строку ЗАКАЗА: спор идёт о
    том, сколько по нему ещё можно вернуть, и двое с двумя возвратами разом
    вернули бы больше, чем отгружено.

    Склад — накладной, когда есть блок накладных (товар физически приехал, и
    у этого обязана быть бумага — тот же довод, что у сторно), иначе голыми
    движениями `return`; при выключенном складе — ничем, и это записано в
    историю словами.
    """
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat, "post")
    rows = documents_repo.lines_of(db, vozvrat.id)
    if not rows:
        raise errors.ValidationError("This return has no lines", code="return_is_empty")
    if vozvrat.refund_minor is None:
        raise errors.ValidationError("Refund amount is required", code="refund_required")
    order = zakaz(db, vozvrat)

    documents_repo.zapert_bumagu(db, order.id)
    mozhno = dostupno(db, order, krome_id=vozvrat.id)
    for row in rows:
        ostalos = mozhno.get(row.product_id or 0, 0)
        if row.product_id is None or row.quantity_milli > ostalos:
            raise errors.ValidationError(
                f"{row.name_snapshot}: only {warehouse_service.format_quantity(ostalos)} "
                "can still be returned by this order",
                code="return_exceeds_shipped",
            )

    previous = vozvrat.status
    if not documents_repo.take_status(db, vozvrat, expected=previous, status=STATUS_CLOSED):
        raise errors.ConflictError(
            "The return has already been processed by someone else",
            code="document_status_changed",
        )

    sklad_vklyuchen = modules_service.is_enabled(db, "warehouse")
    nakladnaya = None
    if sklad_vklyuchen:
        warehouse = warehouse_service.resolve_warehouse(db, warehouse_id)
        vozvrat.warehouse_id = warehouse.id
        if modules_service.is_enabled(db, "waybills"):
            nakladnaya = waybill_service.create(
                db,
                {
                    "kind": KIND_WAYBILL_IN,
                    "basis_id": vozvrat.id,
                    "client_id": vozvrat.client_id,
                    "deal_id": vozvrat.deal_id,
                    "warehouse_id": warehouse.id,
                    "note": f"return {vozvrat.number} for order {order.number}",
                },
                author,
            )
            for row in rows:
                documents_repo.add_line(
                    db,
                    DocumentLine(
                        document_id=nakladnaya.id,
                        product_id=row.product_id,
                        name_snapshot=row.name_snapshot,
                        quantity_milli=row.quantity_milli,
                        price_minor=row.price_minor,
                        cost_minor=None,
                        sort_order=row.sort_order,
                    ),
                )
            # Статус возврата сменён выше — запрет двойной отгрузки на своей же
            # бумаге не нужен, как и у закрытия заказа.
            waybill_service.provesti(db, nakladnaya.id, author, po_zakrytiyu_zakaza=True)
        else:
            for row in rows:
                warehouse_service.add_move(
                    db,
                    {
                        "product_id": row.product_id,
                        "kind": MOVE_RETURN,
                        "quantity": warehouse_service.format_quantity(row.quantity_milli),
                        "warehouse_id": warehouse.id,
                        "deal_id": vozvrat.deal_id,
                        "comment": f"returned by {vozvrat.number}",
                        "document_id": vozvrat.id,
                    },
                    author,
                    announce=False,
                )

    event_bus.emit(
        RETURN_POSTED,
        db=db,
        actor=author,
        reason=f"return {vozvrat.number} posted",
        source=SOURCE_MANUAL,
        source_ref=vozvrat.number,
        vozvrat=vozvrat,
        order=order,
        lines=rows,
        category_id=payload_of(vozvrat).get("category_id"),
        waybill=nakladnaya,
    )

    if not sklad_vklyuchen:
        primechanie = "warehouse module off, no stock moves"
    elif nakladnaya is not None:
        primechanie = f"received by waybill {nakladnaya.number}"
    else:
        primechanie = ""
    _zapisat_perehod(db, vozvrat, previous, STATUS_CLOSED, author, primechanie)
    audit_service.record(
        db,
        action=audit_service.ACTION_RETURN_POSTED,
        actor=author,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=vozvrat.id,
        entity_label=vozvrat.number,
        before=previous,
        after=STATUS_CLOSED,
    )
    return vozvrat


def otmenit(db: Session, document_id: int, author: User, note: str = "") -> Document:
    vozvrat = get(db, document_id)
    _tolko_chernovik(vozvrat, "cancel")
    return document_service.set_status(db, vozvrat.id, STATUS_CANCELLED, author, note)


def udalit(db: Session, document_id: int, author: User) -> dict:
    """Черновик, заведённый по ошибке, — вместе с вложениями на диске."""
    vozvrat = get(db, document_id)
    for file in documents_repo.files_of(db, vozvrat.id):
        _snyat_s_diska_posle_fiksatsii(db, file_path_on_disk(file))
    return document_service.udalit(db, vozvrat.id, author, (KIND_RETURN,))


# --- внутреннее ---------------------------------------------------------------


def _tolko_chernovik(vozvrat: Document, deystvie: str = "edit") -> None:
    if vozvrat.status != STATUS_DRAFT:
        raise errors.ValidationError(
            f"Cannot {deystvie} a return that is already {vozvrat.status}",
            code="return_not_draft",
        )


def _stroka(db: Session, document_id: int, line_id: int) -> DocumentLine:
    line = documents_repo.get_line(db, document_id, line_id)
    if line is None:
        raise errors.NotFoundError("Line not found", code="line_not_found")
    return line


def _zapisat_perehod(db: Session, vozvrat: Document, previous: str, status: str, author: User, note: str) -> None:
    documents_repo.add_event(
        db,
        DocumentEvent(
            document_id=vozvrat.id,
            from_status=previous,
            to_status=status,
            note=(note or "")[:document_service.MAX_NOTE],
            author_id=author.id,
            author_name=(author.name or "")[:120],
        ),
    )
