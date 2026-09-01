"""Каждая ручка API описана в `docs/04-api.md`.

**Зачем сторож.** По этому документу пишут внешние: магазин ходит в наши ручки,
и «разобраться должен любой» — требование владельца, а не пожелание. Документ
при этом правится руками, а ручки заводятся кодом, и расходятся они молча: новый
маршрут просто не появляется в справочнике, и узнать о нём можно только из
исходников.

Расхождение уже было, и не мелкое: **весь блок накладных — двенадцать ручек —
отсутствовал в документе целиком**, при том что раздел есть у каждого другого
блока. Плюс выгрузка клиентов, звёзды GitHub, метки источника у телеграма и
возврат сотрудника в строй.

Проверка сравнивает МАРШРУТЫ, а не описания: она не умеет судить, верно ли
описано право или отказ. Её работа — не дать ручке остаться неназванной вовсе.
"""

import pathlib
import re

KOREN = pathlib.Path(__file__).resolve().parent.parent
MARSHRUTY = KOREN / "web" / "api" / "routes"
SPRAVOCHNIK = KOREN / "docs" / "04-api.md"

#: Имя параметра пути в документе и в коде не совпадает нарочно: код пишет
#: `{client_id}`, документ — `{id}`. Сравниваем по форме, а не по имени.
PARAMETR = re.compile(r"\{[^}]+\}")


def _obshchiy_vid(put: str) -> str:
    """Путь без имён параметров и без строки запроса."""
    return PARAMETR.sub("{}", put.split("?", 1)[0]).rstrip("/") or "/"


def marshruty() -> list[tuple[str, str, str]]:
    """Все ручки: метод, путь с приставкой роутера, файл."""
    najdeno = []
    for fayl in sorted(MARSHRUTY.glob("*.py")):
        tekst = fayl.read_text(encoding="utf-8")
        # Приставка своя у каждого роутера в файле: у склада их два
        # (`/warehouse` и `/warehouses`), и общая приставка склеила бы их.
        pristavki = {
            imya: (re.search(r'prefix="([^"]*)"', telo).group(1)
                   if re.search(r'prefix="([^"]*)"', telo) else "")
            for imya, telo in re.findall(r"(\w+)\s*=\s*APIRouter\((.*?)\)\n", tekst, re.S)
        }
        for imya, metod, put in re.findall(
            r'@(\w+)\.(get|post|patch|put|delete)\("([^"]*)"', tekst
        ):
            najdeno.append((metod.upper(), (pristavki.get(imya, "") + put) or "/", fayl.name))
    return najdeno


def opisannye() -> set[str]:
    """Пути, названные в справочнике, — в общем виде."""
    tekst = SPRAVOCHNIK.read_text(encoding="utf-8")
    return {_obshchiy_vid(m) for m in re.findall(r"`(/[A-Za-z0-9_./{}?=&-]+)`", tekst)}


def test_perebor_marshrutov_ne_pustoy():
    """Страховка от переименований: пустой список проверил бы пустоту.

    Разбор идёт по исходникам регулярным выражением, и первая же правка вида
    `@router.get(f"...")` вынесла бы половину маршрутов из счёта молча.
    """
    najdeno = marshruty()
    assert len(najdeno) > 200, f"маршрутов нашлось {len(najdeno)} — разбор сломался"
    assert any(put == "/deals/{deal_id}/lines" for _, put, _ in najdeno)
    assert any(put == "/waybills/{waybill_id}/post" for _, put, _ in najdeno)


def test_kazhdaya_ruchka_nazvana_v_spravochnike():
    """Ручка, которой нет в документе, для внешнего не существует."""
    v_dokumente = opisannye()
    net = sorted(
        {f"{metod} {put}" for metod, put, _ in marshruty()
         if _obshchiy_vid(put) not in v_dokumente}
    )
    assert not net, (
        "не описаны в docs/04-api.md:\n  " + "\n  ".join(net)
    )
