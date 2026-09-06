"""Сводка из виджетов: реестр, проверка раскладки, хранение у сотрудника.

Владелец 06.09.2026: блоки сводки добавляются, убираются и перетягиваются;
наблюдение за ключом сайта — отдельный виджет на каждый ключ, без ключа его
добавить нельзя.

Реестр — единственное место, где названы виды виджетов и что им нужно: блок
системы и право. Сервер проверяет раскладку при записи, экран — при показе
(блок могли выключить после записи), и оба смотрят в один список. Данные
виджетов раскладка не хранит: они приходят из `/dashboard` и ручек блоков.
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

#: Ширина в долях сетки из четырёх колонок: плитка — одна, блок — две, полоса — четыре.
SHIRINY = (1, 2, 4)

#: kind → (блок, право, ширина по умолчанию, допустимые ширины, один на сводку).
#: Право записано строкой «область.действие», как на экране (`can`).
REESTR: dict[str, dict] = {
    "money_in_work": {"module": None, "perm": "deals.view_amounts", "w": 1, "shiriny": (1,), "odin": True},
    "money_received": {"module": None, "perm": "deals.view_amounts", "w": 1, "shiriny": (1,), "odin": True},
    "money_won": {"module": None, "perm": "deals.view_amounts", "w": 1, "shiriny": (1,), "odin": True},
    "money_due": {"module": None, "perm": "deals.view_amounts", "w": 1, "shiriny": (1,), "odin": True},
    "avg_check": {"module": None, "perm": "deals.view_amounts", "w": 1, "shiriny": (1,), "odin": True},
    "clients": {"module": None, "perm": None, "w": 1, "shiriny": (1,), "odin": True},
    "calls": {"module": "telephony", "perm": None, "w": 1, "shiriny": (1,), "odin": True},
    "funnel": {"module": None, "perm": None, "w": 2, "shiriny": (2, 4), "odin": True},
    "my_tasks": {"module": "tasks", "perm": None, "w": 2, "shiriny": (2, 4), "odin": True},
    "orders_week": {"module": "orders", "perm": None, "w": 2, "shiriny": (2, 4), "odin": True},
    "low_stock": {"module": "warehouse", "perm": "warehouse.view", "w": 2, "shiriny": (2, 4), "odin": True},
    "showcase_views": {"module": "boards", "perm": None, "w": 4, "shiriny": (2, 4), "odin": True},
    "storage": {"module": None, "perm": None, "w": 4, "shiriny": (2, 4), "odin": True},
    "recent_boards": {"module": "boards", "perm": None, "w": 2, "shiriny": (2, 4), "odin": True},
    "recent_clients": {"module": None, "perm": "clients.view", "w": 2, "shiriny": (2, 4), "odin": True},
    # Ключ сайта: по одному виджету на ключ, ключ обязан существовать.
    "api_key": {"module": None, "perm": "settings.manage", "w": 2, "shiriny": (2, 4), "odin": False},
}


def chitat(user: User) -> dict | None:
    """Раскладка сотрудника или `None` — умолчание экрана."""
    if not user.dashboard_json:
        return None
    try:
        return json.loads(user.dashboard_json)
    except ValueError:
        # Испорченная строка — не повод ронять сводку: показываем умолчание.
        return None


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
        w = v.get("w") or opis["w"]
        if w not in opis["shiriny"]:
            raise errors.ValidationError(f"Widget {kind} cannot be {w} wide", code="bad_widget_width")
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
        itog.append({"kind": kind, "w": w, "params": params})
    return itog


def sohranit(db: Session, user: User, vidzhety: list[dict]) -> dict:
    raskladka = {"version": 1, "widgets": razobrat(db, user, vidzhety)}
    user.dashboard_json = json.dumps(raskladka, separators=(",", ":"))
    db.flush()
    return raskladka


def sbrosit(db: Session, user: User) -> None:
    user.dashboard_json = None
    db.flush()
