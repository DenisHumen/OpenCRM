"""Ввезённое имя обязано использоваться. Иначе это мусор, который читают.

Линтера в проекте нет и не заводится: единственное правило, ради которого его
ставили бы, проверяется двадцатью строками разбора дерева, а `ruff` притащил бы
с собой две сотни правил, о каждом из которых пришлось бы решать отдельно.

**Почему это не придирка.** Мёртвый ввоз врёт о связях: открыв файл, читатель
видит `from core.services import task_service` и заключает, что заявки с сайта
заводят задачи, — а они не заводят. Такие ввозы копятся: из четырёх найденных
02.09.2026 ни один не был свежим.

**Что НЕ считается мёртвым.** `from __future__ import annotations` — указание
компилятору, имени у него нет. Ввоз ради побочного действия (регистрация
моделей в `metadata`, подписчиков в шине) помечается `# noqa: F401` — эта
пометка и есть объявление «имя здесь не нужно, нужен сам ввоз».
"""

import ast
import pathlib

KOREN = pathlib.Path(__file__).resolve().parent.parent

#: Где смотрим. Миграции сюда не входят: у них своя жизнь, они снимок прошлого,
#: и править их ради опрятности запрещено — накатанная ревизия не меняется.
OBLASTI = ("core", "database", "web", "deploy", "scripts", "config")

#: Пометка, которой объявляют ввоз ради побочного действия.
POMETKA = "noqa"


def _ispolzuemye(derevo: ast.AST) -> tuple[set[str], str]:
    """Что файл упоминает: имена и все его строковые постоянные одним куском.

    Строки нужны потому, что аннотация в кавычках (`-> "Deal"`) — законный
    способ сослаться на ввезённое имя, и без них проверка объявила бы его
    мёртвым.
    """
    imena: set[str] = set()
    stroki: list[str] = []
    for uzel in ast.walk(derevo):
        if isinstance(uzel, ast.Name):
            imena.add(uzel.id)
        elif isinstance(uzel, ast.Attribute):
            koren = uzel
            while isinstance(koren, ast.Attribute):
                koren = koren.value
            if isinstance(koren, ast.Name):
                imena.add(koren.id)
        elif isinstance(uzel, ast.Constant) and isinstance(uzel.value, str):
            stroki.append(uzel.value)
    return imena, "\n".join(stroki)


def myortvye(put: pathlib.Path) -> list[str]:
    """Ввезённые имена, которых файл не упоминает. Пусто — всё в деле."""
    tekst = put.read_text(encoding="utf-8")
    derevo = ast.parse(tekst)
    po_strokam = tekst.splitlines()
    ispolzuemye, v_strokakh = _ispolzuemye(derevo)

    vinovnye = []
    for uzel in ast.walk(derevo):
        if isinstance(uzel, (ast.Import, ast.ImportFrom)):
            if isinstance(uzel, ast.ImportFrom) and uzel.module == "__future__":
                continue
            if POMETKA in po_strokam[uzel.lineno - 1]:
                continue
            for imya in uzel.names:
                if imya.name == "*":
                    continue
                svoyo = imya.asname or imya.name.split(".")[0]
                if svoyo not in ispolzuemye and svoyo not in v_strokakh:
                    vinovnye.append(f"{put.relative_to(KOREN)}:{uzel.lineno}  {svoyo}")
    return vinovnye


def _fayly() -> list[pathlib.Path]:
    najdeno = []
    for oblast in OBLASTI:
        for put in sorted((KOREN / oblast).rglob("*.py")):
            if "__pycache__" in put.parts or "migrations" in put.parts:
                continue
            najdeno.append(put)
    return najdeno


def test_perebor_vidit_ves_kod():
    """Пустой перебор объявил бы чистым что угодно — в том числе пустоту."""
    fayly = _fayly()
    assert len(fayly) > 150, f"файлов нашлось {len(fayly)} — области перечислены неверно"
    imena = {p.name for p in fayly}
    assert {"main.py", "documents.py", "lead_service.py"} <= imena


def test_v_kode_net_myortvykh_vvozov():
    """Ввезённое имя, которого никто не упоминает, — мусор, вводящий в заблуждение."""
    vinovnye = [stroka for put in _fayly() for stroka in myortvye(put)]
    assert vinovnye == [], (
        "ввезено и не используется:\n  "
        + "\n  ".join(vinovnye)
        + "\nЕсли ввоз нужен ради побочного действия — пометьте его `# noqa: F401`"
        " и скажите, ради чего."
    )
