"""У каждой выборки порядок ПОЛНЫЙ, иначе страницы теряют записи.

`ORDER BY updated_at DESC` без разрешителя ничьей — не косметика. Две записи с
одинаковым значением база вправе вернуть в любом порядке, и порядок этот у неё
меняется: между запросами, между страницами, после перезапуска. Отсюда три
беды, и все молчаливые:

- **страничный список теряет и повторяет.** Вторая страница берёт `OFFSET 50` по
  НОВОМУ порядку: запись, стоявшая пятидесятой, во втором запросе оказывается
  сорок девятой — и не попадает никуда, а соседка приходит дважды. На экране это
  «карточка пропала», и искать её идут в корзине;
- **`LIMIT` отбирает наугад.** «Десять лучших» и «частые причины отказа» на двух
  открытиях подряд показывают разные списки;
- **выборка одной строки цепляет не ту.** Клиента по номеру телефона ищут
  «свежий сверху, взять первого»: два одинаково свежих — и звонок ложится то на
  одну карточку, то на другую, раскалывая историю надвое.

Разбор самой беды — в докстроке `database/query._with_tiebreak`; там же сказано,
почему ключ дописывается в ТУ ЖЕ сторону: вторичный индекс InnoDB физически
хранит (ключ, первичный ключ), и `updated_at DESC, id DESC` читается индексом
насквозь, а `id ASC` при `updated_at DESC` заставил бы базу сортировать.

**Проверка появилась после сплошного разбора 02.09.2026**: из восемнадцати
сортировок без первичного ключа тринадцать шли мимо `page_of`, то есть мимо
единственного места, где ключ дописывался. `docs/03-database.md` при этом
утверждал, что «сортировка в проекте всегда поле, потом id».
"""

import pathlib
import re

KOREN = pathlib.Path(__file__).resolve().parent.parent
REPOZITORII = KOREN / "database" / "repositories"

#: Помощники, которые дописывают первичный ключ САМИ (`query._with_tiebreak`).
#: Выборка, уехавшая в них, разрешителя в своём `order_by` не требует.
STRANICHNYE = ("page_of", "page_without_total")

#: Сортировки без первичного ключа, которые верны и без него, — с доводом.
#:
#: Поводов ровно два, и оба про то, что ключ там не нужен, а не про то, что «и
#: так сойдёт». Ключ записи — файл и само выражение, а не номер строки: номер
#: уедет от первой правки выше по файлу, а выражение переживёт её и заставит
#: принять решение заново.
POLNYY_BEZ_KLYUCHA: dict[tuple[str, str], str] = {
    (
        "reports.py",
        "func.count().desc(), Deal.lost_reason.asc()",
    ): "сгруппировано по причине: строка — это группа, `id` у неё нет, "
       "а сама причина уникальна по построению GROUP BY",
    (
        "roles.py",
        "Role.name.asc()",
    ): "имя должности уникально (отказ `role_name_taken`), порядок и так полный",
}


def _skobki(tekst: str, otkryvayushchaya: int) -> str:
    """Текст внутри скобок, со счётом вложенности.

    Регулярным выражением это не берётся: внутри `order_by(...)` стоят вызовы со
    своими скобками (`func.count()`, `.desc()`), и наивный разбор обрывается на
    первой закрывающей — а с ним обрывается и вся проверка. Первая моя попытка
    так и сделала: объявила «шестьдесят три места без разрешителя», из которых
    сорок пять были с ним.
    """
    gluboko = 0
    for i in range(otkryvayushchaya, len(tekst)):
        if tekst[i] == "(":
            gluboko += 1
        elif tekst[i] == ")":
            gluboko -= 1
            if gluboko == 0:
                return tekst[otkryvayushchaya + 1 : i]
    return ""


def _telo(tekst: str, poz: int) -> str:
    """Текст функции, внутри которой стоит позиция."""
    nachalo = tekst.rfind("\ndef ", 0, poz)
    konets = tekst.find("\ndef ", poz)
    return tekst[nachalo if nachalo >= 0 else 0 : konets if konets > 0 else len(tekst)]


def _imya_funktsii(tekst: str, poz: int) -> str:
    nachalo = tekst.rfind("\ndef ", 0, poz)
    return tekst[nachalo + 5 : tekst.find("(", nachalo)].strip() if nachalo >= 0 else ""


def _uezzhaet_v_stranitsu(tekst: str, poz: int) -> bool:
    """Попадает ли эта выборка в страничный помощник.

    Смотрим и на саму функцию, и на её вызовы в этом же файле: половина
    репозиториев строит запрос отдельным `_search_stmt`, а страницу режет
    вызывающий. Без второго шага проверка объявила бы защищённые выборки
    беззащитными — и список исключений раздулся бы вдвое.
    """
    svoyo = _telo(tekst, poz)
    if any(s in svoyo for s in STRANICHNYE):
        return True
    imya = _imya_funktsii(tekst, poz)
    if not imya:
        return False
    for vyzov in re.finditer(rf"\b{re.escape(imya)}\(", tekst):
        if vyzov.start() == poz:
            continue
        if any(s in _telo(tekst, vyzov.start()) for s in STRANICHNYE):
            return True
    return False


def sortirovki() -> list[tuple[str, int, str, bool]]:
    """Все `order_by` репозиториев: файл, строка, выражение, страничная ли."""
    najdeno = []
    for put in sorted(REPOZITORII.glob("*.py")):
        tekst = put.read_text(encoding="utf-8")
        for m in re.finditer(r"order_by\(", tekst):
            vyrazhenie = " ".join(_skobki(tekst, m.end() - 1).split())
            nomer = tekst[: m.start()].count("\n") + 1
            najdeno.append(
                (put.name, nomer, vyrazhenie, _uezzhaet_v_stranitsu(tekst, m.start()))
            )
    return najdeno


def test_perebor_vidit_sortirovki():
    """Пустой перебор объявил бы порядок полным везде, ничего не прочитав."""
    vse = sortirovki()
    assert len(vse) > 40, f"сортировок нашлось {len(vse)} — разбор сломался"
    assert any("updated_at" in v for _, _, v, _ in vse)
    assert any(stranichnaya for *_, stranichnaya in vse), (
        "ни одной страничной выборки — разбор не дошёл до `page_of`"
    )
    assert any(v.count("(") >= 2 for _, _, v, _ in vse), (
        "ни одного выражения с вложенными вызовами — счёт скобок обрывается"
    )


def test_u_kazhdoy_vyborki_poryadok_polnyy():
    """Порядок доводится до полного — ключом, страничным помощником или доводом."""
    vinovnye = [
        f"{imya}:{nomer}  order_by({vyrazhenie})"
        for imya, nomer, vyrazhenie, stranichnaya in sortirovki()
        if not re.search(r"\.id\b", vyrazhenie)
        and not stranichnaya
        and (imya, vyrazhenie) not in POLNYY_BEZ_KLYUCHA
    ]
    assert vinovnye == [], (
        "порядок неполный — при равных значениях база вправе вернуть что угодно:\n  "
        + "\n  ".join(vinovnye)
        + "\n\nДопишите первичный ключ последним, В ТУ ЖЕ СТОРОНУ, что и основной."
        "\nЕсли ключ там не нужен (группировка, уникальная колонка) — впишите"
        " выборку в POLNYY_BEZ_KLYUCHA с доводом."
    )


def test_v_spiske_isklyucheniy_net_ukazyvayushchikh_v_pustotu():
    """Названная сортировка обязана существовать.

    Иначе запись переживёт свой запрос и станет памяткой о прошлом, а настоящая
    сортировка без разрешителя пройдёт мимо — прикрытая строкой, которая ни на
    что не указывает.
    """
    est = {(imya, vyrazhenie) for imya, _, vyrazhenie, _ in sortirovki()}
    propavshie = sorted(f"{i}: {v}" for i, v in set(POLNYY_BEZ_KLYUCHA) - est)
    assert propavshie == [], (
        "в списке исключений записи без сортировки: " + ", ".join(propavshie)
    )
