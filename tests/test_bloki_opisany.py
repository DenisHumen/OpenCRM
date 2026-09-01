"""Каждый блок реестра назван в документах, и число блоков сходится.

Реестр `core/modules.py` — единственный источник правды о том, из чего состоит
система. Документы пересказывают его руками, и расходятся они молча: блок
заводится кодом, а строка в таблице «что готово» не появляется.

**Расхождение было, и не мелкое.** На 02.09.2026 в реестре стояло семнадцать
блоков, оба документа писали «шестнадцать», а **накладных не было ни в одной
таблице готовности** — при том, что раздел есть у каждого другого блока. То
есть целый блок системы, с двенадцатью ручками и своим экраном, для читателя
документов не существовал.

Проверка сравнивает СПИСОК, а не описания: судить, верно ли описан блок, она не
умеет. Её работа — не дать блоку остаться неназванным.
"""

import pathlib
import re

KOREN = pathlib.Path(__file__).resolve().parent.parent

#: Где обязан быть назван каждый блок ПОИМЁННО — там, где документ и берётся
#: перечислить их все. Соседи сюда не входят намеренно: `01-overview.md`
#: рассказывает, зачем блоки, а `11-modules.md` — как устроен реестр; ни тот,
#: ни другой списком не притворяется, и требовать от них полноты значило бы
#: заставить переписывать таблицу в трёх местах.
DOKUMENTY = ("docs/09-roadmap.md",)

#: Числительные, которыми документы называют размер реестра. Слово, а не цифра:
#: так они и написаны, и «шестнадцать» на семнадцати блоках — та самая молчащая
#: неправда, ради которой проверка и стоит.
CHISLITELNYE = {
    14: "четырнадцать", 15: "пятнадцать", 16: "шестнадцать",
    17: "семнадцать", 18: "восемнадцать", 19: "девятнадцать", 20: "двадцать",
}


def kluchi_blokov() -> list[str]:
    """Ключи блоков из реестра — разбором исходника, а не ввозом.

    Ввоз `core.modules` притащил бы за собой настройки и базу; проверке нужен
    список, и он виден в тексте.
    """
    tekst = (KOREN / "core" / "modules.py").read_text(encoding="utf-8")
    return re.findall(r'^    Module\(key="([a-z_]+)"', tekst, re.M)


def test_perebor_vidit_reestr():
    """Пустой список объявил бы документы полными, ничего не проверив."""
    kluchi = kluchi_blokov()
    assert len(kluchi) >= 15, f"блоков нашлось {len(kluchi)} — разбор реестра сломался"
    assert {"clients", "deals", "warehouse", "waybills"} <= set(kluchi)


def test_kazhdyy_blok_nazvan_v_dokumentakh():
    """Блок, которого нет в документах, для читателя не существует."""
    kluchi = kluchi_blokov()
    propushcheno = []
    for imya in DOKUMENTY:
        tekst = (KOREN / imya).read_text(encoding="utf-8")
        propushcheno += [f"{imya}: {k}" for k in kluchi if f"`{k}`" not in tekst]
    assert propushcheno == [], "блоки не названы в документах:\n  " + "\n  ".join(propushcheno)


def test_chislo_blokov_v_dokumentakh_sovpadaet_s_reestrom():
    """«Шестнадцать блоков» на семнадцати — неправда, которую никто не заметит."""
    skolko = len(kluchi_blokov())
    verno = CHISLITELNYE.get(skolko)
    assert verno, f"блоков {skolko} — допишите числительное в CHISLITELNYE"

    vinovnye = []
    for imya in ("docs/09-roadmap.md", "docs/01-overview.md"):
        tekst = (KOREN / imya).read_text(encoding="utf-8")
        chuzhie = [
            slovo
            for chislo, slovo in CHISLITELNYE.items()
            if chislo != skolko and re.search(rf"{slovo} блок", tekst, re.I)
        ]
        if chuzhie:
            vinovnye.append(f"{imya}: сказано «{', '.join(chuzhie)}», а блоков {skolko}")
        elif not re.search(rf"{verno} блок", tekst, re.I):
            vinovnye.append(f"{imya}: число блоков не названо вовсе")
    assert vinovnye == [], "\n  ".join([""] + vinovnye)
