"""Деньги и количества не проходят через дробное. Нигде, а не только в финансах.

Правило записано в `CLAUDE.md`: **деньги — целые в минорных единицах,
количество — целое в тысячных, ни то, ни другое не проходит через `float` ни на
секунду**. Причина не в аккуратности: `0.1 + 0.2 != 0.3`, и одна такая строка
разводит итог по колонке с суммой карточек — молча, без ошибки, на копейку,
которую потом ищут в отчёте неделю.

**Проверка была, но узкая.** Она стерегла четыре файла блока финансов
(`tests/test_finance.py`), а `_minor` считают **двадцать один**: заказы, акты,
бланки, склад, отчёты, сводка. Себестоимость заявки, сумма заказа и остаток
склада живут вне блока финансов и до сих пор держались на дисциплине.

Список файлов не ведётся руками — он собирается по упоминанию `_minor` и
`_milli`. Признаков два: пока стоял один денежный, мимо сторожа проходили девять
файлов, где количество в тысячных живёт без денег рядом, — и среди них весь
расчёт брони. Список,
выписанный однажды, устарел бы на первом же новом файле, и устарел бы молча:
ровно так и вышло у соседа, где перечислены четыре файла из двадцати одного.

Ищем по дереву разбора, а не по тексту: `/` встречается в комментариях, путях и
адресах, а `ast.Div` — только там, где вправду делят.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Где искать. Три области, в которых деньги вообще бывают.
OBLASTI = ("core", "database", "web")

#: Признаки денежной и количественной колонки в исходнике.
#:
#: Их ДВА, и это не украшение. Пока признак был один (`_minor`), сторож не
#: смотрел на девять файлов, где количество в тысячных живёт без денег рядом, —
#: а среди них весь расчёт брони (`reserve_service`), `documents.promised` и
#: подписчик списания по выигрышу. Правило `CLAUDE.md` называет обе величины
#: одной строкой, и обе бьются одинаково: `0.1 + 0.2 != 0.3` разводит остаток
#: склада с историей движений так же молча, как сумму с карточками.
PRIZNAKI = ("_minor", "_milli")

#: Дробное, законное вне денежного пути, — с причиной у каждой записи.
#:
#: Список короткий намеренно. Каждая строка здесь — место, где сторож не
#: смотрит, и по сегодняшнему опыту такие списки гниют быстрее прочего:
#: причина исчезает, запись остаётся, объяснение при ней выглядит убедительно.
#: Поэтому ниже стоит проверка, требующая, чтобы каждая запись всё ещё была
#: нужна.
RAZRESHENO: dict[str, str] = {
    "core/services/barcode_service.py:_mm": (
        "миллиметры наклейки из настроек: строку «58.0» надо принять и привести "
        "к целому, к деньгам отношения не имеет"
    ),
}


def _dengi_est(text: str) -> bool:
    return any(priznak in text for priznak in PRIZNAKI)


def _fayly_s_dengami() -> list[pathlib.Path]:
    nayd = []
    for oblast in OBLASTI:
        for put in sorted((ROOT / oblast).rglob("*.py")):
            if "__pycache__" in put.parts:
                continue
            if _dengi_est(put.read_text(encoding="utf-8")):
                nayd.append(put)
    return nayd


def _drobnoe(put: pathlib.Path) -> list[tuple[str, str]]:
    """[(имя функции, что нашли)] — дробные действия в файле.

    Имя функции нужно, чтобы разрешение можно было выдать точечно, а не на
    целый файл: разрешив файл, мы перестали бы смотреть и на деньги в нём.
    """
    derevo = ast.parse(put.read_text(encoding="utf-8"))
    vladelets: dict[int, str] = {}
    for uzel in ast.walk(derevo):
        if isinstance(uzel, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for vnutri in ast.walk(uzel):
                vladelets.setdefault(id(vnutri), uzel.name)

    nayd: list[tuple[str, str]] = []
    for uzel in ast.walk(derevo):
        imya = vladelets.get(id(uzel), "<модуль>")
        if isinstance(uzel, ast.BinOp) and isinstance(uzel.op, ast.Div):
            nayd.append((imya, f"строка {uzel.lineno}: деление даёт float"))
        if (
            isinstance(uzel, ast.Call)
            and isinstance(uzel.func, ast.Name)
            and uzel.func.id in {"float", "round"}
        ):
            nayd.append((imya, f"строка {uzel.lineno}: {uzel.func.id}(...)"))
    return nayd


def test_perebor_nakhodit_fayly_s_dengami():
    """Пустой перебор сделал бы проверку ниже зелёной и бессмысленной.

    Признак — упоминание `_minor`. Переименуют колонки — перебор опустеет, и
    сторож станет украшением. Порог с запасом: файлов сейчас двадцать один.
    """
    assert len(_fayly_s_dengami()) >= 15, (
        "файлы с деньгами не нашлись — проверка ничего не значит"
    )


@pytest.mark.parametrize(
    "put", _fayly_s_dengami(), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_dengi_ne_prokhodyat_cherez_drobnoe(put: pathlib.Path):
    """Ни `/`, ни `float()`, ни `round()` в файле, который считает деньги."""
    rel = put.relative_to(ROOT).as_posix()
    vinovnye = [
        f"{rel}:{funktsiya} — {chto}"
        for funktsiya, chto in _drobnoe(put)
        if f"{rel}:{funktsiya}" not in RAZRESHENO
    ]
    assert vinovnye == [], (
        "деньги или количество прошли через дробное:\n  "
        + "\n  ".join(vinovnye)
        + "\nЦелочисленно: `a * b // c`, а не `a * b / c`. Законное исключение — "
        "в RAZRESHENO, с причиной."
    )


def test_razresheniya_na_drobnoe_ne_protukhli():
    """У каждого разрешения обязано быть то, что оно разрешает.

    Проверка обратная: пока место названо в `RAZRESHENO`, сторож на него не
    смотрит. Переписали функцию без дробного, переименовали её или унесли файл
    — и запись прикрывает пустоту, а следующее деление в той же функции пройдёт
    незамеченным.

    Тот же разбор, что у списков исключений в `test_db_boundary.py`,
    `test_route_guards.py` и `test_screens.py`: список «сюда не смотреть»
    пополняют осознанно, а вычищать некому.
    """
    zhivye = set()
    for put in _fayly_s_dengami():
        rel = put.relative_to(ROOT).as_posix()
        for funktsiya, _chto in _drobnoe(put):
            zhivye.add(f"{rel}:{funktsiya}")

    lishnie = sorted(set(RAZRESHENO) - zhivye)
    assert lishnie == [], (
        "разрешения на дробное ничего не разрешают — файла, функции или самого "
        "деления там больше нет:\n  " + "\n  ".join(lishnie)
    )
