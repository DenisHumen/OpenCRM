"""Каждая точка API закрыта осознанно, а не по забывчивости.

Прав около сотни, маршрутов полторы сотни, и раскладывали их руками. Пропуск в
одном маршруте открывает раздел всем — и заметить это чтением нельзя: глазами
проверяют тот файл, который открыли, а открывают обычно не тот.

Поэтому перебор: берём ВСЕ точки API и требуем от каждой либо охранника с
правом, либо строку в списке исключений — с объяснением, почему охранника там
нет. Новый роутер попадает под проверку сам, и добавить его молча не выйдет.
"""

import pytest
from fastapi.routing import APIRoute

from web.main import app
from web.public import routes as public_routes

# Точки без охранника с правом, и почему. Каждая строка — решение, а не
# недосмотр; список короткий намеренно. Пути без префикса `/api/v1`: он
# добавляется при подключении роутера.
EXEMPT: dict[str, str] = {
    "/auth/register": "регистрация: человека в системе ещё нет",
    "/auth/login": "вход: прав ещё нет, проверяется пароль",
    "/auth/logout": "выход доступен любому, кто вошёл",
    "/auth/me": "свой профиль — не раздел системы",
    "/auth/me/password": "свой пароль",
    "/auth/me/avatar": "свой аватар",
    "/auth/heartbeat": "отметка присутствия",
    "/modules": "состав блоков нужен интерфейсу, чтобы знать, что рисовать",
    "/modules/{key}": "переключение блока закрыто своим правом внутри сервиса",
    "/workspace": "название и валюта общие для всех сотрудников",
    "/people": "список сотрудников для выбора ответственного",
    "/dashboard": "сводка сужается по правам внутри, а не закрывается целиком",
    "/search": "поиск отдаёт только то, на что есть право; отбор внутри",
    "/permissions": "матрица прав нужна интерфейсу, чтобы прятать недоступное",
    "/telephony/webhook": "запрос от АТС: сессии нет, проверяется подпись",
    "/arcade/scores": "змейка на странице обслуживания",
    "/arcade/leaderboard": "таблица рекордов змейки на странице обслуживания",
    "/roles/matrix": "состав прав нужен интерфейсу, чтобы прятать недоступное",
    "/system/storage": "место на диске видят все: именно сотрудники загружают файлы",
}

# Точки, меняющие состояние, но закрытые правом на просмотр — и почему это
# верно. Список отдельный от EXEMPT: охранник там есть, вопрос только в том,
# достаточно ли его. Каждая строка объяснена в самом роуте.
WRITE_BY_VIEW_OK: dict[str, str] = {
    "/mail/accounts/{account_id}/sync": (
        "забрать почту — это её чтение; ждать того, кто правит настройки, "
        "ради свежих писем незачем"
    ),
    "/mail/messages/{message_id}/read": (
        "отметка «прочитано» — побочное следствие чтения, а не правка письма"
    ),
}


def api_routes() -> list[APIRoute]:
    """Все точки API, включая вложенные роутеры.

    Подключённые роутеры лежат в `app.routes` не разложенными по одной точке, а
    целыми объектами, и настоящие маршруты живут в `original_router.routes`.
    Плоский перебор нашёл бы две точки из полутора сотен и был бы зелёным,
    ничего не проверив, — поэтому первый тест ниже проверяет сам перебор.

    Публичные страницы (витрина доски, состояние заказа по QR) исключаются по
    списку самого публичного роутера: у них своя защита — токен и PIN.
    """
    public = {route.path for route in public_routes.router.routes}
    found: list[APIRoute] = []
    for included in app.routes:
        original = getattr(included, "original_router", None)
        if original is None:
            continue
        for route in original.routes:
            if isinstance(route, APIRoute) and route.path not in public:
                found.append(route)
    return found


def guards_of(route: APIRoute) -> list[tuple[str, str]]:
    """Права, которыми закрыт маршрут, включая зависимости всего роутера."""
    found: list[tuple[str, str]] = []

    def walk(dependant) -> None:
        permission = getattr(getattr(dependant, "call", None), "opencrm_permission", None)
        if permission is not None:
            found.append(permission)
        for child in dependant.dependencies:
            walk(child)

    walk(route.dependant)
    return found


def test_the_sweep_sees_the_whole_api():
    """Пустой перебор сделал бы все проверки ниже зелёными и бессмысленными."""
    assert len(api_routes()) > 100, "маршруты не собрались — проверка ничего не значит"


@pytest.mark.parametrize(
    "route", api_routes(), ids=lambda r: f"{sorted(r.methods)[0]} {r.path}"
)
def test_every_route_is_guarded_or_listed(route: APIRoute):
    if route.path in EXEMPT:
        return
    assert guards_of(route), (
        f"{sorted(route.methods)} {route.path} не закрыт правом. Либо повесь "
        "require_perm, либо внеси в EXEMPT с объяснением, почему охранника нет."
    )


@pytest.mark.parametrize(
    "route", api_routes(), ids=lambda r: f"{sorted(r.methods)[0]} {r.path}"
)
def test_writing_routes_are_not_guarded_by_view_only(route: APIRoute):
    """Изменять — не то же самое, что смотреть.

    Самая правдоподобная ошибка механической раскладки: на POST или DELETE
    повесили `view`, потому что он стоял выше по файлу. Раздел при этом
    выглядит закрытым, а меняет его всякий, кто может его открыть.
    """
    if route.path in EXEMPT:
        return
    if not (route.methods & {"POST", "PATCH", "PUT", "DELETE"}):
        return
    if route.path in WRITE_BY_VIEW_OK:
        return
    guards = guards_of(route)
    if not guards:
        return  # об этом скажет соседний тест, не дублируем отказ
    assert any(action != "view" for _area, action in guards), (
        f"{sorted(route.methods)} {route.path} закрыт только правом на просмотр: "
        f"{guards}. Изменение должно требовать своего действия."
    )
