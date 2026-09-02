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

from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import uniqueness
from core.services import codes as code_render
from core.utils import money_for_print
from database.models import Product, ProductBarcode
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



# --------------------------------------------------------------------------
# Что печатать на наклейке
# --------------------------------------------------------------------------
#
# РЕЕСТР, а не набор `if` в шаблоне. Состав наклейки — это то, что у каждого
# дела своё: мастерской нужна единица измерения, магазину цена, складу порог
# заказа, а тому, кто печатает на самоклейке для клиента, — название фирмы.
# Угадать набор один раз нельзя, и дописывать его придётся ещё не раз.
#
# Поэтому добавление нового поля стоит ОДНОЙ записи здесь. Всё остальное
# подхватывается само: значение по умолчанию попадает в настройки, ручка
# `/labels/settings` отдаёт флаг, экран настроек рисует переключатель, шаблон
# печатает поле в своей зоне. Стережёт это `tests/test_labels.py` — он требует,
# чтобы у каждой записи реестра был перевод на обоих языках и место в шаблоне.
#
# Зона — это где поле стоит, и зон ровно три, потому что наклейка устроена
# просто: сверху текст, посередине штрихкод, снизу цифры.
#
#   verh   — над штрихкодом, каждое поле своей строкой (название, заметка);
#   stroka — одной строкой в разбивку, слева направо (артикул, единица, цена);
#   niz    — под цифрами, мелким (название фирмы, дата печати).
#
# Отдельно `bok` — QR рядом со штрихкодом: он не текст и своей строкой не встаёт.
#: Сколько знаков заметки влезает. Больше — и штрихкод уезжает за край даже на
#: 58×40: строка заметки набирается мелким, но переносится, а высота фиксирована.
MAX_NOTE_ON_LABEL = 60

#: Единицы измерения словами. Короткими: на наклейке 58 мм «килограмм» съедает
#: пол-строки, а «кг» понятно всем и всюду.
#: «Штука» здесь есть, хотя наклейка её не печатает (`_pole_unit` отсеивает
#: `pcs` раньше, чем заглянет сюда). Нужна она накладной: получатель сверяет
#: «2 шт» с тем, что в коробке, и пустая клетка в столбце единиц читается как
#: недописанная строка. Второй словарь единиц завёл бы «м²» в двух местах.
UNIT_NAMES = {
    "ru": {"pcs": "шт", "kg": "кг", "g": "г", "l": "л", "ml": "мл", "m": "м",
           "m2": "м²", "pack": "упак", "hour": "ч"},
    "uk": {"pcs": "шт", "kg": "кг", "g": "г", "l": "л", "ml": "мл", "m": "м",
           "m2": "м²", "pack": "упак", "hour": "год"},
    "en": {"pcs": "pcs", "kg": "kg", "g": "g", "l": "l", "ml": "ml", "m": "m",
           "m2": "m²", "pack": "pack", "hour": "h"},
}

#: Приставка у порога заказа: без неё число на наклейке ничего не значит.
MIN_STOCK_PREFIX = {"ru": "мин", "uk": "мін", "en": "min"}

ZONA_VERH = "verh"
ZONA_STROKA = "stroka"
ZONA_NIZ = "niz"
ZONA_BOK = "bok"


@dataclass(frozen=True)
class PoleNakleyki:
    """Одно поле наклейки: как зовётся, где стоит и откуда берётся значение.

    `klyuch` даёт имя настройки (`label_show_<klyuch>`) и строки перевода на
    экране — то есть одно слово связывает базу, API и интерфейс. Разойдись они,
    поле просто не появится, а искать причину пришлось бы в трёх местах.

    `znachenie` берёт товар, его ОСНОВНОЙ КОД и `sreda` — всё, что не
    принадлежит ни тому, ни другому (валюта, название фирмы, адрес сайта, язык,
    дата печати). Код нужен потому, что не всё на наклейке принадлежит товару:
    размер упаковки записан у кода, и печатается наклейка именно под этот код.
    Среда собирается ОДИН раз на всю пачку: печатают по сотне наклеек, и лезть
    за настройками на каждую значило бы сто одинаковых запросов.

    `dengi=True` — поле показывает суммы. Такое поле молчит у того, кому суммы
    не положены (`warehouse.view_amounts`). Отдельный признак, а не проверка
    внутри каждой функции: забыть её в новой значит открыть дыру, о которой
    никто не узнает, — а забыть флаг видно в самой записи реестра.
    """

    klyuch: str
    zona: str
    po_umolchaniyu: bool
    znachenie: Callable[[Product, object, dict], str]
    dengi: bool = False


def _pole_name(product, kod, sreda) -> str:
    return product.name or ""


def _pole_sku(product, kod, sreda) -> str:
    return product.sku or ""


def _pole_price(product, kod, sreda) -> str:
    return money_for_print(product.price_minor, sreda["currency"])


def _pole_unit(product, kod, sreda) -> str:
    """Единица измерения словом. Пустая строка у штук — и это осознанно.

    «шт» на наклейке не значит ничего: штука подразумевается по умолчанию, и
    печатать её — тратить миллиметры на слово, которое никому не сообщает
    нового. А вот «кг» или «м» меняют смысл всей позиции.
    """
    if not product.unit or product.unit == "pcs":
        return ""
    return sreda["units"].get(product.unit, product.unit)


def _pole_note(product, kod, sreda) -> str:
    """Заметка о товаре, обрезанная. Длинная выталкивает штрихкод за край.

    Обрезаем по словам и без многоточия: на наклейке 58 мм многоточие — это
    целый знак, а обрыв на середине слова человек и так поймёт.
    """
    text = " ".join((product.note or "").split())
    return text[:MAX_NOTE_ON_LABEL].rstrip() if text else ""


def _pole_min_stock(product, kod, sreda) -> str:
    """Порог заказа. Тому, кто идёт по полкам, он говорит, что пора заказывать."""
    if not product.min_stock_milli:
        return ""
    return f"{sreda['t_min']} {_kolichestvo(product.min_stock_milli)}"


def _pole_company(product, kod, sreda) -> str:
    return sreda["company"]


def _pole_printed_at(product, kod, sreda) -> str:
    return sreda["printed_at"]


def _pole_qr(product, kod, sreda) -> str:
    """QR со ссылкой на карточку товара — открыть телефоном и увидеть всё.

    Ссылка ведёт в CRM, а не наружу: за ней сессия сотрудника. Смысл в том,
    чтобы стоящий у стеллажа человек навёл телефон и увидел остаток, снимки и
    цену, не идя к компьютеру. Постороннему она не откроет ничего — там вход.
    """
    if not sreda["base_url"]:
        return ""
    return code_render.qr_svg(f"{sreda['base_url']}/warehouse/{product.id}")


def _pole_pack(product, kod, sreda) -> str:
    """Сколько единиц товара в этой упаковке — если не одна.

    **Самое недооценённое поле наклейки.** Размер упаковки записан у КОДА, а не
    у товара: один и тот же товар имеет код на штуку и код на блок из десяти.
    Отсканировали блок — в заказ ушло десять штук, и это правильно. Но на самой
    наклейке об этом до сих пор не было ни слова: две наклейки выглядели
    одинаково, а значили разное, и понять, какую клеить на коробку, было
    неоткуда.

    Единица не печатается по той же причине, что и «шт» у единицы измерения:
    упаковка в одну штуку — это обычный случай, и говорить о нём нечего.
    """
    razmer = getattr(kod, "pack_size_milli", None)
    if not razmer or razmer == QUANTITY_SCALE:
        return ""
    return f"×{_kolichestvo(razmer)}"


#: Порядок записей — это порядок полей на наклейке сверху вниз.
POLYA_NAKLEYKI: tuple[PoleNakleyki, ...] = (
    PoleNakleyki("name", ZONA_VERH, True, _pole_name),
    PoleNakleyki("note", ZONA_VERH, False, _pole_note),
    PoleNakleyki("sku", ZONA_STROKA, True, _pole_sku),
    PoleNakleyki("unit", ZONA_STROKA, False, _pole_unit),
    PoleNakleyki("pack", ZONA_STROKA, False, _pole_pack),
    PoleNakleyki("min_stock", ZONA_STROKA, False, _pole_min_stock),
    # Цена по умолчанию ВЫКЛЮЧЕНА: напечатанная цена устаревает в день смены
    # прайса и начинает врать клиенту, а наклейка живёт на коробке месяцами.
    PoleNakleyki("price", ZONA_STROKA, False, _pole_price, dengi=True),
    PoleNakleyki("company", ZONA_NIZ, False, _pole_company),
    PoleNakleyki("printed_at", ZONA_NIZ, False, _pole_printed_at),
    PoleNakleyki("qr", ZONA_BOK, False, _pole_qr),
)

def nastroyka_polya(klyuch: str) -> str:
    """Имя настройки для поля. Одно место, где склеивается приставка."""
    return f"label_show_{klyuch}"


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

    Состав полей собирается ИЗ РЕЕСТРА, а не перечисляется здесь. Перечисли его
    руками — и новое поле пришлось бы дописывать в двух местах, а забытая
    строка означала бы поле, которое есть в настройках и не печатается никогда.

    Печатает браузер, а не сервер: термопринтер стоит на столе в мастерской, и
    с VPS до него дороги нет и не будет.
    """
    from core.services import settings_service

    values = settings_service.get_all(db)
    return {
        "width_mm": _mm(values.get("label_width_mm"), 58),
        "height_mm": _mm(values.get("label_height_mm"), 40),
        "pokazat": {
            pole.klyuch: _vklyucheno(values, pole) for pole in POLYA_NAKLEYKI
        },
    }


def _vklyucheno(values: dict, pole: PoleNakleyki) -> bool:
    """Включено ли поле. Нет записи в базе — берём умолчание реестра.

    Записи может не быть законно: настройки сеются при первом старте, а поле
    добавлено позже. Считать её отсутствие за «выключено» значило бы, что
    новое поле у всех, кто обновился, молча не появится, — и разбираться в этом
    пришлось бы, глядя на отпечатанную ленту.
    """
    raw = values.get(nastroyka_polya(pole.klyuch))
    if raw is None:
        return pole.po_umolchaniyu
    return str(raw) == "1"


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


def print_items(
    db: Session,
    product_ids: list[int],
    copies: int = 1,
    locale: str = "ru",
    amounts: bool = True,
) -> list[dict]:
    """Наклейки к печати: по `copies` штук на каждый товар, в заданном порядке.

    Порядок именно тот, в котором пришли идентификаторы: человек выделил строки
    на экране и ждёт их в том же виде. Сортировать по-своему значит заставить
    его искать, где что, уже на отпечатанной ленте.

    Товар без штрихкода из печати НЕ выбрасывается: наклейка выйдет с пометкой
    «кода нет». Молча пропустить его значит отдать пачку, в которой на две
    наклейки меньше, и обнаружить это на коробках.

    Поля собираются по РЕЕСТРУ и раскладываются по зонам, а не перечисляются
    поимённо: шаблон печатает то, что ему дали, и добавление нового поля его не
    касается вовсе.

    **`amounts=False` — суммы не печатаются.** Это не украшение, а дыра,
    найденная разбором: печать закрыта правом `labels.view` и о суммах не
    спрашивала вовсе, а цену клала безусловно. То есть кладовщик без
    `warehouse.view_amounts` не видел цену на экране — и печатал её на
    наклейке, если владелец включил поле. Право у склада заведено ровно затем,
    чтобы закупочную и продажную видел не всякий, и обходить его печатью нельзя.
    """
    from core.services import settings_service

    copies = max(1, min(int(copies), MAX_COPIES))
    settings = label_settings(db)
    pokazat = settings["pokazat"]
    values = settings_service.get_all(db)
    sreda = _sreda(values, locale)

    products = {p.id: p for p in warehouse_repo.products_by_ids(db, product_ids)}
    barcodes = warehouse_repo.barcodes_by_products(db, product_ids)

    items: list[dict] = []
    for product_id in product_ids:
        product = products.get(product_id)
        if product is None:
            continue
        primary = next(iter(barcodes.get(product_id, [])), None)
        # Зоны отдаём списками уже готовых строк. Пустые поля отсеиваем здесь,
        # а не в шаблоне: иначе включённое, но пустое поле оставляло бы на
        # наклейке пустую строку, а миллиметры на ней считанные.
        zony: dict[str, list[str]] = {
            ZONA_VERH: [],
            ZONA_STROKA: [],
            ZONA_NIZ: [],
        }
        qr = ""
        for pole in POLYA_NAKLEYKI:
            if not pokazat.get(pole.klyuch):
                continue
            if pole.dengi and not amounts:
                continue
            znachenie = pole.znachenie(product, primary, sreda)
            if not znachenie:
                continue
            if pole.zona == ZONA_BOK:
                qr = znachenie
            else:
                zony[pole.zona].append(znachenie)
        row = {
            "verh": zony[ZONA_VERH],
            "stroka": zony[ZONA_STROKA],
            "niz": zony[ZONA_NIZ],
            "qr": qr,
            "code": primary.code if primary else "",
            "barcode": code_render.barcode_svg(primary.code, label=True) if primary else "",
        }
        items.extend([row] * copies)
    return items


def _sreda(values: dict, locale: str) -> dict:
    """Всё, что нужно полям и не принадлежит товару.

    Собирается ОДИН раз на всю пачку: печатают по сотне наклеек, и лезть за
    настройками на каждую значило бы сто одинаковых обращений.

    Дата печати берётся местная, а не UTC: наклейку читает человек, стоящий у
    принтера, и «вчерашнее число» на свежей ленте он посчитает за поломку.
    """
    return {
        "currency": values.get("currency", "USD"),
        "company": values.get("brand_name", "") or "",
        "base_url": _adres_sayta(),
        "printed_at": date.today().strftime("%d.%m.%Y"),
        "units": UNIT_NAMES.get(locale, UNIT_NAMES["ru"]),
        "t_min": MIN_STOCK_PREFIX.get(locale, MIN_STOCK_PREFIX["ru"]),
    }


def _adres_sayta() -> str:
    """Адрес сайта для QR. Пусто — QR не печатаем вовсе.

    Ссылка на `localhost` в QR бесполезна: телефон, наведённый на наклейку, по
    ней не попадёт никуда. Лучше не напечатать код, чем напечатать нерабочий, —
    нерабочий человек попробует трижды и решит, что сломан сканер.
    """
    from config.settings import get_settings

    adres = (get_settings().base_url or "").rstrip("/")
    if "localhost" in adres or "127.0.0.1" in adres:
        return ""
    return adres


def _kolichestvo(milli: int) -> str:
    """Тысячные строкой. Тем же способом, что и везде: целыми, без float."""
    from core.services import warehouse_service

    return warehouse_service.format_quantity(milli)


