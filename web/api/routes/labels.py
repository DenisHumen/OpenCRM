"""Наклейки и штрихкоды: привязка кодов к товару и поиск сканером.

Роутер закрыт `require_module("labels")` целиком, а не по маршруту: пропущенный
маршрут остался бы открытым, и выключенный блок продолжал бы отвечать тому, кто
помнит адрес.

**Сканер здесь ничего не требует от сервера.** Он работает как клавиатура —
«печатает» цифры в поле и жмёт Enter, — поэтому со стороны API это обычный
запрос с кодом в адресе, без единого драйвера и разрешения браузера.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import barcode_service, warehouse_service
from database.models import User
from database.models.document import DOCUMENT_LOCALES
from database.models.warehouse import BARCODE_CODE128, QUANTITY_SCALE
from web.api import schemas
from web.api.deps import get_db, require_module, require_perm
from web.public import routes as public_routes

router = APIRouter(
    prefix="/labels",
    tags=["labels"],
    dependencies=[Depends(require_module("labels"))],
)

#: Подписи печатной страницы. Их две, поэтому отдельного файла строк, как у
#: бланков, здесь нет: наклейка — это штрихкод и название товара, а всё
#: остальное на ней только отнимает миллиметры.
PRINT_STRINGS = {
    "ru": {"title": "Наклейки", "no_code": "код не задан", "print": "Печать"},
    "en": {"title": "Labels", "no_code": "no barcode", "print": "Print"},
    "uk": {"title": "Наклейки", "no_code": "код не задано", "print": "Друк"},
}


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


@router.get("/print", response_class=HTMLResponse)
def print_labels(
    product_id: list[int] = Query(default=[]),
    copies: int = Query(default=1, ge=1, le=barcode_service.MAX_COPIES),
    # Открыть и посмотреть, не печатая. Подбирая размер под свой рулон, на
    # страницу надо смотреть: каждая примерка «вслепую» стоит намотанной ленты,
    # а термобумага не переиспользуется.
    preview: bool = False,
    locale: str | None = None,
    _: User = Depends(require_perm("labels", "view")),
    db: Session = Depends(get_db),
):
    """Страница наклеек — ровно по размеру рулона, печатает её браузер.

    Товары списком в адресе (`?product_id=1&product_id=2`), а не по одному:
    приёмка разбирает поставку пачкой, и открывать вкладку на каждую позицию
    значит не пользоваться этим вовсе. Порядок сохраняется тот, что прислали.

    `copies` — сколько наклеек на каждую позицию: пришло десять коробок,
    печатаем десять одинаковых.

    Открывается по ссылке в новой вкладке, а не через fetch: печать — это
    `window.print()` на настоящей странице с собственным `@page`, и через
    выборку такую страницу не получить.
    """
    if not product_id:
        # Пустая пачка — это не «напечатать ноль наклеек», а промах интерфейса:
        # человек нажал «печать», ничего не выделив. Молча отдать пустой лист
        # значит оставить его гадать, почему принтер промолчал.
        raise errors.ValidationError("Nothing to print", code="nothing_to_print")

    lang = locale if locale in DOCUMENT_LOCALES else "ru"
    settings = barcode_service.label_settings(db)
    html = public_routes.templates.get_template("label_print.html").render(
        items=barcode_service.print_items(db, product_id, copies),
        locale=lang,
        t=PRINT_STRINGS[lang],
        preview=preview,
        **settings,
    )
    return HTMLResponse(html)


@router.get("/settings")
def label_settings(
    _: User = Depends(require_perm("labels", "view")),
    db: Session = Depends(get_db),
):
    """Размер наклейки и что на ней печатать.

    Отдельной ручкой, а не куском общих настроек: общие читает только root
    (там реквизиты и ключи), а размер рулона нужен всякому, кто печатает.
    """
    return barcode_service.label_settings(db)
