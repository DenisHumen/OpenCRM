"""Сводка из виджетов: реестр, проверка раскладки, хранение у сотрудника.

Владелец 06.09.2026: блоки сводки добавляются, убираются и перетягиваются;
наблюдение за ключом сайта — отдельный виджет на каждый ключ, без ключа его
добавить нельзя.

Реестр — единственное место, где названы виды виджетов и что им нужно: блок
системы и право. Сервер проверяет раскладку при записи, экран — при показе
(блок могли выключить после записи), и оба смотрят в один список. Данные
виджетов раскладка не хранит: они приходят из `/dashboard` и ручек блоков.

С 10.09.2026 раскладка — сетка: у виджета есть место (`x`, `y`) и размер
(`w`, `h`) в двенадцати колонках. До этого хранился только порядок и ширина в
четвертях, и такая запись не могла описать ни двух блоков в ряд разной высоты,
ни дырки. Записи первой версии переводятся при чтении — см. `_iz_pervoy`.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import modules_service, permissions_service
from database.models import User
from database.repositories import api_keys as keys_repo

#: Сколько виджетов помещается в раскладку. Больше — не сводка, а лента.
POTOLOK = 40

#: Колонок в сетке. Двенадцать делятся на 2, 3, 4 и 6 — отсюда и ряд плиток по
#: четыре, и «две трети плюс треть», которых сетка из четырёх колонок не умела.
KOLONOK = 12

#: Потолок по вертикали. Не украшение: без него одна запись с `y` в миллион
#: растянула бы сетку на экране в полосу пустоты высотой в километр.
STROK = 400

#: Ширина в четвертях старой сетки → ширина в двенадцатых.
_IZ_CHETVERTEY = {1: 3, 2: 6, 4: 12}

#: kind → блок, право, размер по умолчанию (`w`, `h`), наименьший размер
#: (`min_w`, `min_h`), «один на сводку». Право записано строкой
#: «область.действие», как на экране (`can`).
def _plitka(perm: str | None = None, module: str | None = None, odin: bool = True) -> dict:
    """Плитка с одним числом: четыре в ряд.

    Четыре строки, а не три: под числом стоит поясняющая строка, и у половины
    плиток она в две строки («добавлено за месяц · за неделю · без заявок»).
    В трёх строках такая плитка прокручивалась внутри себя.
    """
    return {"module": module, "perm": perm, "w": 3, "h": 4, "min_w": 2, "min_h": 3, "odin": odin}


def _karta(perm: str | None = None, module: str | None = None, w: int = 6, h: int = 8,
           min_w: int = 4, min_h: int = 6, odin: bool = True) -> dict:
    """Блок со списком или графиком: половина ряда и выше."""
    return {"module": module, "perm": perm, "w": w, "h": h, "min_w": min_w, "min_h": min_h, "odin": odin}


REESTR: dict[str, dict] = {
    "money_in_work": _plitka("deals.view_amounts"),
    "money_received": _plitka("deals.view_amounts"),
    "money_won": _plitka("deals.view_amounts"),
    "money_due": _plitka("deals.view_amounts"),
    "avg_check": _plitka("deals.view_amounts"),
    "clients": _plitka(),
    "calls": _plitka(module="telephony"),
    # Доля отказов: сколько закрытых заявок проиграно. Считается по воронке, а
    # не хранится — как и всё производное.
    "lost_share": _plitka(),
    "funnel": _karta(),
    "my_tasks": _karta(module="tasks"),
    "orders_week": _karta(module="orders"),
    "low_stock": _karta("warehouse.view", module="warehouse"),
    "showcase_views": _karta(module="boards", w=12, h=8, min_w=6),
    "storage": _karta(w=12, h=5, min_w=6, min_h=4),
    "recent_boards": _karta(module="boards", h=9),
    # Клиенты — таблица со своей панелью: поиск, отбор, выгрузка. Строк
    # столько, сколько влезает в высоту виджета.
    "recent_clients": _karta("clients.view", w=12, h=13, min_w=6, min_h=7),
    # Поток денег и источники берут данные не у `/dashboard`, а у отчётов, и
    # право у них поэтому отчётное: считать оборот за год на каждой загрузке
    # сводки — платить за него всем, включая тех, кто блок не держит.
    "money_flow": _karta("reports.view_amounts", w=8, h=9, min_w=6, min_h=7),
    "client_sources": _karta("reports.view", w=4, h=9, min_w=3, min_h=8),
    # Отчёт продаж двумя видами: тепловая карта дней и точечная матрица
    # месяцев. Оба под правом на суммы — под ними стоят деньги за месяц и год.
    "sales_grid": _karta("deals.view_amounts", h=11),
    "sales_matrix": _karta("deals.view_amounts", h=11),
    # Ключ сайта: по одному виджету на ключ, ключ обязан существовать.
    "api_key": _karta("settings.manage", h=7, odin=False),
}


def chitat(user: User) -> dict | None:
    """Раскладка сотрудника или `None` — умолчание экрана."""
    if not user.dashboard_json:
        return None
    try:
        zapis = json.loads(user.dashboard_json)
    except ValueError:
        # Испорченная строка — не повод ронять сводку: показываем умолчание.
        return None
    if not isinstance(zapis, dict):
        return None
    if zapis.get("version") == 1:
        return _iz_pervoy(zapis)
    return zapis


def _iz_pervoy(zapis: dict) -> dict:
    """Запись первой версии в сетку: ширина из четвертей, место — по порядку.

    Перевод при чтении, а не миграцией базы: раскладка лежит строкой JSON у
    каждого сотрудника, и переписать её запросом значило бы разбирать ту же
    строку в SQL. Запись переезжает сама при первом же сохранении.
    """
    mesto: list[dict] = []
    x = y = 0
    ryad = 0
    for v in zapis.get("widgets") or []:
        kind = str(v.get("kind") or "")
        opis = REESTR.get(kind)
        if opis is None:
            continue
        w = _IZ_CHETVERTEY.get(v.get("w") or 0) or opis["w"]
        h = opis["h"]
        if x + w > KOLONOK:
            x, y = 0, y + ryad
            ryad = 0
        mesto.append({"kind": kind, "x": x, "y": y, "w": w, "h": h, "params": dict(v.get("params") or {})})
        x += w
        ryad = max(ryad, h)
    return {"version": 2, "widgets": mesto}


def _tseloe(znachenie, imya: str, ot: int, do: int) -> int:
    if isinstance(znachenie, bool) or not isinstance(znachenie, int):
        raise errors.ValidationError(f"Widget {imya} must be a whole number", code="bad_widget_place")
    if znachenie < ot or znachenie > do:
        raise errors.ValidationError(f"Widget {imya} out of grid", code="bad_widget_place")
    return znachenie


def _peresekayutsya(a: dict, b: dict) -> bool:
    return (a["x"] + a["w"] > b["x"] and b["x"] + b["w"] > a["x"]
            and a["y"] + a["h"] > b["y"] and b["y"] + b["h"] > a["y"])


def razobrat(db: Session, user: User, vidzhety: list[dict]) -> list[dict]:
    """Проверить раскладку и вернуть её в нормальном виде.

    Отказ — на первом же неверном виджете, с кодом: экран показывает причину,
    а не молча теряет виджет.
    """
    if len(vidzhety) > POTOLOK:
        raise errors.ValidationError(f"Too many widgets (max {POTOLOK})", code="too_many_widgets")
    itog: list[dict] = []
    videno: set[tuple[str, int | None]] = set()
    for v in vidzhety:
        kind = str(v.get("kind") or "")
        opis = REESTR.get(kind)
        if opis is None:
            raise errors.ValidationError(f"Unknown widget: {kind}", code="unknown_widget")
        if opis["module"] and not modules_service.is_enabled(db, opis["module"]):
            raise errors.ValidationError(f"Module '{opis['module']}' is switched off", code="module_disabled")
        if opis["perm"]:
            area, action = opis["perm"].split(".", 1)
            if not permissions_service.has(db, user, area, action):
                raise errors.ForbiddenError(f"Permission required: {opis['perm']}", code="permission_denied")
        w = opis["w"] if v.get("w") is None else _tseloe(v.get("w"), "w", 1, KOLONOK)
        h = opis["h"] if v.get("h") is None else _tseloe(v.get("h"), "h", 1, STROK)
        if w < opis["min_w"]:
            raise errors.ValidationError(f"Widget {kind} cannot be {w} wide", code="bad_widget_width")
        if h < opis["min_h"]:
            raise errors.ValidationError(f"Widget {kind} cannot be {h} tall", code="bad_widget_height")
        x = 0 if v.get("x") is None else _tseloe(v.get("x"), "x", 0, KOLONOK - 1)
        y = 0 if v.get("y") is None else _tseloe(v.get("y"), "y", 0, STROK - 1)
        if x + w > KOLONOK:
            raise errors.ValidationError(f"Widget {kind} sticks out of the grid", code="bad_widget_place")
        params = dict(v.get("params") or {})
        key_id: int | None = None
        if kind == "api_key":
            key_id = params.get("key_id")
            if not isinstance(key_id, int) or isinstance(key_id, bool):
                raise errors.ValidationError("Widget api_key needs key_id", code="widget_needs_key")
            if keys_repo.get(db, key_id) is None:
                raise errors.NotFoundError("API key not found", code="api_key_not_found")
            params = {"key_id": key_id}
        else:
            params = {}
        metka = (kind, key_id)
        if metka in videno:
            raise errors.ValidationError(f"Widget {kind} is already on the dashboard", code="widget_duplicate")
        videno.add(metka)
        mesto = {"kind": kind, "x": x, "y": y, "w": w, "h": h, "params": params}
        # Наложение отвергается, а не чинится: починить его молча значит
        # переставить блок, которого человек не двигал, и он этого не поймёт.
        for chuzhoe in itog:
            if _peresekayutsya(mesto, chuzhoe):
                raise errors.ValidationError(
                    f"Widget {kind} overlaps {chuzhoe['kind']}", code="widgets_overlap"
                )
        itog.append(mesto)
    return itog


def sohranit(db: Session, user: User, vidzhety: list[dict]) -> dict:
    raskladka = {"version": 2, "widgets": razobrat(db, user, vidzhety)}
    user.dashboard_json = json.dumps(raskladka, separators=(",", ":"))
    db.flush()
    return raskladka


def sbrosit(db: Session, user: User) -> None:
    user.dashboard_json = None
    db.flush()
