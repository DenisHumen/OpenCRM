"""Машинное описание `/api/v1` для токена — то, из чего MCP собирает инструменты.

В боевой сборке `/api/openapi.json` выключен; здесь — только маршруты, куда токен
пускают: под входом сотрудника и не из `token_service.ZAKRYTO`.
"""

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

from core.services import token_service
from web.api.deps import get_current_user

_kesh: dict[int, dict] = {}


def _pod_vhodom(dependant) -> bool:
    return any(d.call is get_current_user or _pod_vhodom(d) for d in dependant.dependencies)


def dostupnye(app: FastAPI) -> list[RouteContext]:
    """Маршруты `/api/v1`, куда токен пустят. Запреты — по пути внутри роутера."""
    return [
        rc
        for rc in iter_route_contexts(app.routes)
        if isinstance(rc.original_route, APIRoute)
        and (rc.path or "").startswith("/api/v1/")
        and _pod_vhodom(rc.original_route.dependant)
        and not any(token_service.zakryto(m, rc.original_route.path) for m in rc.methods or ())
    ]


def sobrat(app: FastAPI, adres: str) -> dict:
    if id(app) not in _kesh:
        _kesh[id(app)] = get_openapi(
            title=app.title,
            version=app.version,
            description=(
                "OpenCRM internal API for employee tokens: `Authorization: Bearer ocrm_…`. "
                "A token acts with its employee's role; errors are `{error: {code, message}}`."
            ),
            routes=dostupnye(app),
        )
    shema = dict(_kesh[id(app)])
    shema["servers"] = [{"url": adres}]
    shema["components"] = {
        **shema.get("components", {}),
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
    }
    shema["security"] = [{"bearer": []}]
    return shema
