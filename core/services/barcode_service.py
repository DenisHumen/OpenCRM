"""Штрихкоды товара: свои, чужие и поиск по отсканированному.

Блок существует ради одного: **опознать товар однозначно и за долю секунды**.
Пока товар ищут глазами и памятью, склад врёт — приёмщик берёт «ту коробку с
матрицами», продавец выбирает не ту позицию из трёх похожих, приёмка сверяется
по накладной построчно.

Три решения, на которых всё держится.

**Кодов у товара несколько.** Разбор — в докстроке модели `ProductBarcode`.

**Внутренний код есть всегда.** Он выдаётся при заведении товара, даже если
заводского нет и не будет: без него нельзя напечатать наклейку на собственное
изделие, на весовой товар и на всё, что пришло без упаковки. Заводской код при
этом остаётся заводским — свой мы ему не подменяем.

**Сканер — это клавиатура.** Он «печатает» цифры туда, где стоит курсор, и жмёт
Enter. Никакого драйвера, никакого разрешения браузера: со стороны сервера это
обычный запрос с кодом в адресе. Поэтому весь блок — это таблица, три ручки в
API и поле ввода на экране.
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import uniqueness
from database.models import ProductBarcode
from database.models.warehouse import (
    BARCODE_CODE128,
    BARCODE_INTERNAL,
    BARCODE_KINDS,
    QUANTITY_SCALE,
)
from database.repositories import warehouse as warehouse_repo

#: Приставка внутреннего кода. Короткая и произносимая вслух: код диктуют по
#: телефону («у нас это ноль-один-два-три...»), и чем он длиннее, тем чаще его
#: перевирают.
INTERNAL_PREFIX = "2"

#: Длина внутреннего кода вместе с приставкой и контрольным разрядом.
#:
#: Восемь знаков — это 999 999 номеров, чего малому бизнесу хватит навсегда, и
#: при этом код читается одним взглядом. Начинается с «2» не случайно: диапазон
#: EAN, отведённый под внутренние коды магазина, начинается именно с 2, и
#: заводской товар с таким префиксом в продажу не попадает — то есть наш код
#: заведомо не столкнётся с чужим.
INTERNAL_LENGTH = 8

MAX_CODE = 64


def check_digit(digits: str) -> str:
    """Контрольный разряд по схеме EAN: 3-1-3-1 справа налево.

    Нужен не для красоты. Код набирают руками, когда наклейка затёрлась или
    сканер не берёт, и одна перепутанная цифра без проверки означает **чужой
    товар в заказе** — молча и правдоподобно. С контрольным разрядом такая
    опечатка отвергается: система говорит «код неверен», а не подставляет
    соседнюю позицию.
    """
    total = 0
    for position, digit in enumerate(reversed(digits)):
        total += int(digit) * (3 if position % 2 == 0 else 1)
    return str((10 - total % 10) % 10)


def is_valid_internal(code: str) -> bool:
    """Наш ли это код и не переврана ли в нём цифра."""
    code = (code or "").strip()
    if len(code) != INTERNAL_LENGTH or not code.isdigit():
        return False
    if not code.startswith(INTERNAL_PREFIX):
        return False
    return check_digit(code[:-1]) == code[-1]


def next_internal_code(db: Session) -> str:
    """Следующий свободный внутренний код.

    Считается как «максимум выданного плюс один», а не как «сколько товаров»:
    товары удаляют, а код остаётся занятым — на коробках в углу склада уже
    наклеены этикетки, и выдать тот же номер новому товару значит однажды
    отсканировать не то.
    """
    body_length = INTERNAL_LENGTH - len(INTERNAL_PREFIX) - 1
    used = warehouse_repo.internal_codes_like(db, INTERNAL_PREFIX)
    highest = 0
    for code in used:
        if len(code) != INTERNAL_LENGTH or not code.isdigit():
            continue
        body = code[len(INTERNAL_PREFIX):-1]
        if body.isdigit():
            highest = max(highest, int(body))
    body = str(highest + 1).zfill(body_length)
    if len(body) > body_length:
        raise errors.ValidationError(
            "Internal barcode numbers are exhausted", code="barcodes_exhausted"
        )
    prefix_and_body = f"{INTERNAL_PREFIX}{body}"
    return prefix_and_body + check_digit(prefix_and_body)


def clean_code(code: str) -> str:
    """Привести присланный код к тому виду, в котором он лежит в базе.

    Сканеры и люди добавляют пробелы и дефисы для читаемости — в базе их нет.
    Без приведения один и тот же код, набранный руками и отсканированный, стал
    бы двумя разными.
    """
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if not code:
        raise errors.ValidationError("Barcode is required", code="barcode_required")
    if len(code) > MAX_CODE:
        raise errors.ValidationError("Barcode is too long", code="barcode_too_long")
    return code


def add(
    db: Session,
    product_id: int,
    code: str,
    *,
    kind: str = BARCODE_CODE128,
    pack_size_milli: int = QUANTITY_SCALE,
    is_primary: bool = False,
) -> ProductBarcode:
    """Привязать код к товару. Занятый — отказ с указанием, кем занят."""
    if kind not in BARCODE_KINDS:
        raise errors.ValidationError(f"Unknown barcode kind: {kind}", code="unknown_barcode_kind")
    if pack_size_milli <= 0:
        # Ноль или минус означал бы «в упаковке нисколько» — отсканированная
        # коробка добавила бы в заказ пустоту, и понять это по экрану нельзя.
        raise errors.ValidationError(
            "Pack size must be greater than zero", code="bad_pack_size"
        )
    code = clean_code(code)

    row = uniqueness.insert_unique(
        db,
        ProductBarcode(
            product_id=product_id,
            code=code,
            kind=kind,
            pack_size_milli=pack_size_milli,
        ),
        taken=lambda new: warehouse_repo.get_barcode(db, new.code) is not None,
        message="This barcode already belongs to another item",
        code="barcode_taken",
    )
    # Первый код товара становится основным сам: иначе печать наклейки упёрлась
    # бы в «основной не выбран» ровно тогда, когда выбирать не из чего.
    if is_primary or len(warehouse_repo.barcodes_of(db, product_id)) == 1:
        warehouse_repo.make_primary(db, row)
    return row


def issue_internal(db: Session, product_id: int) -> ProductBarcode:
    """Выдать товару собственный код. Уже выданный не выдаётся дважды."""
    for row in warehouse_repo.barcodes_of(db, product_id):
        if row.kind == BARCODE_INTERNAL:
            return row
    return add(db, product_id, next_internal_code(db), kind=BARCODE_INTERNAL)


def remove(db: Session, product_id: int, barcode_id: int) -> None:
    """Отвязать код от товара.

    Последний код отвязать можно: товар без штрихкода — законное состояние
    (услуга, разовая позиция), и запрещать это значило бы заставлять человека
    удалять карточку целиком ради снятия ошибочно введённого кода.
    """
    row = warehouse_repo.barcode_of_product(db, product_id, barcode_id)
    if row is None:
        raise errors.NotFoundError("Barcode not found", code="barcode_not_found")
    was_primary = row.is_primary
    warehouse_repo.drop_barcode(db, row)
    if was_primary:
        # Основной сняли — печатать наклейку станет нечем, пока кто-нибудь не
        # выберет новый. Выбираем сами: первый оставшийся.
        rest = warehouse_repo.barcodes_of(db, product_id)
        if rest:
            warehouse_repo.make_primary(db, rest[0])


def set_primary(db: Session, product_id: int, barcode_id: int) -> ProductBarcode:
    row = warehouse_repo.barcode_of_product(db, product_id, barcode_id)
    if row is None:
        raise errors.NotFoundError("Barcode not found", code="barcode_not_found")
    warehouse_repo.make_primary(db, row)
    return row


def scan(db: Session, code: str):
    """Товар по отсканированному коду. Не нашли — говорим, что именно искали.

    Пустой ответ после писка сканера читается как «сканер сломался». Поэтому
    ручка отвечает 404 с самим кодом внутри: экран показывает «код 2000123 не
    найден» и предлагает завести товар прямо отсюда.
    """
    code = clean_code(code)
    product = warehouse_repo.product_by_code(db, code)
    if product is None:
        raise errors.NotFoundError(
            f"No item with barcode {code}", code="barcode_unknown"
        )
    return product


def list_of(db: Session, product_id: int) -> list[ProductBarcode]:
    """Коды товара. Основной первым — его печатают на наклейке."""
    return warehouse_repo.barcodes_of(db, product_id)


#: Настройки наклейки, которые блок отдаёт экрану печати.
LABEL_SETTINGS = (
    "label_width_mm",
    "label_height_mm",
    "label_show_price",
    "label_show_name",
    "label_show_sku",
)


#: Границы размера наклейки в миллиметрах.
#:
#: Снизу — самый мелкий рулон, который вообще бывает (ювелирные «хвостики»
#: 20×10); сверху — лист A4 в ландшафте, дальше начинается уже не наклейка.
MIN_LABEL_MM = 10
MAX_LABEL_MM = 300


def label_settings(db: Session) -> dict:
    """Размер наклейки и состав полей.

    Размер приводим к числу здесь, а не в шаблоне, и вот почему. Он уезжает
    прямо в `@page { size: ...mm }` печатной страницы, то есть **внутрь тега
    style**, а Jinja экранирует HTML, но не CSS: строка вида `58mm } body {`
    закрыла бы правило и дописала своё. Настройки правит только root, так что
    это не дыра наружу, — но одно неверное значение (пустая строка, «58 мм» с
    буквами, случайный перевод строки) ломало бы печать у всех, и виновника в
    отпечатанной ленте было бы не видно.

    Печатает браузер, а не сервер: термопринтер стоит на столе в мастерской, и
    с VPS до него дороги нет и не будет.
    """
    from core.services import settings_service

    values = settings_service.get_all(db)
    return {
        "width_mm": _mm(values.get("label_width_mm"), 58),
        "height_mm": _mm(values.get("label_height_mm"), 40),
        "show_price": values.get("label_show_price", "0") == "1",
        "show_name": values.get("label_show_name", "1") == "1",
        "show_sku": values.get("label_show_sku", "1") == "1",
    }


def _mm(value, fallback: int) -> int:
    """Миллиметры из настроек: целое в границах рулона, иначе — значение по умолчанию.

    Молча подставить умолчание правильнее, чем отказать: печать не должна
    падать из-за строки в настройках. Неверный размер человек увидит на первой
    же наклейке, а несработавшая кнопка «печать» не объяснит ничего.
    """
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback
    if not MIN_LABEL_MM <= number <= MAX_LABEL_MM:
        return fallback
    return number


#: Сколько наклеек можно напечатать за раз.
#:
#: Не защита от дурака, а защита рулона: опечатка в поле «сколько копий» стоит
#: намотанной впустую термобумаги, и заметить её человек успевает не всегда.
#: Двести — это коробка товара с запасом; больше за один раз не печатают.
MAX_COPIES = 200


def print_items(db: Session, product_ids: list[int], copies: int = 1) -> list[dict]:
    """Наклейки к печати: по `copies` штук на каждый товар, в заданном порядке.

    Порядок именно тот, в котором пришли идентификаторы: человек выделил строки
    на экране и ждёт их в том же виде. Сортировать по-своему значит заставить
    его искать, где что, уже на отпечатанной ленте.

    Товар без штрихкода из печати НЕ выбрасывается: наклейка выйдет с пометкой
    «кода нет». Молча пропустить его значит отдать пачку, в которой на две
    наклейки меньше, и обнаружить это на коробках.
    """
    from core.services import settings_service
    from core.services import codes as code_render
    from database.repositories import warehouse as warehouse_repo

    copies = max(1, min(int(copies), MAX_COPIES))
    settings = label_settings(db)
    currency = settings_service.get_all(db).get("currency", "USD")

    products = {p.id: p for p in warehouse_repo.products_by_ids(db, product_ids)}
    barcodes = warehouse_repo.barcodes_by_products(db, product_ids)

    items: list[dict] = []
    for product_id in product_ids:
        product = products.get(product_id)
        if product is None:
            continue
        primary = next(iter(barcodes.get(product_id, [])), None)
        row = {
            "name": product.name,
            "sku": product.sku or "",
            "price": _money(product.price_minor, currency),
            "code": primary.code if primary else "",
            "barcode": code_render.barcode_svg(primary.code, label=True) if primary else "",
        }
        items.extend([row] * copies)
    return items


def _money(minor: int | None, currency: str) -> str:
    """Цена для печати. Пусто — цену не назвали, и это не ноль.

    Считаем целыми: делить на 100 через float нельзя, деньги в этом проекте
    через float не считаются нигде.
    """
    if minor is None:
        return ""
    whole, cents = divmod(abs(int(minor)), 100)
    sign = "-" if minor < 0 else ""
    return f"{sign}{whole}.{cents:02d} {currency}".strip()
