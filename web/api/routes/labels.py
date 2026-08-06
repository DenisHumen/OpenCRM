"""Наклейки и штрихкоды: привязка кодов к товару и поиск сканером.

Роутер закрыт `require_module("labels")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отвечать тому, кто
помнит адрес.

**Сканер здесь ничего не требует от сервера.** Он работает как клавиатура —
«печатает» цифры в поле и жмёт Enter, — поэтому со стороны API это обычный
запрос с кодом в адресе, без единого драйвера и разрешения браузера.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import barcode_service, warehouse_service
from database.models import User
from database.models.warehouse import BARCODE_CODE128, QUANTITY_SCALE
from web.api import schemas
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/labels",
    tags=["labels"],
    dependencies=[Depends(require_module("labels"))],
)


class BarcodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    kind: str = BARCODE_CODE128
    # Сколько единиц товара в этой упаковке, в тысячных: штука — 1000, блок из
    # десяти — 10000. Отсканировали коробку — в заказ уходит десять штук.
    pack_size_milli: int = Field(default=QUANTITY_SCALE, gt=0)
    is_primary: bool = False


@router.get("/products/{product_id}/barcodes")
def list_barcodes(
    product_id: int,
    _: User = Depends(require_perm("labels", "view")),
    db: Session = Depends(get_db),
):
    warehouse_service.get_product(db, product_id)  # нет товара — честный 404
    return {"items": [schemas.barcode_out(b) for b in barcode_service.list_of(db, product_id)]}


@router.post("/products/{product_id}/barcodes", status_code=201)
def add_barcode(
    product_id: int,
    payload: BarcodeIn,
    _: User = Depends(require_perm("labels", "create")),
    db: Session = Depends(get_db),
):
    warehouse_service.get_product(db, product_id)
    row = barcode_service.add(
        db,
        product_id,
        payload.code,
        kind=payload.kind,
        pack_size_milli=payload.pack_size_milli,
        is_primary=payload.is_primary,
    )
    return schemas.barcode_out(row)


@router.post("/products/{product_id}/barcodes/internal", status_code=201)
def issue_internal(
    product_id: int,
    _: User = Depends(require_perm("labels", "create")),
    db: Session = Depends(get_db),
):
    """Выдать товару собственный код — для того, у чего заводского нет."""
    warehouse_service.get_product(db, product_id)
    return schemas.barcode_out(barcode_service.issue_internal(db, product_id))


@router.post("/products/{product_id}/barcodes/{barcode_id}/primary")
def set_primary(
    product_id: int,
    barcode_id: int,
    _: User = Depends(require_perm("labels", "create")),
    db: Session = Depends(get_db),
):
    """Какой из кодов печатать на наклейке."""
    warehouse_service.get_product(db, product_id)
    return schemas.barcode_out(barcode_service.set_primary(db, product_id, barcode_id))


@router.delete("/products/{product_id}/barcodes/{barcode_id}")
def drop_barcode(
    product_id: int,
    barcode_id: int,
    _: User = Depends(require_perm("labels", "delete")),
    db: Session = Depends(get_db),
):
    warehouse_service.get_product(db, product_id)
    barcode_service.remove(db, product_id, barcode_id)
    return {"message": "Barcode removed"}


@router.get("/scan/{code}")
def scan(
    code: str,
    _: User = Depends(require_perm("labels", "view")),
    db: Session = Depends(get_db),
):
    """Товар по отсканированному коду.

    Не нашли — 404 с самим кодом внутри, чтобы экран сказал «код 20000127 не
    найден» и предложил завести товар прямо отсюда. Пустой ответ после писка
    сканера читается как «сканер сломался».
    """
    product = barcode_service.scan(db, code)
    return schemas.product_out(product, stock_milli=None, amounts=False)
