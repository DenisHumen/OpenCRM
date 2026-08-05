"""Штрихкод и QR для бланков — оба в SVG, оба встраиваются в страницу.

Почему библиотеки, а не свой код. QR — это Рид-Соломон, маскирование и таблицы
версий; такое в CRM не пишут. Штрихкод Code128 сначала был написан руками:
казалось, что таблица шаблонов и сумма по модулю 103 — дело на сто строк. Но
таблица, набранная по памяти, при первой же структурной проверке (каждый шаблон
обязан складываться в 11 модулей) оказалась битой в четырёх местах и лишней на
две строки. Печатать такой штрихкод — значит выдать клиенту бумагу, которая не
сканируется, и узнать об этом от него же.

SVG, а не PNG: бланк печатают, и вектор остаётся резким на любом принтере,
тогда как растр в 300 dpi пришлось бы генерировать и хранить.
"""

import io
import re

import barcode
import segno
from barcode.writer import SVGWriter

# Номер печатаем под полосками. Это не украшение: сканер не читает мятую или
# залитую кофем бумагу, и тогда приёмщик набирает номер руками — а набрать его
# неоткуда, если под штрихкодом пусто.
#
# Тихая зона (quiet_zone) обязательна: без пустых полей по краям сканер не
# находит начало кода, и красиво свёрстанный впритык штрихкод не читается.
_BARCODE_OPTIONS = {
    "module_height": 8.0,
    "module_width": 0.28,
    "quiet_zone": 3.0,
    "font_size": 8,
    "text_distance": 2.0,
    "write_text": True,
}


def barcode_svg(text: str) -> str:
    """Code128 в виде фрагмента SVG, готового к вставке в HTML."""
    if not text:
        raise ValueError("Barcode text is empty")
    buffer = io.BytesIO()
    barcode.get("code128", text, writer=SVGWriter()).write(buffer, _BARCODE_OPTIONS)
    return _inline(buffer.getvalue().decode("utf-8"))


def qr_svg(text: str, scale: float = 2.4) -> str:
    """QR в виде фрагмента SVG.

    Уровень коррекции M: бланк живёт в мастерской, его мнут и пачкают, а M
    переживает потерю примерно 15% изображения. L экономит место, но на
    затёртой бумаге перестаёт читаться первым.
    """
    if not text:
        raise ValueError("QR text is empty")
    # segno пишет байты даже для SVG — StringIO тут падает с TypeError.
    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="svg", scale=scale, border=2, xmldecl=False)
    return _inline(buffer.getvalue().decode("utf-8"))


def _inline(svg: str) -> str:
    """Убирает пролог и DOCTYPE: внутри HTML они недопустимы и ломают разбор."""
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg, flags=re.IGNORECASE)
    return svg.strip()
